import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from civitas.application import economic_run as runner
from civitas.cognition.providers import ArchivedProvider, SyntheticProvider
from civitas.cognition.runtime import RunControlError
from civitas.storage.commit_store import CommitStore, atomic_json


class ClosedEconomyCognitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_joint_supplier_run_starts_from_frozen_raw_response_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            config = {'ticks': 3, 'households': 4, 'archive_observations': True, 'fault_tick': None,
                      'banking': {'credit_creation_mode': 'bank_credit_creation',
                                  'initial_bank_cash': {'bank:a': 4, 'bank:b': 5}},
                      'fiscal': {'announced_tick': 2, 'effective_tick': 3, 'audience': [0],
                                 'brackets': [{'start_minor': 0, 'rate_bps': 2000}], 'transfer_recipients': []}}
            settings = {'mode': 'L', 'ticks': [3], 'reflect': False}
            await runner.run_new({**config, 'supplier_cognition': settings}, folder / 'author')
            generated, = CommitStore(folder / 'author').archived_records()
            archive = {'schema_version': 1, 'records': [{k: generated[k] for k in ('request', 'response')}]}
            await runner.run_new(config, folder / 'rule')
            root = folder / 'archived'
            cfg = {**config, 'supplier_cognition': {**settings, 'source': 'archived_response'}}
            with patch.object(SyntheticProvider, 'complete', side_effect=AssertionError('regenerated archive')):
                paused = await runner.run_new(cfg, root, until=2, response_archive=archive)
                world, store, before = runner.open_run(root)
                memory = world.policy.memory.export_state()

                def interrupt(point):
                    if point == 'before_marker':
                        raise OSError('before archived purchase commit')

                store.fault_hook = interrupt
                with self.assertRaisesRegex(OSError, 'before archived purchase commit'):
                    await world.step()
                self.assertEqual(store.markers()[-1], before)
                self.assertEqual(world.policy.memory.export_state(), memory)
                staged, = world.model_runtime.store.records()
                with patch.object(ArchivedProvider, 'complete', side_effect=AssertionError('archive reread on retry')):
                    result = await runner.resume_run(root)
                self.assertEqual(CommitStore(root).archived_records(), [staged])
                changed = {**archive, 'records': [{**archive['records'][0],
                           'response': {**generated['response'], 'text': '{}'}}]}
                atomic_json(root / 'response-archive.json', changed)
                with self.assertRaisesRegex(ValueError, 'Frozen response archive hash mismatch'):
                    runner.open_run(root)
                replay = await runner.replay_run(root, destination=folder / 'replay')
            rule, archived = CommitStore(folder / 'rule'), CommitStore(root)
            for tick in (0, 1, 2):
                a, sa = rule.load(tick)
                b, sb = archived.load(tick)
                self.assertEqual(sa.data['economic_runtime'], sb.data['economic_runtime'])
                fields = {'policy_id', 'policy_version'}
                self.assertEqual([row.model_dump(exclude=fields) for row in sa.decisions],
                                 [row.model_dump(exclude=fields) for row in sb.decisions])
                self.assertEqual(rule.observation_rows(a), archived.observation_rows(b))
            self.assertEqual(paused['tick'], 2)
            row, = archived.archived_records()
            self.assertEqual(row['response'], generated['response'])
            self.assertEqual(row['request']['configuration']['source'], 'archived_response')
            self.assertEqual((row['state'], row['accounted']), ('success', 0))
            self.assertEqual((replay['status'], replay['state_hash']), ('matched', result['state_hash']))

    async def test_archive_refusal_falls_back_and_unauthorized_or_missing_response_does_not_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            settings = {'mode': 'M', 'ticks': [3], 'social_weight_bps': 2500, 'reflect': False}
            config = {'ticks': 3, 'households': 4, 'supplier_cognition': settings}
            await runner.run_new(config, folder / 'author')
            original, = CommitStore(folder / 'author').archived_records()
            body = json.loads(original['response']['text'])
            body['evidence_ids'].append('account:3:v2:household:0001')
            target = {**config, 'supplier_cognition': {**settings, 'source': 'archived_response'}}
            for case, response, request, error in (
                ('refusal', {**original['response'], 'ok': False, 'error_code': 'refusal', 'text': ''}, original['request'], None),
                ('unauthorized', {**original['response'], 'text': json.dumps(body)}, original['request'], 'invalid_response'),
                ('miss', original['response'], {**original['request'], 'prompt_version': 'wrong-version'}, 'transport_error'),
            ):
                with self.subTest(case=case):
                    path = folder / case
                    archive = {'records': [{'request': request, 'response': response}]}
                    paused = await runner.run_new(target, path, until=2, response_archive=archive)
                    if error is None:
                        result = await runner.resume_run(path)
                        world, store, _ = runner.open_run(path)
                        call, = store.archived_records()
                        choice = next(d for d in world.snapshot.decisions if d.actor_id == 'household:0000' and d.tick == 3)
                        self.assertEqual(choice.belief_patch['model_supplier']['status'], 'rule_fallback')
                        self.assertEqual(choice.belief_patch['model_supplier']['attempt_id'], call['attempt_id'])
                        self.assertEqual((call['state'], call['error']['code'], call['accounted']), ('failed', 'refusal', 0))
                        replay = await runner.replay_run(path, destination=folder / 'refusal-replay')
                        self.assertEqual((replay['status'], replay['state_hash']), ('matched', result['state_hash']))
                    else:
                        with self.assertRaisesRegex(RunControlError, error):
                            await runner.resume_run(path)
                        world, store, committed = runner.open_run(path)
                        self.assertEqual((committed.tick, committed.state_hash), (2, paused['state_hash']))
                        self.assertEqual(store.archived_records(), [])
                        call, = world.model_runtime.store.records()
                        self.assertEqual(call['error']['code'], error)

    async def test_model_purchase_reflection_resume_and_independent_replay(self):
        for mode in ('L', 'M'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'run'
                cfg = {'ticks': 4, 'households': 4, 'supplier_cognition': {'mode': mode, 'ticks': [2, 3], 'social_weight_bps': 2500 if mode == 'M' else 0}}
                await runner.run_new(cfg, root, until=2)
                result = await runner.resume_run(root)
                world, store, _ = runner.open_run(root)
                rows = store.archived_records()
                self.assertCountEqual([r['request']['task'] for r in rows],
                                      ['cd_supplier', 'cd_reflection', 'cd_supplier'])
                reflection, = [r for r in rows if r['request']['task'] == 'cd_reflection']
                evidence = reflection['parsed']['evidence_ids']
                self.assertTrue(evidence)
                self.assertTrue(all(k.startswith('memory:') for k in evidence))
                actor = 'household:0000'
                at3, = [r for r in rows if r['request']['task'] == 'cd_supplier'
                        and r['request']['observation']['visible_until']['tick'] == 3]
                self.assertEqual(at3['request']['memory']['semantic'][0]['evidence_ids'], evidence)
                self.assertEqual(at3['request']['observation']['actor_id'], actor)
                decision = next(d for d in world.snapshot.decisions if d.actor_id == actor and d.tick == 3)
                self.assertEqual(decision.belief_patch['model_supplier']['attempt_id'], at3['attempt_id'])
                purchase = next(r for r in world.snapshot.receipts
                                if r.actor_id == actor and r.tick == 3 and r.command_id.endswith(':purchase'))
                self.assertEqual((purchase.status, purchase.outcome), ('accepted', 'purchased'))
                if mode == 'M':
                    self.assertEqual(at3['request']['observation']['delivered_messages'], [])
                else:
                    self.assertEqual(decision.belief_patch['closed_choice']['consumed_roots'], [])
                self.assertFalse(any(f['value'].get('kind') == 'closed_social_context'
                                     for f in at3['request']['observation']['market_view']))
                Path(store.identity.metadata['control_ledger']).rename(root.parent / 'original.sqlite')
                replay = await runner.replay_run(root, destination=root.parent / 'replay')
                self.assertEqual((replay['status'], replay['state_hash']), ('matched', result['state_hash']))

    async def test_both_modes_have_distinct_witnessed_peer_paths_and_parallel_equivalence(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            cfg = {'ticks': 3, 'households': 4}
            recorded = {}
            for mode in ('L', 'M'):
                for concurrency in (1, 4):
                    root = parent / f'{mode}-{concurrency}'
                    slots = {'mode': mode, 'household_indices': [0, 1], 'ticks': [2, 3],
                             'social_weight_bps': 2500 if mode == 'M' else 0}
                    result = await runner.run_new({**cfg, 'concurrency': concurrency,
                                                   'supplier_cognition': slots}, root, run_id=mode)
                    if concurrency == 4:
                        self.assertEqual(result['state_hash'], recorded[mode])
                        continue
                    recorded[mode] = result['state_hash']
                    world, store, _ = runner.open_run(root)
                    actor = 'household:0000'
                    bundle = next(b for b in world.snapshot.decisions if b.actor_id == actor and b.tick == 3)
                    trace = bundle.belief_patch['model_supplier']
                    numeric = bundle.belief_patch['closed_choice']
                    request = next(r['request'] for r in store.archived_records() if r['attempt_id'] == trace['attempt_id'])
                    basis = trace['model_scores_bps']
                    if mode == 'L':
                        self.assertTrue(request['observation']['delivered_messages'])
                        self.assertEqual(trace['applied_scores_bps'], basis)
                        self.assertEqual(numeric['consumed_roots'], [])
                    else:
                        self.assertEqual(request['observation']['delivered_messages'], [])
                        self.assertFalse(any(ep['source'] == 'message' for ep in request['memory']['episodic']))
                        self.assertTrue(numeric['consumed_roots'])
                        for seller in basis:
                            expected = (3 * basis[seller] + numeric['effective_scores_bps'][seller]) // 4
                            self.assertEqual(trace['applied_scores_bps'][seller], expected)
            self.assertNotEqual(recorded['L'], recorded['M'])

    async def test_reflection_and_purchase_roll_back_together_and_resume_without_new_model_calls(self):
        from unittest.mock import patch

        from civitas.cognition.providers import SyntheticProvider

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'recovery'
            cfg = {'ticks': 4, 'households': 4, 'supplier_cognition': {'mode': 'L', 'ticks': [2, 3]}}
            await runner.run_new(cfg, root, until=2)
            world, store, original = runner.open_run(root)
            before = world.policy.memory.export_state()

            def interrupt(point):
                if point == 'before_marker':
                    raise OSError('before reflection tick marker')

            store.fault_hook = interrupt
            with self.assertRaisesRegex(OSError, 'before reflection tick marker'):
                await world.step()
            self.assertEqual(world.policy.memory.export_state(), before)
            self.assertEqual(store.markers()[-1].commit_hash, original.commit_hash)
            self.assertEqual(len(store.archived_records()), 1)
            ledger = world.model_runtime.store
            self.assertEqual(len(ledger.records()), 3)
            with patch.object(SyntheticProvider, 'complete', side_effect=AssertionError('call beyond durable responses')):
                result = await runner.resume_run(root)
            world, store, _ = runner.open_run(root)
            self.assertEqual(len(world.policy.memory.for_actor('household:0000').semantic), 1)
            self.assertEqual(len(store.archived_records()), 3)
            self.assertEqual(len(ledger.records()), 3)
            replay = await runner.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual(replay['state_hash'], result['state_hash'])

    async def test_peer_intervention_preserves_m_prompt_with_fixed_private_and_market_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode in ('L', 'M'):
                inputs = []
                for neighbors in (1, 2):
                    cfg = {'ticks': 3, 'households': 4, 'network_neighbors': neighbors,
                           'supplier_cognition': {'mode': mode, 'ticks': [3],
                               'social_weight_bps': 2500 if mode == 'M' else 0, 'reflect': False}}
                    root = Path(tmp) / f'{mode}-{neighbors}'
                    await runner.run_new(cfg, root)
                    _, store, _ = runner.open_run(root)
                    request, = [r['request'] for r in store.archived_records()]
                    inputs.append(request)
                a, b = inputs
                if mode == 'M':
                    self.assertEqual(a, b)
                else:
                    for field in ('account_view', 'market_view', 'private_memory_refs'):
                        self.assertEqual(a['observation'][field], b['observation'][field])
                    first = {m['body']['root_claim_id'] for m in a['observation']['delivered_messages']}
                    second = {m['body']['root_claim_id'] for m in b['observation']['delivered_messages']}
                    self.assertEqual(len(second - first), 1)
                    self.assertTrue(first < second)
