"""A funded two-person cooperation round on the C/D execution boundary."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import replay_run
from civitas.contracts.economic import CooperationIntent
from civitas.economy.runtime import EconomicReject, EconomicRuntime
from civitas.experiments.closed_economy import audit_snapshot
from civitas.experiments.labor_factorial import _measure_cooperation, analyze_pilot, run_pilot
from civitas.mechanisms.games import bilateral_cooperation_deltas
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-cooperation.json"


def config(actions=("cooperate", "defect"), **changes):
    raw = json.loads(EXAMPLE.read_text())["base_config"]
    raw.update(changes)
    raw["cooperation"] = {"offer_tick": 9, "household_indices": [1, 2], "actions": list(actions)}
    return raw


class ClosedCooperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_asymmetric_choice_settles_one_prefunded_matrix_transaction(self):
        world = build_closed_economy(config())
        await world.run(9)
        round_ = world.snapshot.data["closed_economy"]["cooperation_round"]
        self.assertEqual(round_["status"], "pending")
        self.assertEqual(round_["offered"], ["household:0001", "household:0002"])
        self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "cooperation_settlement"])
        await world.step()
        round_ = world.snapshot.data["closed_economy"]["cooperation_round"]
        self.assertEqual(round_["status"], "settled")
        entry, = [e for e in world.scenario.economy.journal if e.kind == "cooperation_settlement"]
        self.assertEqual(entry.tick, 10)
        self.assertEqual(entry.details["cash_delta_minor"], {"household:0001": -1, "household:0002": 3, "firm:00": -2})
        self.assertEqual(round_["settlement_tx_id"], entry.tx_id)
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_offer_is_private_and_decline_keeps_both_offered_seats(self):
        world = build_closed_economy(config(("cooperate", "decline")))
        await world.run(8)
        decide = world.policy.decide

        async def inject(observation):
            bundle = await decide(observation)
            if bundle.actor_id in {"household:0001", "household:0003"}:
                extra = CooperationIntent(intent_id=f"injected:{bundle.actor_id}", opportunity_id="cooperation:9",
                                          action="defect")
                return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, extra]})
            return bundle

        with patch.object(world.policy, "decide", inject):
            await world.step()
        snapshot = world.snapshot
        round_ = snapshot.data["closed_economy"]["cooperation_round"]
        self.assertEqual(round_["status"], "unmatched")
        self.assertEqual(set(round_["choices"]), set(round_["offered"]))
        self.assertEqual(set(round_["offered"]), {"household:0001", "household:0002"})
        for decision in snapshot.decisions:
            if decision.tick == 9:
                self.assertEqual("cooperation:9" in decision.evidence_ids, decision.actor_id in round_["offered"])
        rejections = {r.actor_id: r.reason_codes for r in snapshot.receipts if r.command_id.startswith("command:injected:")}
        self.assertEqual(rejections, {"household:0001": ["duplicate_cooperation_choice"],
                                      "household:0003": ["cooperation_opportunity_unavailable"]})
        measured = _measure_cooperation(snapshot)
        self.assertEqual((measured["offered"], measured["declines"], measured["cooperation_rate"]), (2, 1, 0.5))
        await world.step()
        self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "cooperation_settlement"])

    async def test_unfunded_offer_has_no_choices_or_offered_seats(self):
        raw = config(initial_firm_cash_minor=0, initial_household_cash_minor=0, cash_jitter_minor=0)
        raw["cooperation"]["offer_tick"] = 1
        world = build_closed_economy(raw)
        await world.step()
        round_ = world.snapshot.data["closed_economy"]["cooperation_round"]
        self.assertEqual((round_["status"], len(round_["seats"]), round_["offered"], round_["choices"]),
                         ("ineligible", 2, [], {}))
        self.assertIsNone(_measure_cooperation(world.snapshot)["cooperation_rate"])

    def test_actual_settlement_rechecks_funding_before_any_postings(self):
        for (a, b), expected in {(4, 4): (2, 2), (4, 0): (-1, 3), (0, 4): (3, -1), (0, 0): (0, 0)}.items():
            self.assertEqual(bilateral_cooperation_deltas({"a": a, "b": b}), dict(zip(("a", "b"), expected)))
        cases = [({"a": 3, "b": 20, "sponsor": 20}, "contribution"),
                 ({"a": 20, "b": 20, "sponsor": 1}, "sponsor")]
        for cash, party in cases:
            with self.subTest(party=party):
                ledger = EconomicRuntime(cash)
                ledger.begin_tick(1, 0)
                ledger.begin_tick(2, 1)
                with self.assertRaisesRegex(EconomicReject, f"insufficient_cooperation_{party}_cash"):
                    ledger.settle_cooperation("cooperation:1", 1, "sponsor", {"a": 4, "b": 0})
                self.assertEqual(len(ledger.journal), 1)
                self.assertEqual({a: ledger.account_view(a).available_cash_minor for a in cash}, cash)

    async def test_common_stop_and_failed_settlement_commit_have_different_denominators(self):
        spec = json.loads(EXAMPLE.read_text())
        spec["seeds"] = [7, 251]
        original_fault = CommitStore.fault

        def interrupt_settlement(store, point):
            if (point == "before_marker" and store.root.parent.name == "seed-7"
                    and store.root.name == "cooperate" and store.marker_path(9).is_file()):
                raise OSError("cooperation marker interrupted")
            original_fault(store, point)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            with patch.object(CommitStore, "fault", interrupt_settlement):
                failed = await run_pilot(root, spec)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["comparison"]["all_assigned_bounds"], [0.5, 1])
            interrupted = failed["runs"][0]
            self.assertEqual(interrupted["periods"][-1]["tick"], 9)
            self.assertNotIn("cooperation", interrupted)
            censored = failed["runs"][2]["cooperation"]
            self.assertEqual((censored["settlement_status"], censored["offered"], censored["cooperation_rate"]),
                             ("censored", 2, 1.0))
            self.assertIsNone(censored["settlement_tx_id"])
            report = await run_pilot(root)
            self.assertEqual(report, analyze_pilot(root))
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["runs"][1:], failed["runs"][1:])
            recovered = report["runs"][0]
            self.assertEqual(recovered["periods"][:10], interrupted["periods"])
            self.assertEqual(recovered["cooperation"]["cash_delta_minor"],
                             {"household:0001": 2, "household:0002": 2, "firm:00": -4})
            self.assertEqual(report["comparison"]["offered_rate"]["mean"], 1)
            replay = await replay_run(root / recovered["relative_path"], destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 11))
