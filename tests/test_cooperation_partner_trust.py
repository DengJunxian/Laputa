"""Repeated direct settlements feed a bounded, role-scoped partner belief."""
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import replay_run
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot
from civitas.scenarios.closed_economy_partner import PartnerPerformanceTrust
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-partner-trust.json"
FOCAL, CONSISTENT, RECENT = (f"household:{i:04d}" for i in (1, 5, 7))


def graph(world):
    return {(e["source_id"], e["target_id"])
            for e in world.snapshot.data["closed_social"]["network"]["communication"]}


class CooperationPartnerTrustTests(unittest.IsolatedAsyncioTestCase):
    async def test_cumulative_state_and_latest_action_disagree_under_equal_fees(self):
        for enabled, expected in ((True, CONSISTENT), (False, RECENT)):
            raw = json.loads(EXAMPLE.read_text())["base_config"]
            raw["cooperation"]["followup"]["partner_choice"]["trust_ranking"]["enabled"] = enabled
            world = build_closed_economy(raw)
            initial = graph(world)
            await world.run(13)
            state = world.snapshot.data["closed_social"]
            focal = state["current"][FOCAL]
            scores = focal.get("partner_performance_trust", {})
            if enabled:
                self.assertEqual({a: (r["a"], r["b"]) for a, r in scores.items()}, {CONSISTENT: (3, 2), RECENT: (2, 3)})
                updates = [r for r in state["trace"] if r["kind"] == "partner_performance_update"]
                self.assertEqual(len({r["root_claim_id"] for r in updates}), 6)
                self.assertEqual([r["effective_tick"] for r in updates], [3, 5, 7, 9, 11, 13])
            else:
                self.assertFalse(scores)
                self.assertFalse([r for r in state["trace"] if r["kind"] == "partner_performance_update"])
            self.assertEqual(graph(world), initial)
            await world.step()
            after = graph(world)
            self.assertIn((FOCAL, expected), after)
            for key in (0, 1):
                self.assertEqual(Counter(e[key] for e in initial), Counter(e[key] for e in after))
            fee, = [e for e in world.scenario.economy.journal if e.kind == "link_fee"]
            self.assertEqual((fee.tick, fee.payload["amount_minor"]), (14, 3))
            contract = world.snapshot.data["closed_economy"]["cooperation_followup_round"]
            self.assertEqual(contract["seats"], [FOCAL, expected])
            self.assertEqual(contract["choices"][FOCAL]["action"], "defect" if enabled else "cooperate")

    async def test_duplicate_root_is_idempotent_and_declined_trial_never_updates(self):
        score = PartnerPerformanceTrust().consume("original", True)
        self.assertEqual(score.consume("original", False), score)
        raw = json.loads(EXAMPLE.read_text())["base_config"]
        raw["cooperation"]["followup"]["partner_choice"]["trust_ranking"]["trials"][0]["action"] = "decline"
        world = build_closed_economy(raw)
        baseline = graph(world)
        await world.run(14)
        state = world.snapshot.data["closed_social"]
        self.assertEqual(state["current"][FOCAL]["partner_performance_trust"][CONSISTENT]["a"], 2)
        rows = [r for r in state["trace"] if r["kind"] == "partner_performance_update"]
        self.assertEqual(len({r["root_claim_id"] for r in rows}), 5)
        rejection = next(r for r in state["trace"] if r["kind"] == "cooperation_partner_choice")
        self.assertEqual(rejection["status"], "admission_unavailable")
        self.assertEqual(graph(world), baseline)
        self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "link_fee"])
        self.assertFalse(any(f.value.get("kind") == "partner_performance_trust"
                             for f in world.observe(RECENT).market_view))

    async def test_last_receipt_and_exchange_replay_once_after_commit_failure(self):
        spec = json.loads(EXAMPLE.read_text())
        spec["seeds"] = [7, 173]
        original = CommitStore.fault

        def fail(store, point):
            if (point == "before_marker" and store.root.parent.name == "seed-7"
                    and store.root.name == "cumulative" and store.marker_path(11).is_file()):
                raise OSError("last trust receipt commit interrupted")
            original(store, point)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            with patch.object(CommitStore, "fault", fail):
                interrupted = await run_pilot(root, spec)
            self.assertEqual(interrupted["status"], "failed")
            original_row = interrupted["runs"][0]
            self.assertEqual(original_row["periods"][-1]["tick"], 11)
            self.assertEqual(interrupted["comparison"]["all_assigned_bounds"], [-0.5, 0.0])
            for row in interrupted["runs"][2:]:
                self.assertEqual(row["partner_selection"]["status"], "censored")
                self.assertEqual(row["partner_selection"]["fee_minor"], 0)
            out = await run_pilot(root)
            self.assertEqual(out, analyze_pilot(root))
            self.assertEqual(out["status"], "completed")
            self.assertEqual(out["comparison"]["all_assigned_share"]["mean"], -0.25)
            self.assertEqual(out["comparison"]["exchange_mismatches"], [])
            self.assertEqual(out["runs"][1:], interrupted["runs"][1:])
            primary = out["runs"][0]
            self.assertEqual(primary["periods"][:12], original_row["periods"])
            self.assertEqual(len(primary["candidate_trust"]["updates"]), 6)
            self.assertEqual(primary["candidate_trust"]["effective_scores"][RECENT]["a"], 2)
            self.assertEqual(primary["partner_selection"]["fee_minor"], 3)
            replay = await replay_run(root / primary["relative_path"], destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 15))
