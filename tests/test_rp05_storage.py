import sqlite3
import tempfile
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from io import StringIO
import json
from unittest.mock import patch

from civitas.application import economic_run
from civitas.cli_v4 import main
from civitas.contracts.base import content_hash
from civitas.contracts.observations import Observation
from civitas.contracts.state import EconomicWorldSnapshot
from civitas.storage import commit_store
from core.experiments import identity


class IncrementalIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_observe_v1_writes_one_wire_row_at_a_time_with_identical_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, store = economic_run.create_run(
                {'ticks': 1, 'households': 8, 'archive_observations': True}, Path(tmp)/'run')
            publish, dump = store._publish, Observation.model_dump
            alive, peak = 0, 0

            class WireRow(dict):
                def __init__(self, value):
                    super().__init__(value)
                    nonlocal alive, peak
                    alive += 1
                    peak = max(peak, alive)

                def __del__(self):
                    nonlocal alive
                    alive -= 1

            def measured_publish(*args, **kwargs):
                with patch.object(Observation, 'model_dump',
                                  lambda row, **options: WireRow(dump(row, **options))):
                    return publish(*args, **kwargs)

            with patch.object(store, '_publish', measured_publish):
                await world.step()
            self.assertEqual(peak, 1)
            marker = store.markers()[-1]
            rows = [row.model_dump(mode='json') for row in store.observation_rows(marker)]
            self.assertEqual(store._segment_bytes(marker.observations),
                             commit_store.stored_json_bytes(rows, compressed=True))

    async def test_v4_barriers_retain_the_same_verified_history_and_wire_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, store = economic_run.create_run(
                {'ticks': 3, 'households': 8, 'archive_observations': True}, Path(tmp)/'run')
            await world.run(2)
            prior = world.snapshot
            observe = world.observer.observe_batch
            effect = world.observer.prepare_commit
            feedback = []

            def observe_batch(snapshot, project):
                self.assertIs(snapshot.receipts, prior.receipts)
                self.assertIs(snapshot.memories, prior.memories)
                return observe(snapshot, project)

            def prepare_commit(snapshot, observations):
                feedback.append(snapshot)
                return effect(snapshot, observations)

            with (patch.object(world.observer, 'observe_batch', observe_batch),
                  patch.object(world.observer, 'prepare_commit', prepare_commit)):
                await world.step()
            after = world.snapshot
            wire = after.model_dump(mode='json')
            for field in ('decisions', 'receipts', 'memories'):
                self.assertIs(getattr(feedback[0], field), getattr(after, field))
                self.assertIs(type(wire[field]), list)
            self.assertEqual(after.state_hash, content_hash(wire))
            self.assertEqual(store.load()[1].model_dump(mode='json'), wire)

            raw = prior.model_dump(mode='json')
            edited = after.model_copy(update={'receipts': [
                after.receipts[0].model_copy(update={'actor_id': 'edited:actor'}), *after.receipts[1:]]})
            delta = commit_store.StateDelta(base_hash=prior.state_hash, state_hash=edited.state_hash,
                changes=commit_store._snapshot_changes(prior, edited))
            self.assertEqual(commit_store.apply_delta(raw, delta), edited.model_dump(mode='json'))
            self.assertNotEqual(after.state_hash, edited.state_hash)
            with self.assertRaisesRegex(ValueError, 'Duplicate command_id'):
                after.model_copy(update={'receipts': [*after.receipts, after.receipts[0]]})

    async def test_observe_batch_projects_evidence_history_and_recovers_mid_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {'ticks': 3, 'households': 8, 'archive_observations': True}
            world, store = economic_run.create_run(settings, Path(tmp)/'run')
            reference, reference_store = economic_run.create_run(settings, Path(tmp)/'reference')
            await world.run(2)
            await reference.run(3)
            committed = world.snapshot
            book, = world.participants
            observe = world.observer.observe
            completed = []

            def interrupted(actor, snapshot, candidates):
                self.assertEqual(snapshot.decisions, [])
                self.assertEqual(snapshot.used_ids, [])
                self.assertEqual(snapshot.receipts, committed.receipts)
                self.assertEqual(snapshot.messages, committed.messages)
                self.assertEqual(snapshot.memories, committed.memories)
                if completed:
                    raise OSError('interrupted during observation batch')
                result = observe(actor, snapshot, candidates)
                completed.append(result)
                return result

            with patch.object(world.observer, 'observe', interrupted):
                with self.assertRaisesRegex(OSError, 'interrupted during observation batch'):
                    await world.step()
            self.assertEqual(len(completed), 1)
            self.assertEqual(world.failures[-1]['phase'], 'OBSERVE')
            self.assertIs(world.snapshot, committed)
            self.assertEqual(book.export_state(), committed.model_dump(mode='json')['data']['economic_runtime'])
            self.assertEqual(store.markers()[-1].tick, 2)

            await economic_run.resume_run(store.root, until=3)
            actual, expected = store.markers()[-1], reference_store.markers()[-1]
            self.assertEqual((actual.state_hash, actual.segment, actual.observations),
                             (expected.state_hash, expected.segment, expected.observations))
            self.assertTrue(any(obs.delivered_messages for obs in store.observation_rows(actual)))

    async def test_working_dump_shares_sealed_economic_history_and_copies_current_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, _ = economic_run.create_run({'ticks': 1, 'households': 4}, Path(tmp)/'run')
            await world.step()
            committed = world.snapshot
            original = committed.data['economic_runtime']
            wire = committed.model_dump(mode='json')
            working = committed.working_dump(mode='json')
            economy = working['data']['economic_runtime']
            for field in ('journal', 'commands', 'receipts'):
                self.assertIs(economy[field], original[field])
                with self.assertRaises(TypeError):
                    economy[field][0]['tick'] = 0
            self.assertEqual(EconomicWorldSnapshot.model_validate(working).model_dump(mode='json'), wire)
            actor = next(iter(economy['balances']))
            economy['balances'][actor]['cash'] += 1
            self.assertEqual(committed.model_dump(mode='json'), wire)

            restored = EconomicWorldSnapshot.model_validate(wire)
            unsealed = restored.working_dump(mode='json')['data']['economic_runtime']
            self.assertEqual(unsealed, wire['data']['economic_runtime'])
            self.assertIsNot(unsealed['journal'], restored.data['economic_runtime']['journal'])

    async def test_warm_tick_uses_committed_economic_backup_without_a_full_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, store = economic_run.create_run({'ticks': 2, 'households': 4}, Path(tmp)/'run')
            await world.step()
            committed = world.snapshot
            book, = world.participants

            def fail(point):
                if point == 'before_marker':
                    raise OSError('interrupted before marker')

            store.fault_hook = fail
            with patch.object(book, 'export_state', wraps=book.export_state) as export:
                with self.assertRaisesRegex(OSError, 'interrupted before marker'):
                    await world.step()
                export.assert_not_called()
            self.assertIs(world.snapshot, committed)
            self.assertEqual(book.export_state(), committed.model_dump(mode='json')['data']['economic_runtime'])
            self.assertEqual(store.markers()[-1].tick, 1)
            store.fault_hook = None
            self.assertEqual((await world.step()).tick, 2)

    async def test_warm_backup_rejects_out_of_tick_state_changes(self):
        for change in ('clock', 'journal'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                world, store = economic_run.create_run({'ticks': 2, 'households': 4}, Path(tmp)/'run')
                await world.step()
                committed = world.snapshot
                book, = world.participants
                if change == 'clock':
                    book.begin_tick(2, 1)
                else:
                    entry = book.journal[-1]
                    book._journal[entry.event_id] = entry.model_copy(update={
                        'details': {**entry.details, 'changed_outside_tick': True},
                    })

                with self.assertRaisesRegex(RuntimeError, 'Participant changed outside a tick transaction'):
                    await world.step()
                self.assertIs(world.snapshot, committed)
                self.assertEqual(store.markers()[-1].tick, 1)

    async def test_committed_social_trace_carries_history_without_encoding_old_rows_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, store = economic_run.create_run({'ticks': 2, 'households': 4}, Path(tmp)/'run')
            await world.step()
            old = world.snapshot
            old_trace = old.data['closed_social']['trace']
            tagged = identity._tagged
            repeated = []

            def measured(value, active):
                if isinstance(value, dict) and value.get('kind') == 'decision' and value.get('tick') == 1:
                    repeated.append(value['decision_id'])
                return tagged(value, active)

            with patch.object(identity, '_tagged', measured):
                prepared = await world.step()
            trace = prepared.snapshot.data['closed_social']['trace']
            self.assertEqual(repeated, [])
            self.assertIs(trace[0], old_trace[0])
            wire = prepared.snapshot.model_dump(mode='json')
            self.assertEqual(prepared.state_hash, content_hash(wire))
            marker, recovered = store.load()
            self.assertEqual(recovered.model_dump(mode='json'), wire)
            full_delta = commit_store.StateDelta(base_hash=old.state_hash, state_hash=prepared.state_hash,
                changes=commit_store.differences(old.model_dump(mode='json'), wire))
            self.assertEqual(store.segment(marker), full_delta.model_dump(mode='json'))

    def test_rewritten_and_rolled_social_trace_uses_the_actual_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, _ = economic_run.create_run({'ticks': 1, 'households': 4}, Path(tmp)/'run')

            def snapshot(rows):
                raw = world.snapshot.model_dump(mode='json')
                raw['data']['closed_social']['trace'] = rows
                value = EconomicWorldSnapshot.model_validate(raw)
                return EconomicWorldSnapshot.model_validate(value.working_dump(mode='json'))

            old_rows = [{'kind': 'exposure', 'ordinal': n} for n in range(50_000)]
            old = snapshot(old_rows)
            old_wire = old.model_dump(mode='json')
            for rows in ([{'kind': 'decision', 'ordinal': -1}, *old_rows[1:]],
                         [*old_rows[1:], {'kind': 'exposure', 'ordinal': 50_000}]):
                new = snapshot(rows)
                new_wire = new.model_dump(mode='json')
                self.assertEqual(new_wire['data']['closed_social']['trace'], rows)
                self.assertNotEqual(new.state_hash, old.state_hash)
                self.assertEqual(new.state_hash, content_hash(new_wire))
                delta = commit_store.StateDelta(base_hash=old.state_hash, state_hash=new.state_hash,
                    changes=commit_store._snapshot_changes(old, new))
                self.assertEqual(commit_store.apply_delta(old_wire, delta), new_wire)
            self.assertEqual(old.data['closed_social']['trace'][0]['ordinal'], 0)

    async def test_publication_writes_the_same_delta_without_dumping_retained_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, store = economic_run.create_run({'ticks': 2, 'households': 4}, Path(tmp)/'run')
            await world.step()
            before = world.snapshot
            publish = store.commit
            dump = EconomicWorldSnapshot.model_dump
            prefixes = []
            repeated = []
            tagged = identity._tagged
            old_execution = before.data['economic_runtime']
            old_ids = {row['command_id'] for row in old_execution['commands']}

            def count_history(snapshot, *args, **kwargs):
                value = dump(snapshot, *args, **kwargs)
                economic = value.get('data', {}).get('economic_runtime', {})
                if 'decisions' in value or {'journal', 'commands', 'receipts'} & economic.keys():
                    prefixes.append(snapshot.tick)
                return value

            def count_encodes(value, active):
                if isinstance(value, dict) and value.get('command_id') in old_ids:
                    repeated.append(value['command_id'])
                return tagged(value, active)

            def counted_publish(*args, **kwargs):
                with patch.object(EconomicWorldSnapshot, 'model_dump', count_history):
                    return publish(*args, **kwargs)

            with (patch.object(store, 'commit', counted_publish),
                  patch.object(identity, '_tagged', count_encodes)):
                prepared = await world.step()
            for field in ('commands', 'receipts'):
                self.assertIs(prepared.snapshot.data['economic_runtime'][field][0], old_execution[field][0])
            recorded, recovered = store.load()
            expected = commit_store.StateDelta(base_hash=before.state_hash, state_hash=prepared.state_hash,
                changes=commit_store.differences(dump(before, mode='json'), dump(prepared.snapshot, mode='json')))
            self.assertEqual(store.segment(recorded), expected.model_dump(mode='json'))
            self.assertEqual(recovered.state_hash, prepared.state_hash)
            self.assertEqual(prepared.state_hash, content_hash(dump(prepared.snapshot, mode='json')))
            self.assertEqual(prefixes, [])
            self.assertEqual(repeated, [])

    async def test_checkpointless_recovery_reuses_canonical_journal_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, store = economic_run.create_run({'ticks': 3, 'households': 4}, Path(tmp)/'run')
            await world.run(3)
            whole_state_encodes = []

            def counted(value):
                if isinstance(value, dict) and value.get('schema_version') == 4 and 'actors' in value:
                    whole_state_encodes.append(value['tick'])
                return content_hash(value)

            with patch.object(commit_store, 'content_hash', counted):
                marker, snapshot = store.load()
            self.assertEqual(content_hash(snapshot), world.snapshot.state_hash)
            self.assertEqual(marker.state_hash, world.snapshot.state_hash)
            self.assertEqual(whole_state_encodes, [])
            first = store.markers()[1]
            path = store.segment_path(first.segment)
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, 'Segment byte hash mismatch'):
                store.load()

    async def test_recovery_rechecks_journal_rows_edited_by_a_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, store = economic_run.create_run({'ticks': 3, 'households': 4}, Path(tmp)/'run')
            await world.run(3)
            marker, snapshot = store.load()
            original = store.segment(marker)
            data = snapshot.model_dump(mode='json')
            data['data']['economic_runtime']['journal'][1]['details']['audit_example'] = 'changed prefix'

            def publish(state_hash):
                rewritten = {**original, 'state_hash': state_hash, 'changes': [*original['changes'],
                    {'op': 'set', 'path': ['data', 'economic_runtime', 'journal', 1, 'details', 'audit_example'],
                     'value': 'changed prefix', 'offset': None}]}
                body = marker.model_dump(mode='json', exclude={'commit_hash'})
                body.update(state_hash=state_hash, segment=store.write_segment(rewritten).model_dump(mode='json'))
                commit_store.atomic_json(store.marker_path(marker.tick), {**body, 'commit_hash': content_hash(body)})

            publish(marker.state_hash)
            with self.assertRaisesRegex(ValueError, 'Delta result hash mismatch'):
                store.load()
            publish(content_hash(data))
            _, changed = store.load()
            self.assertEqual(changed.model_dump(mode='json'), data)

    async def test_lag_damage_delete_and_explicit_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            world, store = economic_run.create_run({'ticks': 3, 'households': 4}, root)
            await world.step()
            self.assertEqual(store.update_index()['appended'], 0)

            def fail(point):
                if point == 'before_index':
                    raise OSError('before index')

            store.fault_hook = fail
            with self.assertRaises(OSError):
                await world.step()
            store.fault_hook = None
            recovered = store.update_index()
            self.assertEqual((recovered['mode'], recovered['appended'], recovered['tick']), ('incremental', 1, 2))
            with sqlite3.connect(root/'index.sqlite') as db:
                db.execute("UPDATE commits SET state_hash='bad' WHERE tick=2")
            self.assertEqual(store.update_index()['mode'], 'rebuilt')
            (root/'index.sqlite').write_bytes(b'broken sqlite')
            self.assertEqual(store.repair_index()['tick'], 2)
            (root/'index.sqlite').unlink()
            self.assertEqual(store.update_index()['appended'], 3)
            with sqlite3.connect(root/'index.sqlite') as db:
                indexed = db.execute('SELECT tick,commit_hash,state_hash FROM commits ORDER BY tick').fetchall()
            self.assertEqual(indexed, [(r.tick, r.commit_hash, r.state_hash) for r in store.verified_markers()])

    async def test_reindex_cli_repairs_a_damaged_prefix_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'run'
            await economic_run.run_new({'ticks': 3, 'households': 4}, root)
            with sqlite3.connect(root/'index.sqlite') as db:
                db.execute("UPDATE commits SET state_hash='bad' WHERE tick=1")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['reindex', str(root)]), 0)
            self.assertEqual(json.loads(output.getvalue())['mode'], 'rebuilt')
            with sqlite3.connect(root/'index.sqlite') as db:
                row = db.execute('SELECT state_hash FROM commits WHERE tick=1').fetchone()
            self.assertNotEqual(row[0], 'bad')
