"""Actual v4 run control and projection boundaries, not protocol-only fixtures."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'reproduction/runtime/source'
sys.path[:0] = [str(SOURCE/'src'), str(SOURCE)]

from civitas.application import economic_run as runner
from civitas.contracts.base import content_hash
from civitas.contracts.time import Phase
from civitas.experiments import registry
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig


class EconomicRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = ClosedEconomyConfig(ticks=4, households=4)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_resume_and_independent_replay_match_uninterrupted(self):
        interrupted = self.root/'interrupted'
        first = await runner.run_new(self.config, interrupted, until=2)
        self.assertEqual(first['status'], 'checkpointed')
        resumed = await runner.resume_run(interrupted)
        direct = await runner.run_new(self.config, self.root/'direct')
        self.assertEqual(resumed['state_hash'], direct['state_hash'])
        self.assertEqual(resumed['tick'], 4)
        replay = await runner.replay_run(interrupted, destination=self.root/'replay')
        self.assertEqual(replay['status'], 'matched')
        self.assertEqual(replay['compared_ticks'], 4)
        self.assertFalse(replay['applied_deltas_to_execution'])
        self.assertEqual(replay['state_hash'], resumed['state_hash'])
        self.assertEqual(replay['external_api_calls'], 0)
        world, store, _ = runner.open_run(interrupted)
        self.assertEqual(store.identity.config, self.config.model_dump(mode='json'))
        self.assertEqual(content_hash(world.snapshot), resumed['state_hash'])

    async def test_fixed_cursor_projection_is_non_authoritative_and_tamper_evident(self):
        root = self.root/'run'
        result = await runner.run_new(self.config, root)
        output = self.root/'historical'
        projection = runner.export_projection(root, destination=output, tick=2)
        cursor = projection['cursor']
        self.assertEqual(cursor['tick'], 2)
        self.assertFalse(cursor['authority'])
        for filename in projection['files']:
            record = json.loads((output/filename).read_text())
            self.assertEqual(record['cursor'], cursor)
        (output/'metrics.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'modified'):
            runner.export_projection(root, destination=output, tick=2)
        world, _, _ = runner.open_run(root)
        self.assertEqual(content_hash(world.snapshot), result['state_hash'])
        rebuilt = runner.export_projection(root, destination=self.root/'rebuilt', tick=2)
        self.assertEqual(rebuilt['files'], projection['files'])

    async def test_resume_rejects_source_and_dependency_identity_drift(self):
        root = self.root/'run'
        await runner.run_new(self.config, root, until=1)
        original = runner.current_implementation()
        for key in ('source_hash', 'dependency_hash'):
            changed = copy.deepcopy(original)
            changed[key] = '0'*64
            with patch.object(runner, 'current_implementation', return_value=changed):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    await runner.resume_run(root)
        _, _, marker = runner.open_run(root)
        self.assertEqual(marker.tick, 1)

    async def test_failure_preserves_primary_error_and_safe_head(self):
        root = self.root/'run'
        world, store = runner.create_run(self.config, root)
        initial = content_hash(world.snapshot)
        def fail(phase, snapshot):
            if phase == Phase.FEEDBACK:
                raise RuntimeError('primary feedback failure')
        world.phase_hook = fail
        with patch.object(runner, 'export_projection', side_effect=OSError('projection unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'primary feedback failure'):
                await runner.advance(world, store, until=1)
        self.assertEqual(content_hash(world.snapshot), initial)
        _, _, marker = runner.open_run(root)
        self.assertEqual(marker.tick, 0)
        last = registry.history(root)[-1]
        self.assertEqual(last['status'], 'failed')
        self.assertIn('primary feedback failure', last['error'])
        self.assertIn('projection unavailable', last['finalization_error'])
        # The frozen run is still executable after the external fault disappears.
        resumed = await runner.resume_run(root)
        self.assertEqual(resumed['status'], 'completed')

    async def test_active_writer_blocks_resume_and_replay(self):
        root = self.root/'run'
        await runner.run_new(self.config, root, until=1)
        with registry.run_lock(root):
            with self.assertRaises(BlockingIOError):
                await runner.resume_run(root)
            with self.assertRaises(BlockingIOError):
                await runner.replay_run(root, destination=self.root/'replay')
        self.assertFalse((self.root/'replay').exists())

    async def test_partial_projection_failure_does_not_poison_resume(self):
        root = self.root/'run'
        world, store = runner.create_run(self.config, root)
        original = runner.atomic_json
        def fail_second_table(path, value):
            if Path(path).name == 'economy.json':
                raise OSError('injected partial projection failure')
            return original(path, value)
        with patch.object(runner, 'atomic_json', side_effect=fail_second_table):
            with self.assertRaisesRegex(OSError, 'partial projection failure'):
                await runner.advance(world, store)
        marker = store.verified_markers()[-1]
        self.assertEqual(marker.tick, self.config.ticks)
        self.assertFalse((root/'projections'/marker.commit_hash).exists())
        # No extra economic tick is needed: repair only the failed projection.
        result = await runner.resume_run(root)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['from_tick'], self.config.ticks)
        self.assertEqual(result['state_hash'], marker.state_hash)
        self.assertTrue((root/'projections'/marker.commit_hash/'manifest.json').is_file())


if __name__ == '__main__':
    unittest.main()
