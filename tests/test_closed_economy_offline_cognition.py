"""The offline C/D decision uses the normal run, journal and replay ports."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application import economic_run as runner
from civitas.cognition.invocations import ModelInvocationStore
from civitas.cognition.providers import SyntheticProvider
from civitas.cognition.runtime import RunControlError


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/closed-economy-offline-cooperation.json'
FOCAL, PARTNER = 'household:0000', 'household:0001'


class ClosedEconomyOfflineCognitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_choice_settles_and_replays_from_its_archived_response(self):
        config = json.loads(EXAMPLE.read_bytes())
        config.update(archive_observations=True, observation_archive_format='observe.v2')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'model'
            result = await runner.run_new(config, root)
            world, store, _ = runner.open_run(root)
            self.assertEqual(result['external_api_calls'], 0)
            call, = store.archived_records()
            self.assertEqual(call['state'], 'success')
            self.assertEqual(call['accounted'], 0)
            self.assertEqual(call['request']['observation']['actor_id'], FOCAL)
            self.assertEqual(store.markers()[1].invocation_receipt_refs, [])
            self.assertEqual(store.markers()[2].invocation_receipt_refs, [call['attempt_id']])
            self.assertEqual(store.markers()[2].observations.encoding, 'observe+zip')
            self.assertFalse(runner.open_run(root, tick=1)[0].invocations)
            decision = next(d for d in world.snapshot.decisions if d.actor_id == FOCAL and d.tick == 2)
            self.assertEqual(decision.belief_patch['offline_cooperation']['attempt_id'], call['attempt_id'])
            settled, = [e for e in world.snapshot.data['economic_runtime']['journal']
                        if e['kind'] == 'cooperation_settlement']
            self.assertEqual(settled['tick'], 3)
            self.assertEqual(settled['payload']['contributions'], {FOCAL: 0, PARTNER: 4})
            self.assertEqual(settled['details']['cash_delta_minor'], {FOCAL: 3, PARTNER: -1, 'firm:00': -2})
            for actor in (FOCAL, PARTNER):
                own, = [f for f in world.observe(actor).private_memory_refs
                        if f.value.get('ledger_tx_id') == settled['tx_id']]
                self.assertEqual((own.owner_id, own.visible_at.tick), (actor, 4))
            for actor, last in ((FOCAL, runner.open_run(root, tick=3)[0]), ('household:0002', world)):
                self.assertFalse(any(f.value.get('ledger_tx_id') == settled['tx_id']
                                     for f in last.observe(actor).private_memory_refs))
            # Verification re-executes from the immutable tape without the control ledger.
            Path(store.identity.metadata['control_ledger']).rename(root.parent / 'saved.sqlite')
            with self.assertRaisesRegex(ValueError, 'Missing invocation ledger'):
                runner.open_run(root)
            replay = await runner.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 4))
            self.assertEqual(replay['state_hash'], result['state_hash'])
            self.assertFalse(replay['applied_deltas_to_execution'])

    async def test_invalid_response_aborts_economy_and_preserves_external_failure(self):
        config = json.loads(EXAMPLE.read_bytes())
        complete = SyntheticProvider.complete
        cases = {'foreign': f'account:2:v1:{PARTNER}', 'future': f'account:3:v2:{FOCAL}',
                 'malformed': None, 'state_write': None}
        with tempfile.TemporaryDirectory() as folder:
            for case, evidence in cases.items():
                with self.subTest(case=case):
                    root = Path(folder) / case
                    first = await runner.run_new(config, root, until=1)

                    async def invalid(provider, request):
                        response = await complete(provider, request)
                        if case == 'malformed':
                            return response.model_copy(update={'text': '{'})
                        body = json.loads(response.text)
                        if case == 'state_write':
                            body['belief_patch'] = {'cash_minor': 500}
                        else:
                            body['evidence_ids'].append(evidence)
                        return response.model_copy(update={'text': json.dumps(body)})

                    with patch.object(SyntheticProvider, 'complete', invalid):
                        with self.assertRaisesRegex(RunControlError, 'invalid_response'):
                            await runner.resume_run(root)
                    _, store, marker = runner.open_run(root)
                    self.assertEqual((marker.tick, marker.state_hash), (1, first['state_hash']))
                    self.assertEqual(store.archived_records(), [])
                    ledger = ModelInvocationStore(store.identity.metadata['control_ledger'], read_only=True)
                    row, = ledger.records()
                    self.assertEqual((row['state'], row['error']['code'], row['accounted']), ('failed', 'invalid_response', 0))
                    if evidence is not None:
                        self.assertIn('invisible evidence', row['error']['message'])

    async def test_marker_recovery_reuses_raw_response_and_settles_once(self):
        config = json.loads(EXAMPLE.read_bytes())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'recovery'
            world, store = runner.create_run(config, root)
            await world.step()

            def interrupt(point):
                if point == 'before_marker':
                    raise OSError('before model commit')

            store.fault_hook = interrupt
            with self.assertRaisesRegex(OSError, 'before model commit'):
                await world.step()
            self.assertEqual(store.markers()[-1].tick, 1)
            self.assertEqual(store.archived_records(), [])
            ledger = ModelInvocationStore(store.identity.metadata['control_ledger'], read_only=True)
            one, = ledger.records()
            self.assertEqual(one['state'], 'success')
            with patch.object(SyntheticProvider, 'complete', side_effect=AssertionError('unexpected new response')):
                await runner.resume_run(root, until=2)
            world, store, _ = runner.open_run(root)
            self.assertEqual(store.archived_records(), [one])

            def interrupt_settlement(point):
                if point == 'after_marker':
                    raise OSError('after settlement commit')

            store.fault_hook = interrupt_settlement
            with self.assertRaisesRegex(OSError, 'after settlement commit'):
                await world.step()
            self.assertEqual(store.markers()[-1].tick, 3)
            await runner.resume_run(root)
            world, store, _ = runner.open_run(root)
            self.assertEqual(store.markers()[-1].invocation_receipt_refs, [one['attempt_id']])
            self.assertEqual(ledger.records(), [one])
            settled, = [e for e in world.snapshot.data['economic_runtime']['journal']
                        if e['kind'] == 'cooperation_settlement']
            self.assertEqual(settled['payload']['contributions'], {FOCAL: 0, PARTNER: 4})
