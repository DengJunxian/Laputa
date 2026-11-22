from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from civitas.application import economic_run
from civitas.cli_v4 import main
from civitas.contracts.base import content_hash
from civitas.experiments.institutions import audit_institutions
from civitas.scenarios.closed_economy_config import LaborShiftIntervention
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from scripts.verify_joint_shift import audit, freeze


class JointWorldTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_shift_branches_share_state_and_only_treatment_executes(self):
        config = {'households': 4, 'ticks': 3, 'fault_tick': None, 'archive_observations': True,
                  'fiscal': {'announced_tick': 2, 'effective_tick': 3, 'initial_cash_minor': 30, 'audience': [1],
                             'brackets': [{'start_minor': 0, 'rate_bps': 2000}], 'transfer_recipients': []},
                  'banking': {'credit_creation_mode': 'bank_credit_creation',
                              'initial_bank_cash': {'bank:a': 3, 'bank:b': 3}}}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent, control, treatment = (root / name for name in ('parent', 'control', 'short_shift'))
            await economic_run.run_new(config, parent, until=1)
            spec = root / 'shorter-shift.json'
            atomic_json(spec, {'kind': 'labor_income_shock', 'schema_version': 1,
                               'shock': {'tick': 2, 'household_indices': [0, 1], 'work_minutes': 240}})
            plan = freeze(CommitStore(parent), LaborShiftIntervention.model_validate(read_json(spec)))
            atomic_json(root / 'plan.json', {'plan': plan, 'plan_hash': content_hash(plan)})
            self.assertEqual(audit(root)['completed_arms'], 0)
            baseline = economic_run.branch_run(parent, tick=1, destination=treatment)
            with self.assertRaisesRegex(ValueError, 'short_shift does not match the frozen assignment'):
                audit(root)
            treatment.rename(control)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(['branch', str(parent), '--tick', '1', '--output', str(treatment),
                             '--intervention', str(spec)])
            self.assertEqual(code, 0)
            applied = json.loads(stdout.getvalue())
            self.assertEqual((applied['parent'], applied['state_hash'], applied['shared_rng']),
                             (baseline['parent'], baseline['state_hash'], baseline['shared_rng']))
            await economic_run.resume_run(control)
            self.assertIsNone(audit(root)['summary'])
            await economic_run.resume_run(treatment, until=2)
            await economic_run.resume_run(treatment)
            for path, expected, reached in ((control, [48, 48, 48], []),
                                             (treatment, [48, 36, 48], ['household:0000', 'household:0001'])):
                store = CommitStore(path)
                self.assertNotIn('labor_income_shock', store.identity.config)
                _, snapshot = store.load()
                macro = snapshot.data['closed_economy']['macro_history']
                self.assertEqual([row['wage_paid_minor'] for row in macro], expected)
                facts = [o.actor_id for m in store.markers() for o in store.observation_rows(m)
                         if any(f.value.get('kind') == 'labor_income_shock' for f in o.market_view)]
                self.assertEqual(facts, reached)
                replay = await economic_run.replay_run(path, destination=root / (path.name + '-replay'))
                self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 2))
            paired = audit(root)
            self.assertEqual(paired['complete_pairs'], 1)
            self.assertEqual(paired['summary']['primary_effect_minor'], -12)
            self.assertEqual(paired['summary']['impact_period']['delta']['produced_units'], -4)
            self.assertEqual(paired['summary']['impact_period']['delta']['fiscal_notice_reached'], 0)

    async def test_shift_rejects_wrong_period_invalid_target_and_duplicate_assignment(self):
        config = {'households': 4, 'ticks': 4, 'fault_tick': None}
        good = {'kind': 'labor_income_shock', 'shock': {'tick': 2, 'household_indices': [0], 'work_minutes': 240}}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent, child = root / 'parent', root / 'child'
            await economic_run.run_new(config, parent, until=1)
            invalid = [({**good, 'shock': {**good['shock'], **change}}, error) for change, error in (
                ({'tick': 1}, 'next uncommitted'), ({'tick': 3}, 'next uncommitted'),
                ({'household_indices': [4]}, 'in-range households'),
                ({'household_indices': [0, 0]}, 'distinct in-range'),
                ({'work_minutes': 121}, 'integral wage'),
            )]
            invalid.append(({**good, 'kind': 'set_cash'}, 'labor_income_shock'))
            for spec, message in invalid:
                with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, message):
                    economic_run.branch_run(parent, tick=1, destination=child, intervention=spec)
                self.assertFalse(child.exists())
            economic_run.branch_run(parent, tick=1, destination=child, intervention=good)
            await economic_run.resume_run(child, until=2)
            later = {**good, 'shock': {**good['shock'], 'tick': 3}}
            with self.assertRaisesRegex(ValueError, 'already scheduled'):
                economic_run.branch_run(child, tick=2, destination=root / 'second', intervention=later)
            nested = root / 'nested'
            economic_run.branch_run(child, tick=1, destination=nested)
            resumed = await economic_run.resume_run(nested, until=2)
            self.assertEqual(resumed['state_hash'], CommitStore(child).markers()[-1].state_hash)

    def test_income_shock_rejects_out_of_range_or_fractional_terms(self):
        for shock in (
            {'tick': 2, 'household_indices': [4], 'work_minutes': 240},
            {'tick': 2, 'household_indices': [0], 'work_minutes': 121},
        ):
            with self.subTest(shock=shock), self.assertRaisesRegex(ValueError, 'integral wage and production'):
                economic_run.parse_config({'ticks': 3, 'households': 4, 'labor_income_shock': shock})

    async def test_shorter_shift_changes_paid_wages_only_at_the_frozen_boundary(self):
        config = {
            'ticks': 3, 'households': 4, 'fault_tick': None, 'archive_observations': True,
            'labor_income_shock': {'tick': 2, 'household_indices': [0, 1], 'work_minutes': 240},
            'fiscal': {'announced_tick': 1, 'effective_tick': 2, 'audience': [],
                       'brackets': [{'start_minor': 0, 'rate_bps': 2000}], 'transfer_recipients': []},
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'joint'
            await economic_run.run_new(config, path, until=1)
            await economic_run.resume_run(path)
            world, store, _ = economic_run.open_run(path)
            history = world.snapshot.data['closed_economy']['macro_history']
            self.assertEqual([row['wage_paid_minor'] for row in history], [48, 36, 48])
            self.assertEqual([row['produced_units'] for row in history], [16, 12, 16])
            marker, before = store.load(1)
            for observation in store.observation_rows(marker):
                self.assertFalse(any(f.value.get('kind') == 'labor_income_shock' for f in observation.market_view))
            self.assertEqual(audit_institutions(before)['fiscal']['collected_minor'], 0)
            marker, shock = store.load(2)
            self.assertEqual(audit_institutions(shock)['fiscal']['collected_minor'], 6)
            reached = [obs.actor_id for obs in store.observation_rows(marker)
                       if any(f.value.get('kind') == 'labor_income_shock' for f in obs.market_view)]
            self.assertEqual(reached, ['household:0000', 'household:0001'])
            replay = await economic_run.replay_run(path, destination=path.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 3))

    async def test_no_intervention_branch_from_earlier_commit_matches_parent_suffix(self):
        config = {'households': 4, 'ticks': 5, 'archive_observations': True, 'fault_tick': None}
        with TemporaryDirectory() as tmp:
            parent, child = Path(tmp) / 'parent', Path(tmp) / 'child'
            await economic_run.run_new(config, parent, until=3)
            source = CommitStore(parent)
            prefix = {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(['branch', str(parent), '--tick', '2', '--output', str(child)])
            self.assertEqual(code, 0)
            plan = json.loads(stdout.getvalue())
            self.assertEqual((plan['status'], plan['tick']), ('planned', 2))
            self.assertEqual(prefix, {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()})
            fork = CommitStore(child)
            original, _ = source.load(2)
            self.assertEqual(plan['parent']['commit_hash'], original.commit_hash)
            self.assertEqual(fork.identity.rng, source.identity.rng)
            self.assertEqual(fork.identity.config_hash, source.identity.config_hash)
            self.assertEqual(fork.identity.lineage_id, source.identity.lineage_id)
            self.assertNotEqual(fork.identity.run_id, source.identity.run_id)
            self.assertEqual([(m.tick, m.state_hash) for m in fork.markers()], [(2, original.state_hash)])
            await economic_run.resume_run(parent)
            await economic_run.resume_run(child)
            for tick in range(3, 6):
                a, sa = source.load(tick)
                b, sb = fork.load(tick)
                self.assertEqual((a.state_hash, sa), (b.state_hash, sb))
                self.assertEqual(source.observation_rows(a), fork.observation_rows(b))
            verified = await economic_run.replay_run(child, destination=Path(tmp) / 'verify')
            self.assertEqual((verified['status'], verified['initial_tick'], verified['compared_ticks']),
                             ('matched', 2, 3))

    async def test_branch_rejects_uncommitted_tick_and_unknown_parent_participant(self):
        with TemporaryDirectory() as tmp:
            parent, child = Path(tmp) / 'parent', Path(tmp) / 'child'
            await economic_run.run_new({'households': 4, 'ticks': 3}, parent, until=1)
            with self.assertRaisesRegex(ValueError, 'No committed tick 2'):
                economic_run.branch_run(parent, tick=2, destination=child)
            with self.assertRaisesRegex(ValueError, 'outside the parent'):
                economic_run.branch_run(parent, tick=1, destination=parent / 'child')
            identity_file = parent / 'identity.json'
            identity = read_json(identity_file)
            identity['participants']['economic_runtime'] = 100
            atomic_json(identity_file, identity)
            with self.assertRaisesRegex(ValueError, 'Participant versions'):
                economic_run.branch_run(parent, tick=1, destination=child)
            self.assertFalse(child.exists())

    async def test_branch_rejects_replaced_parent_even_with_identical_business_state(self):
        config = {'households': 4, 'ticks': 3, 'fault_tick': None}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent, child = root / 'parent', root / 'child'
            await economic_run.run_new(config, parent, until=1)
            plan = economic_run.branch_run(parent, tick=1, destination=child)
            parent.rename(root / 'original')
            replacement = await economic_run.run_new(config, parent, until=1, run_id='replacement')
            self.assertEqual(replacement['state_hash'], plan['state_hash'])
            with self.assertRaisesRegex(ValueError, 'Parent reference or branch prefix mismatch'):
                await economic_run.resume_run(child)
            with self.assertRaisesRegex(ValueError, 'Parent reference or branch prefix mismatch'):
                await economic_run.replay_run(child, destination=root / 'invalid-replay')
            self.assertFalse((root / 'invalid-replay').exists())
            self.assertEqual([m.tick for m in CommitStore(child).markers()], [1])

    async def test_branch_recovers_from_failed_publication_and_failed_child_commit(self):
        with TemporaryDirectory() as tmp:
            parent, child = Path(tmp) / 'parent', Path(tmp) / 'child'
            await economic_run.run_new({'households': 4, 'ticks': 3}, parent, until=1)
            with patch('civitas.application.economic_run.registry.record', side_effect=OSError('plan interrupted')):
                with self.assertRaisesRegex(OSError, 'plan interrupted'):
                    economic_run.branch_run(parent, tick=1, destination=child)
            self.assertFalse(child.exists())
            plan = economic_run.branch_run(parent, tick=1, destination=child)
            with self.assertRaises(FileExistsError):
                economic_run.branch_run(parent, tick=1, destination=child)
            with self.assertRaisesRegex(ValueError, 'after current head'):
                await economic_run.resume_run(child, until=0)

            def fail(point):
                if point == 'before_marker':
                    raise OSError('child commit interrupted')

            world, store, _ = economic_run.open_run(child)
            store.fault_hook = fail
            with self.assertRaisesRegex(OSError, 'child commit interrupted'):
                await economic_run.advance(world, store)
            self.assertEqual([m.commit_hash for m in CommitStore(child).markers()], [plan['commit_hash']])
            resumed = await economic_run.resume_run(child)
            expected = await economic_run.resume_run(parent)
            self.assertEqual(resumed['state_hash'], expected['state_hash'])
