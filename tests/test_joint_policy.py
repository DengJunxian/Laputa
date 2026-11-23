import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from civitas.application import economic_run
from civitas.cognition.providers import ArchivedProvider, LiveProvider, ProviderConfig, SyntheticProvider
from civitas.cognition.runtime import RunControlError
from civitas.contracts.base import content_hash
from civitas.experiments.institutions import audit_institutions
from civitas.storage.commit_store import CommitStore, atomic_json, read_json


FIXTURE = Path(__file__).resolve().parents[1] / 'examples/closed-economy-security-feedback.json'
POLICY = read_json(FIXTURE.with_name('joint-policy-seats.json'))
BEHAVIOR, _, QUOTE = POLICY['seats']


class JointPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_joint_strategy_uses_the_existing_commit_and_model_receipts(self):
        raw = read_json(FIXTURE)
        mixed = {**raw, 'joint_policy': POLICY}
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            author = folder / 'synthetic'
            await economic_run.run_new(mixed, author)
            economic_run.freeze_offline_responses(author, folder / 'tape.json')
            archive = read_json(folder / 'tape.json')
            root = folder / 'fixed'
            cfg = {**raw, 'joint_policy': {**POLICY, 'source': 'archived_response'}}
            with (patch.object(SyntheticProvider, 'complete', side_effect=AssertionError('synthetic call')),
                  patch.object(LiveProvider, 'complete', side_effect=AssertionError('live API call'))):
                rule_result = await economic_run.run_new(raw, folder / 'rule')
                behavior_result = await economic_run.run_new(
                    {**raw, 'joint_policy': {'seats': [BEHAVIOR]}}, folder / 'behavior')
                first = await economic_run.run_new(cfg, root, until=2, response_archive=archive)
                world, store, marker = economic_run.open_run(root)
                memory = next(p for p in world.participants if p.participant_id == 'cognition_memory')
                before = memory.export_state()

                def interrupt(point):
                    if point == 'before_marker':
                        raise OSError('before mixed strategy commit')

                store.fault_hook = interrupt
                with self.assertRaisesRegex(OSError, 'before mixed strategy commit'):
                    await world.step()
                self.assertEqual(store.markers()[-1], marker)
                self.assertEqual(memory.export_state(), before)
                self.assertEqual(store.archived_records(), [])
                self.assertEqual(len(world.model_runtime.store.records()), 2)
                with patch.object(ArchivedProvider, 'complete', side_effect=AssertionError('repeated recorded attempt')):
                    await economic_run.resume_run(root, until=3)
                result = await economic_run.resume_run(root)
                replay = await economic_run.replay_run(root, destination=folder / 'verify')
            self.assertEqual({r['external_api_calls'] for r in (rule_result, behavior_result, first, result, replay)}, {0})
            self.assertEqual((replay['status'], replay['compared_ticks'], replay['state_hash']),
                             ('matched', 5, result['state_hash']))
            self.assertFalse(replay['applied_deltas_to_execution'])
            store = CommitStore(root)
            self.assertEqual(dict(store.identity.participants), {'economic_runtime': 3, 'cognition_memory': 1})
            calls = store.archived_records()
            self.assertEqual({(row['request']['observation']['actor_id'],
                               row['request']['observation']['visible_until']['tick']) for row in calls},
                             {('household:0001', 3), ('household:0001', 4), ('firm:00', 3)})
            self.assertEqual(len(calls), 3)
            self.assertEqual(store.identity.metadata['budget_limits']['max_attempts'], 3)
            self.assertTrue(all(row['state'] == 'success' and row['accounted'] == 0
                                and row['response']['origin'] == 'synthetic_test'
                                and row['request']['configuration']['source'] == 'archived_response' for row in calls))
            for row in calls:
                obs = row['request']['observation']
                self.assertFalse(obs['delivered_messages'])
                self.assertTrue(all(f['owner_id'] == obs['actor_id']
                                    for f in obs['account_view'] + obs['private_memory_refs']))
            rule = CommitStore(folder / 'rule')
            behavior = CommitStore(folder / 'behavior')
            self.assertEqual(rule.archived_records(), [])
            self.assertEqual(behavior.archived_records(), [])
            self.assertEqual(rule.load(0)[1].data['economic_runtime'], store.load(0)[1].data['economic_runtime'])
            for arm, ordered in ((rule, 2), (behavior, 1), (store, 1)):
                _, snap = arm.load(1)
                first_choice = next(d for d in snap.decisions if d.actor_id == 'household:0000')
                self.assertEqual(first_choice.belief_patch['closed_choice']['ordered_units'], ordered)
                self.assertEqual(audit_institutions(arm.load(5)[1])['economy']['security_final_units'], 4)

            final = store.load(5)[1]
            self.assertEqual(final.data['economic_runtime'], CommitStore(author).load(5)[1].data['economic_runtime'])
            for actor, tick, kind, outcome in (('household:0001', 3, 'hold', 'hold'),
                                               ('household:0001', 4, 'hold', 'hold'),
                                               ('firm:00', 3, 'quote', 'quote_updated')):
                decision = next(d for d in final.decisions if (d.actor_id, d.tick) == (actor, tick))
                intent, = [i for i in decision.economic_intents if i.kind == kind]
                receipt, = [r for r in final.receipts if r.command_id == 'command:' + intent.intent_id]
                self.assertEqual(receipt.outcome, outcome)
                self.assertEqual(decision.belief_patch['joint_policy']['action'], 'cautious')
                if kind == 'quote':
                    marker, _ = store.load(3)
                    obs = next(o for o in store.observation_rows(marker) if o.actor_id == actor)
                    price = next(f.value['unit_price_minor'] for f in obs.market_view if f.value['kind'] == 'firm_plan')
                    self.assertEqual(intent.unit_price_minor, price - 1)
                else:
                    self.assertFalse(any(i.kind == 'purchase' for i in decision.economic_intents))

            history = [next(row for row in store.load(tick)[1].data['cognition_memory']['actors']
                            if row['actor_id'] == 'household:0000') for tick in (2, 3, 4)]
            self.assertEqual([row['working']['observed_tick'] for row in history], [2, 3, 4])
            self.assertEqual([row['working']['reference_value_minor'] for row in history], [None, 100, 100])
            self.assertEqual(history[0]['working']['prior_purchase_minor'], 10)
            self.assertIn('memory:command:intent:1:household:0000:purchase', history[0]['working']['feedback_ids'])
            self.assertLess(history[0]['version'], history[2]['version'])
            self.assertTrue(any(ep['source'] == 'receipt' for ep in history[2]['episodic']))
            second = next(d for d in final.decisions if d.actor_id == 'household:0000' and d.tick == 2)
            self.assertEqual(second.belief_patch['joint_policy']['additional_reserve_minor'], 54)

    async def test_offline_choice_with_foreign_evidence_fails_closed(self):
        raw = read_json(FIXTURE)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            await economic_run.run_new({**raw, 'joint_policy': {'seats': [QUOTE]}}, folder / 'author')
            economic_run.freeze_offline_responses(folder / 'author', folder / 'tape.json')
            tape = read_json(folder / 'tape.json')
            response = tape['records'][0]['response']
            response['text'] = json.dumps({'action': 'cautious', 'evidence_id': 'account:3:v2:household:0000'})
            cfg = {**raw, 'joint_policy': {'seats': [QUOTE], 'source': 'archived_response'}}
            path = folder / 'invalid'
            paused = await economic_run.run_new(cfg, path, until=2, response_archive=tape)
            with self.assertRaisesRegex(RunControlError, 'invalid_response'):
                await economic_run.resume_run(path)
            world, store, marker = economic_run.open_run(path)
            self.assertEqual((marker.tick, marker.state_hash), (2, paused['state_hash']))
            attempt, = world.model_runtime.store.records()
            self.assertEqual((attempt['state'], attempt['error']['code'], attempt['accounted']),
                             ('failed', 'invalid_response', 0))
            self.assertEqual(store.archived_records(), [])
            tape['records'][0]['response']['text'] = '{}'
            atomic_json(path / 'response-archive.json', tape)
            with self.assertRaisesRegex(ValueError, 'Frozen response archive hash mismatch'):
                economic_run.open_run(path)

    def test_manifest_pins_unlisted_release_and_exact_price(self):
        entry = {'provider': 'openai', 'model': 'new-mock-model', 'revision': 'new-mock-model-v2',
                 'price': {'currency': 'USD', 'input_nano': 120, 'output_nano': 360},
                 'pricing_reference': 'approved-test-version', 'verified_at': '2026-11-23'}
        raw = {**entry, 'source': 'live', 'endpoint': 'https://api.openai.com/v1',
               'model_manifest': {'entry': entry, 'entry_hash': content_hash(entry)}}
        self.assertEqual(ProviderConfig.model_validate(raw).revision, 'new-mock-model-v2')
        with self.assertRaisesRegex(ValueError, 'pinned live model manifest'):
            ProviderConfig.model_validate({**raw, 'price': {**entry['price'], 'output_nano': 361}})
        with self.assertRaisesRegex(ValueError, 'manifest digest mismatch'):
            ProviderConfig.model_validate({**raw, 'model_manifest': {**raw['model_manifest'],
                                          'entry': {**entry, 'revision': 'model-unpinned'}}})

    def test_registry_rejects_ambiguous_or_unregistered_seats(self):
        raw = read_json(FIXTURE)
        for seats in ([BEHAVIOR, BEHAVIOR], [{**QUOTE, 'actor_id': 'bank:a'}],
                      [{**QUOTE, 'strategy': 'behavioral'}], [{**QUOTE, 'ticks': [6]}]):
            with self.subTest(seats=seats), self.assertRaisesRegex(ValueError, 'unique in-range seats'):
                economic_run.parse_config({**raw, 'joint_policy': {'seats': seats}})
