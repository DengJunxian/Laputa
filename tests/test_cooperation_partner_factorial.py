"""Factor assignments, matched interventions and durable fee recovery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.economic_run import replay_run
from civitas.experiments import registry
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot, summarize_interaction
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-partner-factorial.json"
ARMS = ("t0r0", "t0r1", "t1r0", "t1r1")


class CooperationPartnerFactorialTests(unittest.IsolatedAsyncioTestCase):
    async def test_matched_quartets_preserve_fee_and_recover_interaction(self):
        spec = json.loads(EXAMPLE.read_text())
        spec["seeds"] = [272, 260]
        original = CommitStore.fault

        def interrupt(store, point):
            if (point == "before_marker" and store.root.parent.name == "seed-272"
                    and store.root.name == "t1r1" and store.marker_path(13).is_file()):
                raise OSError("exchange and fee interrupted before commit")
            original(store, point)

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "pilot"
            with patch.object(CommitStore, "fault", interrupt):
                failed = await run_pilot(root, spec)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["interaction"]["all_assigned_bounds"], [-0.5, 0.0])
            self.assertEqual(failed["interaction"]["excluded_quartets"],
                             [{"seed": 272, "reason": "incomplete_quartet", "arms": {"t1r1": "failed"}}])
            self.assertEqual(failed["runs"][3]["periods"][-1]["tick"], 13)
            self.assertEqual(analyze_pilot(root), failed)
            _, interrupted = CommitStore(root / "seed-272/t1r1").load()
            self.assertFalse([r for r in interrupted.data["economic_runtime"]["journal"] if r["kind"] == "link_fee"])

            report = await run_pilot(root)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report, analyze_pilot(root))
            self.assertEqual(report["runs"][:3], failed["runs"][:3])
            self.assertEqual(report["runs"][4:], failed["runs"][4:])
            self.assertEqual(report["runs"][3]["periods"][:14], failed["runs"][3]["periods"])
            counts = {arm: r["partner_selection"] for arm, r in report["arms"].items()}
            self.assertEqual({(r["attempted"], r["replaced"], r["fee_minor"]) for r in counts.values()}, {(2, 1, 3)})
            completed = dict(zip(ARMS, report["runs"][:4], strict=True))
            choices = {arm: r["partner_selection"] for arm, r in completed.items()}
            self.assertEqual(choices["t0r0"]["draw_u64_hex"], choices["t1r0"]["draw_u64_hex"])
            self.assertIsNotNone(choices["t0r0"]["draw_u64_hex"])
            self.assertEqual(choices["t0r0"]["active_partner"], choices["t1r0"]["active_partner"])
            self.assertEqual(choices["t0r1"]["active_partner"], "household:0007")
            self.assertEqual(choices["t1r1"]["active_partner"], "household:0005")
            self.assertEqual({arm: len(r["candidate_trust"]["updates"]) for arm, r in completed.items()},
                             {"t0r0": 0, "t0r1": 0, "t1r0": 6, "t1r1": 6})
            self.assertEqual({arm: len(r["result_fact_ids"]) for arm, r in choices.items()},
                             {"t0r0": 0, "t0r1": 2, "t1r0": 0, "t1r1": 6})
            self.assertEqual({r["cooperation"]["offered"] for r in completed.values()}, {2})
            self.assertEqual(report["interaction"]["all_assigned_bounds"], [-0.25, -0.25])
            self.assertEqual(report["interaction"]["offered_rate"]["mean"], -0.5)
            self.assertEqual(report["interaction"]["contrasts"][0]["trust_effect_uniform"], 0)
            self.assertEqual(report["interaction"]["exchange_mismatches"], [])
            self.assertEqual([r["status"] for r in registry.history(root) if r["job"] == "seed-272/t1r1"],
                             ["running", "failed", "running", "finished"])
            replay = await replay_run(root / "seed-272/t1r1", destination=Path(scratch) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 15))

    def test_realized_fee_mismatch_and_missing_arm_remain_in_assignment_bounds(self):
        rows = []
        for seed, counts in enumerate(([0, 0, 0, 0], [0, 0, 0, 2], [2, None, 0, 1])):
            for arm, count in zip(ARMS, counts, strict=True):
                offers = 2 if seed else 0
                rows.append({
                    "seed": seed, "arm": arm, "status": "failed" if count is None else "completed",
                    "partner_replaced": int(bool(offers)),
                    "partner_selection": {"fee_minor": 4 if (seed, arm) == (1, "t1r1") else 3 * bool(offers)},
                    "opportunity_hash": "same-terms", "pretreatment_hash": "common-history",
                    "cooperation": {"offered": offers, "cooperative_choices": count},
                })
        result = summarize_interaction(rows, [0, 1, 2], partner=True)
        self.assertEqual(result["assigned_quartets"], 3)
        self.assertEqual(result["matched_quartets"], 1)
        self.assertEqual(result["offered_quartets"], 0)
        self.assertIsNone(result["offered_rate"]["mean"])
        self.assertEqual(result["all_assigned_bounds"], [0.5, 5 / 6])
        self.assertEqual(result["exchange_mismatches"], [1])
        self.assertEqual(result["excluded_quartets"], [
            {"seed": 1, "reason": "realized_exchange_mismatch"},
            {"seed": 2, "reason": "incomplete_quartet", "arms": {"t0r1": "failed"}},
        ])
