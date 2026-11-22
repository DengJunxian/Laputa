"""Known effects and durable four-arm C/D experiment acceptance."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.economic_run import replay_run
from civitas.experiments import registry
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot, summarize_interaction
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-labor-windows.json"


class LaborFactorialTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_offers_and_shared_prehistory_with_positive_interaction(self):
        with tempfile.TemporaryDirectory() as temp:
            report = await run_pilot(
                Path(temp) / "pilot",
                {
                    "base_config": json.loads(EXAMPLE.read_text()),
                    "seeds": [7],
                },
            )
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["expected_worlds"], 4)
        worlds = {row["arm"]: row for row in report["runs"]}
        self.assertEqual(
            {arm: row["worked"] for arm, row in worlds.items()}, {"t0r0": 1, "t0r1": 1, "t1r0": 0, "t1r1": 1}
        )
        self.assertEqual({row["visible_candidates"] for row in worlds.values()}, {2})
        self.assertEqual(len({row["opportunity_hash"] for row in worlds.values()}), 1)
        self.assertEqual(len({row["pretreatment_hash"] for row in worlds.values()}), 1)
        self.assertEqual(worlds["t1r0"]["reasons"], {"skipped_low_trust": 1, "not_attempted": 1})
        self.assertEqual(worlds["t1r1"]["second_window_fee_minor"], 3)
        self.assertEqual(report["interaction"]["mean"], 1)
        self.assertIsNone(report["interaction"]["monte_carlo_se"])

    def test_known_effects_covariance_and_failed_quartet_bounds(self):
        arms = ("t0r0", "t0r1", "t1r0", "t1r1")
        outcomes = ([1, 1, 0, 1], [0, 0, 1, 0], [0, 0, 1, 1], [1, 1, 0, None])
        rows = [
            {
                "seed": seed,
                "arm": arm,
                "worked": value,
                "status": "failed" if value is None else "completed",
                "opportunity_hash": "same-offers",
                "pretreatment_hash": "same-world",
            }
            for seed, quartet in enumerate(outcomes)
            for arm, value in zip(arms, quartet)
        ]
        result = summarize_interaction(rows, [0, 1, 2, 3])
        self.assertEqual([r["interaction"] for r in result["contrasts"]], [1, -1, 0])
        self.assertEqual(result["mean"], 0)
        self.assertAlmostEqual(result["monte_carlo_se"], 1 / 3**0.5)
        self.assertAlmostEqual(result["paired_simple_effect_covariance"], 1 / 3)
        self.assertEqual(result["all_assigned_bounds"], [0, 0.25])
        self.assertEqual(
            result["excluded_quartets"],
            [
                {"seed": 3, "reason": "incomplete_quartet", "arms": {"t1r1": "failed"}},
            ],
        )
        rows[3]["opportunity_hash"] = "one-different-quote"
        unmatched = summarize_interaction(rows, [0, 1, 2, 3])
        self.assertEqual(unmatched["excluded_quartets"][0], {"seed": 0, "reason": "opportunity_mismatch"})
        self.assertEqual(unmatched["all_assigned_bounds"], [0, 0.25])
        with self.assertRaisesRegex(ValueError, "uniquely"):
            summarize_interaction([*rows, rows[0]], [0, 1, 2, 3])

    async def test_failed_work_commit_stays_in_assignment_and_recovers_from_tick_nine(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            spec = {"base_config": json.loads(EXAMPLE.read_text()), "seeds": [7]}
            original_fault = CommitStore.fault

            def fail_before_ten(store, point):
                if point == "before_marker" and store.root.name == "t1r1" and store.marker_path(9).is_file():
                    raise OSError("second-window work commit interrupted")
                original_fault(store, point)

            with patch.object(CommitStore, "fault", fail_before_ten):
                failed = await run_pilot(root, spec)
            self.assertEqual(failed["expected_worlds"], 4)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["interaction"]["all_assigned_bounds"], [0, 1])
            unfinished = failed["runs"][-1]
            self.assertEqual(unfinished["status"], "failed")
            self.assertIsNone(unfinished["worked"])
            self.assertEqual(unfinished["periods"][-1]["tick"], 9)
            self.assertEqual(analyze_pilot(root), failed)

            recovered = await run_pilot(root)
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["interaction"]["all_assigned_bounds"], [1, 1])
            self.assertEqual(recovered["runs"][:3], failed["runs"][:3])
            self.assertEqual(recovered["runs"][-1]["periods"][:10], unfinished["periods"])
            self.assertEqual(recovered["runs"][-1]["second_window_fee_minor"], 3)
            self.assertEqual(analyze_pilot(root), recovered)
            attempts = [row["status"] for row in registry.history(root) if row["job"] == "seed-7/t1r1"]
            self.assertEqual(attempts, ["running", "failed", "running", "finished"])
            replay = await replay_run(root / "seed-7/t1r1", destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 11))

    async def test_completed_no_offer_and_rejection_keep_the_assigned_denominator(self):
        with tempfile.TemporaryDirectory() as temp:
            config = json.loads(EXAMPLE.read_text())
            config["disabled_actions"] = ["purchase"]
            empty = await run_pilot(Path(temp) / "empty", {"base_config": config, "seeds": [7]})
            self.assertEqual(empty["status"], "completed")
            self.assertEqual(empty["interaction"]["all_assigned_bounds"], [0, 0])
            self.assertEqual({r["visible_candidates"] for r in empty["runs"]}, {0})
            self.assertEqual({r["worked"] for r in empty["runs"]}, {0})

            config["disabled_actions"] = []
            config["labor_transfer"]["second_rehire"]["switch_fee_minor"] = 10000
            rejected = await run_pilot(Path(temp) / "costly", {"base_config": config, "seeds": [7]})
            self.assertEqual(rejected["status"], "completed")
            self.assertEqual(rejected["interaction"]["all_assigned_bounds"], [0, 0])
            costly = rejected["runs"][-1]
            self.assertEqual(
                (costly["visible_candidates"], costly["worked"], costly["second_window_fee_minor"]), (2, 0, 0)
            )
            self.assertEqual(costly["reasons"], {"rejected": 1, "skipped_low_trust": 1})
            self.assertEqual(costly["unemployed_households"], 1)
            self.assertIn(
                ["insufficient_labor_switch_fee_cash"], [row.get("reason_codes") for row in costly["choices"]]
            )
