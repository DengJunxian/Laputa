"""Paired fee results from registered pilot stores, including failed assignments."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application import economic_run
from civitas.experiments import labor_factorial

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-partner-fee.json"


def short_spec(fee):
    raw = json.loads(EXAMPLE.read_bytes())
    raw["seeds"] = raw["seeds"][:1]
    raw["base_config"]["stopping_hazard_bps"] = 10000
    raw["base_config"]["cooperation"]["followup"]["partner_choice"]["fee_minor"] = fee
    return raw


class CooperationPartnerFeeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fee_comparison_retains_early_stops_and_bounds_a_failed_assignment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            low, high = root / "low", root / "high"
            await labor_factorial.run_pilot(low, short_spec(3))
            resume = economic_run.resume_run

            async def fail_ranked(path):
                if path == high / "seed-2037" / "ranked":
                    raise OSError("interrupted before the first commit")
                return await resume(path)

            with patch.object(economic_run, "resume_run", fail_ranked):
                await labor_factorial.run_pilot(high, short_spec(52))

            report = labor_factorial.compare_partner_fees(low, high)
            self.assertEqual(report["status"], "failed")
            static, ranked = (report["arms"][a] for a in ("static", "ranked"))
            self.assertEqual(static["all_assigned_high_minus_low"]["mean"], 0)
            self.assertEqual(static["roots"][0]["opportunity"], "no_offers")
            self.assertEqual(ranked["all_assigned_bounds"], [-52, 4])
            self.assertIsNone(ranked["all_assigned_high_minus_low"]["mean"])
            self.assertIsNone(ranked["roots"][0]["high"]["net_minor"])
            self.assertEqual(ranked["roots"][0]["high"]["status"], "failed")

            await labor_factorial.run_pilot(high)
            report = labor_factorial.compare_partner_fees(low, high)
            self.assertEqual(report["status"], "completed")
            ranked = report["arms"]["ranked"]
            self.assertEqual(ranked["all_assigned_bounds"], [0, 0])
            self.assertEqual(ranked["matched_offered_roots"], 0)

    async def test_fee_comparison_rejects_a_changed_continuation_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            low, high = root / "low", root / "high"
            raw = short_spec(52)
            raw["base_config"]["cooperation"]["followup"]["partner_noise_bps"] = 3500
            await labor_factorial.run_pilot(low, short_spec(3))
            await labor_factorial.run_pilot(high, raw)
            with self.assertRaisesRegex(ValueError, "differ only in the registered partner fee"):
                labor_factorial.compare_partner_fees(low, high)
