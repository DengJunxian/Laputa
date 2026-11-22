import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from hashlib import sha256
from io import BytesIO, StringIO
from unittest.mock import patch
from zipfile import ZipFile

from civitas.application import economic_run
from civitas.application.inspection import inspect_run, write_inspection
from civitas.application.observation_cursor import (PreparedObservationCursor, build_observation_index,
                                                   read_observation_index)
from civitas.application.research_cursor import build_research_index, research_page, research_summary
from civitas.cli_v4 import main
from civitas.contracts.base import content_hash
from civitas.experiments.closed_batch import run_closed_batch
from civitas.storage.commit_store import CommitStore, Segment, atomic_json, json_bytes
from civitas.storage.checkpoint import save
from civitas.storage.history import retain_history
from civitas.storage.parquet_sink import export_economic_tables


class InspectionProductsTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_pages_keep_the_original_commit_after_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, index = Path(tmp)/'run', Path(tmp)/'index'
            await economic_run.run_new({'ticks': 2, 'households': 67, 'archive_observations': True}, root, until=1)
            store = CommitStore(root)
            marker, snapshot = store.load(1)
            cursor = ['--tick', '1', '--commit', marker.commit_hash]
            with redirect_stdout(StringIO()):
                main(['research-index', str(root), *cursor, '--output', str(index)])

            def page(number):
                with redirect_stdout(StringIO()) as output:
                    main(['research-page', str(root), *cursor, '--index', str(index), '--page', str(number)])
                return json.loads(output.getvalue())

            first, second = page(1), page(2)
            self.assertEqual(research_summary(root, index, tick=1, commit=marker.commit_hash),
                             {key:value for key,value in first.items() if key!='accounts'})
            accounts = snapshot.data['economic_runtime']['balances']
            macro = snapshot.data['closed_economy']['last_macro']
            self.assertEqual(first['metrics'], {'cash_minor': sum(a['cash'] for a in accounts.values()),
                'produced_units': macro['produced_units'], 'consumed_units': macro['consumed_units'],
                'actor_count': len(snapshot.actors)})
            self.assertEqual(first['cursor']['state_hash'], marker.state_hash)
            self.assertEqual(first['provenance']['state_segment_sha256'], marker.segment.sha256)
            self.assertEqual(first['verification_scope'], 'indexed_research_cursor')
            self.assertEqual((first['accounts']['next_page'], second['accounts']['next_page']), (2, None))
            entries = first['accounts']['rows'] + second['accounts']['rows']
            self.assertEqual([row['account_id'] for row in entries], sorted(accounts))
            self.assertEqual({row['account_id']: row['cash_minor'] for row in entries},
                             {actor: row['cash'] for actor, row in accounts.items()})
            await economic_run.resume_run(root)
            self.assertEqual(page(2), second)
            future = store.markers()[-1]
            with self.assertRaisesRegex(ValueError, 'Commit does not match'):
                main(['research-page', str(root), '--tick', '1', '--commit', future.commit_hash,
                      '--index', str(index)])

    async def test_research_cursor_checks_source_state_and_the_selected_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, index = Path(tmp)/'run', Path(tmp)/'index'
            await economic_run.run_new({'ticks': 2, 'households': 4, 'archive_observations': True}, root)
            store = CommitStore(root)
            _, first, head = store.markers()
            cursor = {'tick': head.tick, 'commit': head.commit_hash}
            with self.assertRaises(FileNotFoundError):
                research_page(root, index, **cursor)
            build_research_index(root, index, **cursor)
            report = research_page(root, index, **cursor)
            with self.assertRaisesRegex(ValueError, 'No such research account page'):
                research_page(root, index, **cursor, page=2)
            page = index/'accounts-1.json'
            original = page.read_bytes()
            page.write_bytes(b'[]\n')
            with self.assertRaisesRegex(ValueError, 'page digest mismatch'):
                research_page(root, index, **cursor)
            page.write_bytes(original)
            source = store.segment_path(first.segment)
            original = source.read_bytes()
            source.unlink()
            with self.assertRaisesRegex(ValueError, 'Missing referenced segment'):
                research_page(root, index, **cursor)
            source.write_bytes(b'broken historical state')
            with self.assertRaisesRegex(ValueError, 'Segment byte hash mismatch'):
                research_page(root, index, **cursor)
            with self.assertRaisesRegex(ValueError, 'Segment byte hash mismatch'):
                build_research_index(root, index.with_name('broken'), **cursor, state_only=True)
            source.write_bytes(original)
            store.segment_path(first.observations).write_bytes(b'broken archived observation')
            self.assertEqual(research_page(root, index, **cursor), report)
            with self.assertRaisesRegex(ValueError, 'Segment byte hash mismatch'):
                store.audit_history()
            scoped = index.with_name('state-only')
            build_research_index(root, scoped, **cursor, state_only=True)
            self.assertEqual(research_page(root, scoped, **cursor), report)

    async def test_prepared_legacy_observation_rechecks_commit_actor_and_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, index = Path(tmp)/'run', Path(tmp)/'index'
            await economic_run.run_new({'ticks':2,'households':4,'archive_observations':True}, root)
            store = CommitStore(root)
            _, older, head = store.markers()
            cursor = {'tick':2,'commit':head.commit_hash}
            actors = ['household:0000','household:0001']
            build_observation_index(root, index, actors=actors, **cursor)
            actor = actors[0]
            prepared = PreparedObservationCursor(root, index, **cursor)
            actual = prepared.read(actor)
            self.assertEqual(prepared.read(actors[1])['actor_id'], actors[1])
            view, = actual['views']
            self.assertEqual(view.pop('verification_scope'), 'prepared_observation_cursor')
            self.assertEqual(actual, inspect_run(root, [2], audience='agent', actor=actor))
            with self.assertRaisesRegex(ValueError, 'Commit does not match'):
                read_observation_index(root, index, actor=actor, tick=2, commit=older.commit_hash)
            with self.assertRaises(FileNotFoundError):
                read_observation_index(root, index, actor='household:0002', **cursor)
            path = index/(sha256(actor.encode()).hexdigest()+'.json')
            original = path.read_bytes()
            path.write_bytes((index/(sha256(actors[1].encode()).hexdigest()+'.json')).read_bytes())
            with self.assertRaisesRegex(ValueError, 'does not match the committed decision'):
                prepared.read(actor)
            path.write_bytes(original)
            store.segment_path(head.observations).write_bytes(b'corrupt study fixture')
            with self.assertRaisesRegex(ValueError, 'Segment byte hash mismatch'):
                prepared.read(actor)

    async def test_indexed_observations_preserve_the_wire_observation_and_fixed_actor_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            original, indexed = path/'original', path/'indexed'
            config = {'ticks': 3, 'households': 4, 'archive_observations': True}
            await economic_run.run_new(config, original)
            await economic_run.run_new({**config, 'observation_archive_format': 'observe.v2'}, indexed, until=1)
            await economic_run.resume_run(indexed)
            legacy, packed = CommitStore(original), CommitStore(indexed)
            left, right = legacy.markers(), packed.markers()
            self.assertEqual([m.state_hash for m in left], [m.state_hash for m in right])
            for old, new in zip(left[1:], right[1:], strict=True):
                self.assertEqual(legacy.observation_rows(old), packed.observation_rows(new))
            self.assertEqual(right[-1].observations.encoding, 'observe+zip')
            with ZipFile(packed.segment_path(right[-1].observations)) as archive:
                self.assertEqual(json.loads(archive.read('index.json'))['block_size'], 32)
            actor = 'household:0000'
            expected = inspect_run(original, [1, 3], audience='agent', actor=actor)['views']
            actual = inspect_run(indexed, [1, 3], audience='agent', actor=actor)['views']
            for before, after in zip(expected, actual, strict=True):
                self.assertEqual(after['sections'], before['sections'])
                self.assertEqual(after['observation_id'], before['observation_id'])
                self.assertEqual(after['cursor']['state_hash'], before['cursor']['state_hash'])
                self.assertEqual(after['verification_scope'], 'indexed_observation_cursor')
            replay = await economic_run.replay_run(indexed, destination=path/'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 3))
            retained = retain_history(indexed, path/'retained', tick=2)
            retained_view = inspect_run(path/'retained', [2], audience='agent', actor=actor)['views'][0]
            self.assertEqual(retained_view['cursor']['source_commit_hash'], retained['source_commit_hash'])

            with self.assertRaisesRegex(ValueError, 'not registered'):
                inspect_run(indexed, [3], audience='agent', actor='household:9999')
            with self.assertRaisesRegex(ValueError, 'No committed'):
                inspect_run(indexed, [4], audience='agent', actor=actor)

    async def test_indexed_observations_distinguish_cursor_and_full_archive_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'world'
            await economic_run.run_new({'ticks': 3, 'households': 4, 'archive_observations': True,
                                        'observation_archive_format': 'observe.v2'}, root)
            store = CommitStore(root)
            _, first, target, _ = store.markers()
            actor = 'household:0000'
            expected = inspect_run(root, [2], audience='agent', actor=actor)
            store.segment_path(first.observations).write_bytes(b'invalid historical archive bytes')
            self.assertEqual(inspect_run(root, [2], audience='agent', actor=actor), expected)
            with self.assertRaisesRegex(ValueError, 'Segment byte hash'):
                store.audit_history()
            with self.assertRaisesRegex(ValueError, 'Segment byte hash'):
                inspect_run(root, [1], audience='agent', actor=actor)
            store.segment_path(target.segment).write_bytes(b'invalid selected decision delta')
            with self.assertRaisesRegex(ValueError, 'Segment byte hash'):
                inspect_run(root, [2], audience='agent', actor=actor)

    async def test_indexed_observation_fault_keeps_previous_cursor_until_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'world'
            config = {'ticks': 2, 'households': 4, 'archive_observations': True,
                      'observation_archive_format': 'observe.v2'}
            world, store = economic_run.create_run(config, root)
            await world.step()
            marker = store.markers()[-1]
            actor = 'household:0000'
            expected = inspect_run(root, [1], audience='agent', actor=actor)

            def fail(point):
                if point == 'before_marker':
                    raise OSError('indexed observation publication failed')

            store.fault_hook = fail
            with self.assertRaisesRegex(OSError, 'indexed observation publication failed'):
                await world.step()
            self.assertEqual(store.markers()[-1], marker)
            self.assertEqual(inspect_run(root, [1], audience='agent', actor=actor), expected)
            self.assertTrue(any(path.endswith('.observe.zip') for path in store.orphans()))
            resumed = await economic_run.resume_run(root)
            self.assertEqual(resumed['status'], 'completed')
            self.assertEqual(inspect_run(root, [1], audience='agent', actor=actor), expected)
            replay = await economic_run.replay_run(root, destination=Path(tmp)/'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 2))

    async def test_indexed_directory_cannot_substitute_an_observation_for_a_committed_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'world'
            config = {'ticks': 2, 'households': 4, 'archive_observations': True,
                      'observation_archive_format': 'observe.v2'}
            await economic_run.run_new(config, root, until=1)
            store = CommitStore(root)
            _, head = store.markers()
            actor = 'household:0000'
            stream = BytesIO()
            with ZipFile(store.segment_path(head.observations)) as original, ZipFile(stream, 'w') as replace:
                index = json.loads(original.read('index.json'))
                index['decisions'][actor] = index['decisions']['household:0001']
                for name in original.namelist():
                    replace.writestr(name, json_bytes(index) if name == 'index.json' else original.read(name))
            raw = stream.getvalue()
            reference = Segment(sha256=sha256(raw).hexdigest(), bytes=len(raw), encoding='observe+zip')
            store.segment_path(reference).write_bytes(raw)
            marker = head.model_dump(mode='json', exclude={'commit_hash'})
            marker['observations'] = reference.model_dump(mode='json')
            atomic_json(store.marker_path(head.tick), {**marker, 'commit_hash': content_hash(marker)})

            with self.assertRaisesRegex(ValueError, 'does not match the committed decisions'):
                inspect_run(root, [1], audience='agent', actor=actor)
            with self.assertRaisesRegex(ValueError, 'does not match the committed decision'):
                store.audit_history()

    async def test_observe_is_exact_and_single_agent_even_in_static_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            root = path/'world'
            await economic_run.run_new({'ticks':3, 'households':4, 'archive_observations':True}, root)
            store = CommitStore(root)
            marker, snapshot = store.load(2)
            actor = 'household:0000'
            actual = next(row for row in store.observation_rows(marker) if row.actor_id == actor)
            agent = inspect_run(root,[1,2],audience='agent',actor=actor)
            frame = agent['views'][-1]
            self.assertEqual(frame['observation_id'],actual.observation_id)
            self.assertEqual(frame['sections'][0]['rows'][0][2], actual.account_view[0].value)
            self.assertNotEqual(actual.account_view[0].value['cash_minor'],
                                snapshot.data['economic_runtime']['balances'][actor]['cash'])
            final = inspect_run(root,[3],audience='researcher')['views'][0]
            self.assertEqual(final['source_kind'],'verified_eight_tables')
            self.assertEqual(final['status'],'completed')
            self.assertEqual(final['metrics'][0][1],1040)
            before = {str(p.relative_to(root)):p.stat().st_mtime_ns for p in root.rglob('*') if p.is_file()}
            output = path/'agent.html'
            write_inspection(root,output,[1,2],audience='agent',actor=actor)
            self.assertNotIn('account:2:v1:household:0001',output.read_text())
            self.assertNotIn('verified_eight_tables',output.read_text())
            self.assertEqual({str(p.relative_to(root)):p.stat().st_mtime_ns for p in root.rglob('*') if p.is_file()},before)
            with self.assertRaisesRegex(ValueError,'not registered'):
                inspect_run(root,[2],audience='agent',actor='other')
            with self.assertRaisesRegex(ValueError,'outside'):
                write_inspection(root,root/'index.html',[2],audience='agent',actor=actor)

    async def test_archive_prefix_unrecorded_observe_and_retained_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            root = path/'world'
            await economic_run.run_new({'ticks':3,'households':4},root)
            with self.assertRaisesRegex(ValueError,'did not retain'):
                inspect_run(root,[2],audience='agent',actor='household:0000')
            retained = retain_history(root,path/'retained',tick=2)
            self.assertEqual((retained['tick'],len(CommitStore(path/'retained').verified_markers())),(2,3))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['audit-history', str(path/'retained'), '--checkpoint-tail']), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual((receipt['mode'], receipt['states_verified']), ('full_state_audit', 3))
            self.assertEqual(receipt['state_hash'], retained['state_hash'])
            self.assertEqual(CommitStore(root).markers()[-1].tick,3)
            replay = await economic_run.replay_run(path/'retained',destination=path/'replay')
            self.assertEqual((replay['compared_ticks'],replay['state_hash']),(2,retained['state_hash']))

    async def test_full_audit_detects_historical_damage_with_explicit_checkpoint_tail_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'world'
            await economic_run.run_new({'ticks':3,'households':4},root)
            store = CommitStore(root)
            marker, _ = store.load()
            self.assertEqual(save(store)['tick'],marker.tick)
            damaged = store.segment_path(store.markers()[1].segment)
            damaged.write_bytes(b'damaged')
            with self.assertRaisesRegex(ValueError,'Segment byte hash'):
                store.load()
            tail, _ = store.load(checkpoint_tail=True)
            self.assertEqual(tail.commit_hash,marker.commit_hash)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['audit-history', str(root), '--checkpoint-tail']), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual((receipt['mode'], receipt['checkpoint_tick'], receipt['states_verified']),
                             ('checkpoint_tail', 3, 1))

            store.identity = store.identity.model_copy(update={'participants': {'economic_runtime': 999}})
            with self.assertRaisesRegex(ValueError, 'Participant versions'):
                store.load(checkpoint_tail=True)

    async def test_stream_cursor_and_partial_failure_stays_unpublished(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            root = path/'world'
            await economic_run.run_new({'ticks':3,'households':4},root)
            marker, snapshot = CommitStore(root).load(2)
            with patch('civitas.storage.parquet_sink.json_bytes',side_effect=OSError('interrupted export')):
                with self.assertRaises(OSError):
                    export_economic_tables(root,path/'export',tick=2,format='jsonl')
            self.assertFalse((path/'export').exists())
            receipt = export_economic_tables(root,path/'export',tick=2,format='jsonl',batch_rows=3)
            self.assertEqual(receipt['source_commit_hash'],marker.commit_hash)
            actual = [json.loads(line) for line in (path/'export'/'receipts.jsonl').read_text().splitlines()]
            self.assertEqual([r['command_id'] for r in actual],[r.command_id for r in snapshot.receipts])
            self.assertTrue(all(r['tick'] <= 2 for r in actual))
            if importlib.util.find_spec('pyarrow') is not None:
                import pyarrow.dataset as ds
                import pyarrow.parquet as pq
                table = export_economic_tables(root,path/'arrow',tick=2,batch_rows=3)
                target = path/'arrow'/'receipts.parquet'
                self.assertEqual(table['state_hash'],receipt['state_hash'])
                self.assertGreater(pq.ParquetFile(target).num_row_groups,1)
                self.assertEqual(ds.dataset(target).to_table().to_pylist(),actual)


class BatchProductsTests(unittest.TestCase):
    def test_spawn_and_serial_business_hashes_equal_for_seeds_and_integer_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            config = {'ticks':2,'households':4,'initial_household_cash_minor':11,'cash_jitter_minor':0}
            a = run_closed_batch(config,[7,19],path/'serial')
            b = run_closed_batch(config,[7,19],path/'pool',workers=2)
            for name in a['rows']:
                self.assertEqual(a['rows'][name]['status'],'completed')
                self.assertEqual(a['rows'][name]['period_business_hashes'],b['rows'][name]['period_business_hashes'])
                self.assertEqual(a['rows'][name]['config_hash'],b['rows'][name]['config_hash'])
            self.assertEqual((a['decisions'],b['decisions']),(24,24))
