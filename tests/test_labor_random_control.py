"""Constrained random labor control through the durable pilot interface."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.economic_run import replay_run
from civitas.experiments import registry
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot, summarize_switch_control
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-labor-windows.json"


def spec(seed, **changes):
    config = json.loads(EXAMPLE.read_text())
    config.update(changes)
    return {"schema_version": 2, "base_config": config, "seeds": [seed]}


class LaborRandomControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_random_draw_can_renew_low_score_incumbent_with_same_quotes(self):
        with tempfile.TemporaryDirectory() as temp:
            report = await run_pilot(Path(temp) / "pilot", spec(43))
        self.assertEqual(report["status"], "completed")
        selective, random = report["runs"]
        self.assertEqual((selective["arm"], random["arm"]), ("selective", "random"))
        self.assertEqual(selective["pretreatment_hash"], random["pretreatment_hash"])
        self.assertEqual(selective["opportunity_hash"], random["opportunity_hash"])
        self.assertEqual((selective["work"]["employer_id"], random["work"]["employer_id"]), ("firm:00", "firm:01"))
        self.assertEqual((selective["switch_worked"], random["switch_worked"]), (1, 0))
        self.assertEqual(random["random_draw"]["address"]["root_seed"], 43)
        self.assertEqual(random["random_draw"]["address"]["tick"], 9)
        self.assertEqual(random["random_draw"]["u64_hex"], "71b173a0c07e7d41")
        accepted = next(row for row in random["choices"] if row["status"] == "accepted")
        self.assertEqual(random["random_draw"]["selected_offer_id"], accepted["offer"]["offer_id"])
        self.assertEqual(report["comparison"]["mean"], 0)

    async def test_empty_and_unaffordable_pools_preserve_assignment_without_redrawing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty = await run_pilot(root / "empty", spec(7, disabled_actions=["purchase"]))
            self.assertEqual(empty["status"], "completed")
            self.assertEqual(empty["comparison"]["all_assigned_bounds"], [0, 0])
            for row in empty["runs"]:
                self.assertIsNone(row["random_draw"])
                self.assertEqual((row["visible_candidates"], row["worked"], row["switch_attempted"]), (0, 0, 0))
            self.assertEqual(empty["arms"]["random"]["allocated_switch_slots"], 1)

            costly = spec(7)
            costly["base_config"]["labor_transfer"]["second_rehire"]["switch_fee_minor"] = 10000
            failed_choice = await run_pilot(root / "costly", costly)
            self.assertEqual(failed_choice["status"], "completed")
            row = failed_choice["runs"][1]
            self.assertEqual(row["reasons"], {"rejected": 1, "not_selected": 1})
            self.assertEqual((row["switch_attempted"], row["switch_accepted"], row["switch_worked"]), (1, 0, 0))
            self.assertEqual((row["worked"], row["second_window_fee_minor"], row["unemployed_households"]), (0, 0, 1))
            self.assertIn(["insufficient_labor_switch_fee_cash"], [r.get("reason_codes") for r in row["choices"]])

    async def test_failed_random_switch_preserves_draw_prefix_and_replays_after_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            original_fault = CommitStore.fault

            def fail_before_work(store, point):
                if point == "before_marker" and store.root.name == "random" and store.marker_path(9).is_file():
                    raise OSError("interrupted randomized work commit")
                original_fault(store, point)

            with patch.object(CommitStore, "fault", fail_before_work):
                failed = await run_pilot(root, spec(7))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["comparison"]["all_assigned_bounds"], [0, 1])
            self.assertEqual(failed["comparison"]["excluded_pairs"][0]["arms"], {"random": "failed"})
            self.assertEqual(failed["runs"][1]["periods"][-1]["tick"], 9)
            self.assertIsNone(failed["runs"][1]["worked"])
            draw = failed["runs"][1]["random_draw"]
            self.assertEqual(draw["address"]["tick"], 9)

            recovered = await run_pilot(root)
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(analyze_pilot(root), recovered)
            self.assertEqual(recovered["runs"][0], failed["runs"][0])
            self.assertEqual(recovered["runs"][1]["periods"][:10], failed["runs"][1]["periods"])
            self.assertEqual(recovered["runs"][1]["second_window_fee_minor"], 3)
            self.assertEqual(recovered["runs"][1]["random_draw"], draw)
            self.assertEqual(recovered["comparison"]["all_assigned_bounds"], [0, 0])
            attempts = [r["status"] for r in registry.history(root) if r["job"] == "seed-7/random"]
            self.assertEqual(attempts, ["running", "failed", "running", "finished"])
            replay = await replay_run(root / "seed-7/random", destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 11))

    def test_paired_contrast_keeps_failed_arm_and_does_not_filter_on_switch(self):
        rows = [
            {
                "seed": seed,
                "arm": arm,
                "worked": worked,
                "status": "failed" if worked is None else "completed",
                "pretreatment_hash": "same",
                "opportunity_hash": "equal",
                "switch_worked": 0,
            }
            for seed, outcomes in enumerate(((1, 0), (1, 1), (0, None)))
            for arm, worked in zip(("selective", "random"), outcomes)
        ]
        result = summarize_switch_control(rows, [0, 1, 2])
        self.assertEqual([row["difference"] for row in result["contrasts"]], [1, 0])
        self.assertEqual(result["matched_pairs"], 2)
        self.assertEqual(result["mean"], 0.5)
        self.assertAlmostEqual(result["monte_carlo_se"], 0.5)
        self.assertEqual(result["all_assigned_bounds"], [0, 1 / 3])
