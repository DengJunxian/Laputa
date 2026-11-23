from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.application import economic_run
from civitas.cli_v4 import main
from civitas.contracts.base import content_hash
from civitas.experiments.institutions import audit_institutions
from civitas.scenarios.closed_economy_config import LaborShiftIntervention
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from scripts.verify_joint_shift import audit, freeze


SECURITY_FEEDBACK_FIXTURE = Path(__file__).resolve().parents[1] / 'examples/closed-economy-security-feedback.json'


class JointWorldTests(unittest.IsolatedAsyncioTestCase):
    async def test_security_shock_reaches_joint_economy(self):
        config = read_json(SECURITY_FEEDBACK_FIXTURE)
        feedback = config.pop('security_feedback')
        with self.assertRaisesRegex(ValueError, 'exclusive funded asset'):
            build_closed_economy(config)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stores = {}
            for arm, toggles in (('shock', {}), ('flat', {'shock_enabled': False}),
                                 ('hidden', {'wealth_observed': False})):
                raw = {**config, 'security_feedback': {**feedback, **toggles}}
                world, store = economic_run.create_run(raw, root / arm)
                self.assertEqual([p.participant_id for p in world.participants], ['economic_runtime'])
                self.assertEqual(store.identity.metadata['economic_model'], 'joint_prefunded_security_and_institutions')
                stores[arm] = store
                await economic_run.resume_run(root / arm)
            for tick in (0, 1):
                self.assertEqual(len({s.load(tick)[0].state_hash for s in stores.values()}), 1)

            impact = {arm: store.load(2)[1] for arm, store in stores.items()}
            books = {arm: snap.data['economic_runtime'] for arm, snap in impact.items()}
            self.assertEqual(books['shock']['balances'], books['flat']['balances'])
            self.assertEqual(books['shock']['decision_cash_budget'], books['flat']['decision_cash_budget'])
            book = books['shock']
            self.assertEqual((book['schema_version'], book['good_id']), (3, 'good'))
            self.assertTrue({'bank:a', 'bank:b', 'public:treasury', 'firm:01', 'household:0007'} <= book['balances'].keys())
            self.assertEqual(sum(book['security_units'].values()), 4)
            self.assertEqual(book['security_units']['firm:01'], 1)
            self.assertEqual(book['balances']['firm:01']['security_inventory'], 100)
            mark, = [e for e in book['journal'] if e['kind'] == 'security_mark']
            self.assertEqual((mark['payload']['price_minor'], mark['postings'], mark['goods_movements']), (10, [], []))
            fill, = [e for e in book['journal'] if e['kind'] == 'security_trade']
            seller = fill['payload']['seller_id']
            self.assertEqual(book['balances'][seller]['sales_income'], 0)
            self.assertEqual(book['balances'][seller]['security_sale_proceeds'], -100)
            self.assertEqual(book['balances'][seller]['security_disposal_cost'], 100)
            self.assertEqual(book['decision_cash_budget'][seller], book['balances'][seller]['cash'] - 100)
            self.assertEqual(sum(row['cash'] for row in book['balances'].values()), sum(book['initial_cash'].values()))
            self.assertEqual(fill['goods_movements'][0]['resource_id'], 'security:A')
            linked = [r for r in impact['shock'].receipts if fill['tx_id'] in r.ledger_tx_ids]
            self.assertEqual(sorted(r.cash_delta_minor for r in linked), [-100, 100])
            self.assertTrue(all(r.unit == 'security_units' and r.goods_delta_units == 0 for r in linked))
            self.assertTrue(any(r.outcome == 'security_unfilled' for r in impact['shock'].receipts))

            for arm, store in stores.items():
                marker, snapshot = store.load(3)
                private = {o.actor_id: (o, f) for o in store.observation_rows(marker) for f in o.market_view
                           if f.value.get('kind') == 'security_wealth'}
                holders = {a for a, units in book['security_units'].items() if units and a.startswith('household:')}
                self.assertEqual(set(private), holders if arm != 'hidden' else set())
                self.assertFalse(any(f.value.get('kind') == 'security_wealth'
                    for o in store.observation_rows(store.load(2)[0]) for f in o.market_view))
                for actor, (obs, fact) in private.items():
                    self.assertEqual(fact.owner_id, actor)
                    self.assertEqual(fact.value['mark_tx_id'], mark['tx_id'])
                    change = -90 if arm == 'shock' else 0
                    self.assertEqual(fact.value['market_value_change_minor'], change)
                    self.assertEqual(fact.value['marked_net_worth_minor'], obs.account_view[0].value['net_worth_minor'] + change)
                    decision, = [d for d in snapshot.decisions if d.actor_id == actor and d.tick == 3]
                    self.assertIn(fact.fact_id, decision.evidence_ids)
                    self.assertEqual(decision.belief_patch['security_wealth_response']['reserve_increase_minor'], -change)
                    self.assertEqual(sum(i.quantity_units for i in decision.economic_intents if i.kind == 'purchase'),
                                     0 if arm == 'shock' else 2)
                current = snapshot.data['economic_runtime']['balances']
                self.assertEqual((current['bank:a']['loan_asset'], current['household:0003']['loan_payable']), (12, -12))
                self.assertEqual((current['bank:a']['deposit_payable'], current['household:0003']['deposit_asset']), (-12, 12))
                _, final = store.load(5)
                own = final.data['closed_economy']
                sales = 10 if arm == 'shock' else 16
                macro = own['macro_history'][2]
                self.assertEqual((macro['sold_units_by_seller']['firm:00'], macro['consumed_units']), (sales, sales))
                self.assertEqual(macro['sales_minor_by_seller']['firm:00'], 11 * sales)
                work, = [e for e in own['labor_events'] if e['tick'] == 5 and e['worker_id'] == 'household:0001']
                self.assertEqual(work['employer_id'], 'firm:01' if arm == 'shock' else 'firm:00')
                final_book = final.data['economic_runtime']
                audited = audit_institutions(final)['economy']
                self.assertEqual((audited['security_final_units'], audited['security_volume_units']), (4, 1))
                self.assertEqual(audited['goods_final_units'], sum(final_book['goods'].values()))
                wage, = [e for e in final_book['journal'] if e['tx_id'] == work['payment_tx_id']]
                self.assertEqual((wage['payload']['employer_id'], wage['details']['paid_minor']), (work['employer_id'], 12))
                fiscal, = [e for e in final_book['journal'] if e['kind'] == 'fiscal' and e['tick'] == 4]
                response = fiscal['details']['consumption_response']
                self.assertEqual((response['consumed_units'], response['triggered']), (sales, arm == 'shock'))
                self.assertEqual(fiscal['details']['transfers_paid_minor'], 2 if arm == 'shock' else 0)
                self.assertEqual(set(response['source_tx_ids']), {
                    e['tx_id'] for e in final_book['journal'] if e['kind'] == 'consumption' and e['tick'] == 3})

    async def test_joint_security_rolls_back_and_replays_without_a_second_wallet(self):
        config = read_json(SECURITY_FEEDBACK_FIXTURE)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            world, store = economic_run.create_run(config, root / 'interrupted')
            await world.step()
            before = world.snapshot

            def fail_before_marker(point):
                if point == 'before_marker':
                    raise OSError('failed joint period before marker')

            store.fault_hook = fail_before_marker
            with self.assertRaisesRegex(OSError, 'failed joint period before marker'):
                await world.step()
            self.assertEqual(world.snapshot, before)
            self.assertEqual(store.markers()[-1].tick, 1)
            self.assertIsNone(world.participants[0].security_mark)
            self.assertEqual(world.participants[0].security_units('firm:01'), 0)
            resumed = await economic_run.resume_run(root / 'interrupted')
            clean = await economic_run.run_new(config, root / 'clean')
            self.assertEqual(resumed['state_hash'], clean['state_hash'])
            loaded, _, _ = economic_run.open_run(root / 'interrupted')
            committed = loaded.snapshot
            changed = committed.model_dump(mode='json')
            changed['data']['economic_runtime']['security_units']['firm:01'] += 1
            with self.assertRaisesRegex(ValueError, 'Security quantities differ from journal reconstruction'):
                loaded.restore_snapshot(type(committed).model_validate(changed))
            self.assertEqual(loaded.snapshot, committed)
            replay = await economic_run.replay_run(root / 'interrupted', destination=root / 'verification')
            self.assertEqual((replay['status'], replay['compared_ticks'], replay['state_hash']),
                             ('matched', 5, clean['state_hash']))
            self.assertFalse(replay['applied_deltas_to_execution'])

    async def test_joint_security_no_trade_still_marks_wealth_without_cash_postings(self):
        config = read_json(SECURITY_FEEDBACK_FIXTURE)
        config['initial_firm_cash_minor'] = 95
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / 'no-fill'
            await economic_run.run_new(config, root, until=3)
            store = CommitStore(root)
            _, impact = store.load(2)
            period = impact.data['closed_economy']['security_session']
            self.assertEqual((period['close_minor'], period['fill_count'], period['journal_tx_ids']), (None, 0, []))
            receipt, = [r for r in impact.receipts if r.unit == 'security_units' and r.actor_id == 'firm:01']
            self.assertEqual((receipt.status, receipt.reason_codes), ('rejected', ['insufficient_security_cash']))
            marker, current = store.load(3)
            book = current.data['economic_runtime']
            self.assertEqual(book['security_units']['firm:01'], 0)
            mark, = [e for e in book['journal'] if e['kind'] == 'security_mark']
            self.assertFalse(mark['postings'])
            obs = next(o for o in store.observation_rows(marker) if o.actor_id == 'household:0000')
            fact, = [f for f in obs.market_view if f.value.get('kind') == 'security_wealth']
            self.assertEqual((fact.value['market_value_minor'], fact.value['market_value_change_minor']), (10, -90))

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
