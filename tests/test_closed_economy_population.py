import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from civitas.application.closed_economy import build_closed_economy
from civitas.population.fit import fit_households


def profile(index):
    return {'profile_id': f'profile:{index}', 'initial_cash_minor': 55 + index,
            'target_units': 2, 'basket_share_bps': 6000, 'cash_reserve_minor': 7}


class ClosedEconomyPopulationTests(unittest.TestCase):
    def test_population_rows_feed_existing_endowments_and_own_observations(self):
        rows = [{**profile(i), 'attributes': {'age_band': 'adult', 'region': str(i // 2)}} for i in range(4)]
        world = build_closed_economy({'households': 4, 'population_input': {
            'source_id': 'synthetic:case:1', 'data_origin': 'synthetic', 'household_profiles': rows}})
        population = world.snapshot.data['closed_economy']['population']
        self.assertEqual([p['profile_id'] for p in population.values()], [p['profile_id'] for p in rows])
        fact = next(f for f in world.observe('household:0000').market_view
                    if f.value.get('kind') == 'household_preferences')
        self.assertEqual(fact.value['attributes'], rows[0]['attributes'])
        self.assertEqual(world.scenario.economy.account_view('household:0003').cash_minor, 58)
        with self.assertRaisesRegex(ValueError, 'one profile per household'):
            build_closed_economy({'households': 4, 'population_input': {
                'source_id': 'synthetic:case:1', 'data_origin': 'synthetic', 'household_profiles': rows[:-1]}})

    @unittest.skipUnless(importlib.util.find_spec('ipfn'), 'requires population extra')
    def test_fit_support_controls_integerization_and_unfitted_joint_holdout(self):
        spec = {'source_id': 'synthetic:fit', 'data_origin': 'synthetic',
                'axes': {'age': ['young', 'older'], 'city': ['in', 'out']}, 'households': 7,
                'seed': [{'coordinates': v, 'weight': w, 'profile': profile(i)}
                         for i, (v, w) in enumerate([(['young', 'in'], 1),
                             (['young', 'out'], 1), (['older', 'out'], 1)])],
                'controls': [{'axes': ['age'], 'counts': [4, 3]}, {'axes': ['city'], 'counts': [2, 5]}],
                'holdout': [{'axes': ['age', 'city'], 'counts': [1, 3, 1, 2]}]}
        report = fit_households(spec)
        self.assertEqual(report['status'], 'fitted')
        self.assertEqual(report['structural_zero_cells'], 1)
        self.assertEqual(sum(report['integer_cells']), 7)
        self.assertEqual(report['integer_cells'], [2, 2, 0, 3])
        self.assertLess(report['controls'][0]['max_abs_weighted_residual'], 1e-5)
        self.assertGreater(report['holdout'][0]['max_abs_weighted_residual'], 0.99)
        self.assertEqual(len(report['population_input']['household_profiles']), 7)
        spec['controls'] = [{'axes': ['age', 'city'], 'counts': [1, 3, 1, 2]}]
        spec['holdout'] = []
        failure = fit_households(spec)
        self.assertEqual(failure['status'], 'infeasible_support')
        self.assertIsNone(failure['population_input'])

    @unittest.skipUnless(importlib.util.find_spec('ipfn'), 'requires population extra')
    def test_cli_reports_inconsistent_supported_margins_as_json(self):
        example = Path(__file__).resolve().parents[1] / 'examples/closed-economy-population-fit.json'
        spec = json.loads(example.read_bytes())
        spec['controls'][0]['counts'] = [1, 6]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'inconsistent.json'
            path.write_text(json.dumps(spec))
            result = subprocess.run([sys.executable, '-m', 'civitas.cli_v4', 'fit-population',
                                     '--spec', str(path)], text=True, capture_output=True, timeout=30)
        report = json.loads(result.stdout)
        self.assertEqual((result.returncode, report['status']), (1, 'residual_exceeds_tolerance'))
        self.assertEqual(report['unsupported_controls'], [])
        self.assertIsNone(report['population_input'])
