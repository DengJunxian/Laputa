"""Observe and consume a funded result through the normal C/D decision path."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import replay_run
from civitas.experiments.closed_economy import audit_snapshot
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-cooperation-feedback.json"
SEAT_1, SEAT_2 = "household:0001", "household:0002"


def visible_outcomes(observation):
    return [fact for fact in observation.private_memory_refs if fact.value.get("kind") == "cooperation_outcome"]


class CooperationFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_settlement_result_changes_one_followup_choice(self):
        raw = json.loads(EXAMPLE.read_text())["base_config"]
        world = build_closed_economy(raw)
        decide = world.policy.decide
        seen = {}

        async def capture(observation):
            seen[observation.visible_until.tick, observation.actor_id] = visible_outcomes(observation)
            return await decide(observation)

        def abort_first_settlement(phase, snapshot):
            if phase == "COMMIT" and snapshot.tick == 10:
                raise OSError("first result commit interrupted")

        with patch.object(world.policy, "decide", capture):
            await world.run(9)
            with patch.object(world, "phase_hook", abort_first_settlement):
                with self.assertRaisesRegex(OSError, "first result commit interrupted"):
                    await world.step()
            self.assertEqual(world.snapshot.tick, 9)
            self.assertFalse([f for f in world.snapshot.memories if f.value.get("kind") == "cooperation_outcome"])
            self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "cooperation_settlement"])
            await world.step()
            self.assertTrue(all(not facts for facts in seen.values()))
            self.assertEqual(visible_outcomes(world.observe(SEAT_1)), [])
            await world.step()
            first, = seen[11, SEAT_1]
            other, = seen[11, SEAT_2]
            self.assertEqual((first.value["partner_action"], first.value["cash_delta_minor"]), ("defect", -1))
            self.assertEqual((other.value["partner_action"], other.value["cash_delta_minor"]), ("cooperate", 3))
            self.assertEqual(first.value["ledger_tx_id"], other.value["ledger_tx_id"])
            self.assertEqual(first.value["root_claim_id"], other.value["root_claim_id"])
            self.assertNotEqual(first.fact_id, other.fact_id)
            self.assertEqual({actor for (tick, actor), facts in seen.items() if tick == 11 and facts}, {SEAT_1, SEAT_2})
            outcomes = {}
            for decision in world.snapshot.decisions:
                if decision.tick == 11 and decision.actor_id in (SEAT_1, SEAT_2):
                    intent, = [i for i in decision.economic_intents if i.kind == "cooperation_choice"]
                    outcomes[decision.actor_id] = intent.action
                    self.assertIn(seen[11, decision.actor_id][0].fact_id, decision.evidence_ids)
            self.assertEqual(outcomes, {SEAT_1: "defect", SEAT_2: "cooperate"})
            await world.step()

        evidence_ids = [e for d in world.snapshot.decisions for e in d.evidence_ids if e.startswith("cooperation-outcome:")]
        self.assertCountEqual(evidence_ids, [first.fact_id, other.fact_id])
        entries = [e for e in world.scenario.economy.journal if e.kind == "cooperation_settlement"]
        self.assertEqual([e.tick for e in entries], [10, 12])
        self.assertEqual(entries[-1].details["cash_delta_minor"], {SEAT_1: 3, SEAT_2: -1, "firm:00": -2})
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_unsettled_first_round_gives_no_result_and_uses_fixed_fallback(self):
        for expected in ("unmatched", "funding_rejected"):
            with self.subTest(status=expected):
                raw = json.loads(EXAMPLE.read_text())["base_config"]
                raw.update(ticks=4, labor_transfer=None)
                raw["cooperation"]["offer_tick"] = 1
                raw["cooperation"]["followup"]["offer_tick"] = 3
                if expected == "unmatched":
                    raw["cooperation"]["actions"] = ["cooperate", "decline"]
                else:
                    raw.update(initial_household_cash_minor=4, cash_jitter_minor=0, initial_price_minor=16,
                               basket_share_min_bps=10000, basket_share_max_bps=10000, cash_reserve_minor=0)
                world = build_closed_economy(raw)
                await world.run(3)
                self.assertEqual(world.snapshot.data["closed_economy"]["cooperation_round"]["status"], expected)
                self.assertEqual(visible_outcomes(world.observe(SEAT_1)), [])
                choices = [intent.action for d in world.snapshot.decisions if d.tick == 3
                           for intent in d.economic_intents if intent.kind == "cooperation_choice"]
                self.assertEqual(choices, ["cooperate", "cooperate"])
                self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "cooperation_settlement"])

    async def test_paired_analysis_and_recovery_preserve_authorized_result_use(self):
        spec = json.loads(EXAMPLE.read_text())
        spec["seeds"] = [7, 15, 277]
        original_fault = CommitStore.fault

        def fail_settlement(store, point):
            if (store.root.parent.name == "seed-7" and store.root.name == "conditional"
                    and point == "before_marker" and store.marker_path(11).is_file()):
                raise OSError("follow-up settlement marker interrupted")
            original_fault(store, point)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            with patch.object(CommitStore, "fault", fail_settlement):
                failed = await run_pilot(root, spec)
            interrupted = failed["runs"][0]
            self.assertEqual((failed["status"], interrupted["periods"][-1]["tick"]), ("failed", 11))
            self.assertNotIn("cooperation", interrupted)
            self.assertEqual(failed["comparison"]["all_assigned_bounds"], [-0.5, -1 / 6])
            recovered = await run_pilot(root)
            self.assertEqual(recovered, analyze_pilot(root))
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["runs"][1:], failed["runs"][1:])
            self.assertEqual(recovered["runs"][0]["periods"][:12], interrupted["periods"])
            self.assertEqual(recovered["comparison"]["offered_rate"]["mean"], -0.5)
            conditional, fixed, censored, _, no_followup, _ = recovered["runs"]
            self.assertEqual(conditional["first_cooperation"], fixed["first_cooperation"])
            self.assertEqual((conditional["cooperation"]["feedback_uses"], fixed["cooperation"]["feedback_uses"]), (2, 0))
            self.assertEqual((conditional["cooperation"]["sponsor_subsidy_minor"],
                              fixed["cooperation"]["sponsor_subsidy_minor"]), (2, 4))
            self.assertEqual((censored["cooperation"]["settlement_status"],
                              censored["cooperation"]["cooperation_rate"]), ("censored", 0.5))
            self.assertEqual(no_followup["first_cooperation"]["settlement_status"], "settled")
            self.assertIsNone(no_followup["cooperation"]["cooperation_rate"])
            replay = await replay_run(root / conditional["relative_path"], destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 12))
