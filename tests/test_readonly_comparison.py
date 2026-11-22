import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from civitas.application import economic_run
from civitas.cli_v4 import main


class ReadonlyComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_paired_exports_keep_the_requested_tick_and_scope(self):
        cfg = {'ticks': 3, 'households': 4, 'archive_observations': True,
               'labor_income_shock': {'tick': 2, 'household_indices': [0, 1], 'work_minutes': 240}}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            left, right = root/'funded', root/'control'
            await economic_run.run_new(cfg, left)
            await economic_run.run_new(cfg, right, until=2)
            sources = (left, right)
            before = {p: p.stat().st_mtime_ns for run in sources for p in run.rglob('*') if p.is_file()}

            def compare(name, args):
                output = root/name
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(['inspect-compare', str(left), str(right), '--output', str(output),
                                           '--ticks', '2', '3', *args]), 0)
                return output

            research = compare('research', ['--audience', 'researcher', '--baseline-tick', '2'])
            report = json.loads((research/'left-2.json').read_text())
            self.assertFalse(report['authority'])
            self.assertEqual(report['views'][0]['comparison_metrics']['wages_paid_minor'], 36)
            expected_marker = json.loads((left/'commits/00000002.json').read_text())
            self.assertEqual(report['views'][0]['cursor']['source_commit_hash'],
                             expected_marker['commit_hash'])
            self.assertFalse((research/'right-3.json').exists())
            self.assertIn('"uncommitted"', (research/'index.html').read_text())
            self.assertIn('"status":"matched"', (research/'index.html').read_text())

            agent = compare('agent', ['--audience', 'agent', '--actor', 'household:0000'])
            for page in agent.iterdir():
                body = page.read_text()
                self.assertNotIn('account:2:v1:household:0001', body)
                self.assertNotIn('"wages_paid_minor":', body)
            agent_report = json.loads((agent/'left-2.json').read_text())
            self.assertEqual(agent_report['actor_id'], 'household:0000')
            self.assertIn('labor_income_shock', (agent/'left-2.json').read_text())
            self.assertEqual(before, {p: p.stat().st_mtime_ns for run in sources for p in run.rglob('*') if p.is_file()})

    async def test_interrupted_publication_can_retry_without_touching_either_run(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            left, right, output = (root/name for name in ('left', 'right', 'comparison'))
            cfg = {'ticks': 1, 'households': 4}
            for run in (left, right):
                await economic_run.run_new(cfg, run)
            args = ['inspect-compare', str(left), str(right), '--output', str(output), '--ticks', '1',
                    '--audience', 'researcher']
            with patch('civitas.application.inspection_html.render', side_effect=OSError('interrupted')):
                with self.assertRaisesRegex(OSError, 'interrupted'):
                    main(args)
            self.assertFalse(output.exists())
            with redirect_stdout(StringIO()):
                self.assertEqual(main(args), 0)
            self.assertEqual({p.name for p in output.iterdir()}, {'index.html', 'left-1.json', 'right-1.json'})
            with self.assertRaises(FileExistsError):
                main(args)
