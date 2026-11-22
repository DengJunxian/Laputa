"""A private, fee-backed partner exchange using the C/D world and ledger."""
import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import replay_run
from civitas.contracts.economic import CooperationPartnerProposal
from civitas.experiments.closed_economy import audit_snapshot
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-partner-choice.json"
FOCAL, INCUMBENT, ANCHOR, ALTERNATE = (f"household:{i:04d}" for i in (1, 2, 4, 5))


def graph(world):
    return {(e["source_id"], e["target_id"])
            for e in world.snapshot.data["closed_social"]["network"]["communication"]}


def choices(world):
    return [r for r in world.snapshot.data["closed_social"]["trace"] if r["kind"] == "cooperation_partner_choice"]


class CooperationPartnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_partner_action_drives_one_degree_preserving_fee_backed_exchange(self):
        raw = json.loads(EXAMPLE.read_text())["base_config"]
        world = build_closed_economy(raw)
        initial = graph(world)
        await world.run(11)
        self.assertTrue(any(f.value.get("kind") == "cooperation_partner_offer" for f in world.observe(FOCAL).market_view))
        self.assertFalse(any(f.value.get("kind") == "cooperation_partner_offer" for f in world.observe(INCUMBENT).market_view))
        self.assertEqual(graph(world), initial)
        self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "link_fee"])
        await world.step()
        after = graph(world)
        self.assertEqual(after - initial, {(FOCAL, ALTERNATE), (ANCHOR, INCUMBENT)})
        self.assertEqual(initial - after, {(FOCAL, INCUMBENT), (ANCHOR, ALTERNATE)})
        for direction in (0, 1):
            self.assertEqual(Counter(e[direction] for e in after), Counter(e[direction] for e in initial))
        fee, = [e for e in world.scenario.economy.journal if e.kind == "link_fee"]
        self.assertEqual((fee.tick, fee.payload["payer_id"], fee.payload["amount_minor"]), (12, FOCAL, 3))
        round_ = world.snapshot.data["closed_economy"]["cooperation_followup_round"]
        self.assertEqual(round_["seats"], [FOCAL, ALTERNATE])
        self.assertEqual({a: r["action"] for a, r in round_["choices"].items()}, {FOCAL: "cooperate", ALTERNATE: "cooperate"})
        await world.step()
        settlement = next(e for e in world.scenario.economy.journal if e.tick == 13 and e.kind == "cooperation_settlement")
        self.assertEqual(set(settlement.payload["contributions"]), {FOCAL, ALTERNATE})
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_insufficient_cash_unauthorized_candidate_and_duplicate_leave_original_graph(self):
        for expected in ("funding_rejected", "unavailable", "duplicate_proposal"):
            with self.subTest(status=expected):
                raw = json.loads(EXAMPLE.read_text())["base_config"]
                raw.update(ticks=5, labor_transfer=None)
                raw["cooperation"]["offer_tick"] = 1
                followup = raw["cooperation"]["followup"]
                followup["offer_tick"] = 4
                if expected == "funding_rejected":
                    followup["partner_choice"]["fee_minor"] = 1_000_000
                world = build_closed_economy(raw)
                initial = graph(world)
                decide = world.policy.decide

                async def inject(observation):
                    bundle = await decide(observation)
                    if bundle.actor_id == FOCAL and bundle.tick == 3 and expected != "funding_rejected":
                        proposal, = bundle.network_proposals
                        proposals = [proposal, proposal] if expected == "duplicate_proposal" else [
                            proposal.model_copy(update={"candidate_id": "household:0008"})]
                        return bundle.model_copy(update={"network_proposals": proposals})
                    if bundle.actor_id == INCUMBENT and bundle.tick == 3:
                        stolen = CooperationPartnerProposal(option_id=f"cooperation-partner:3:{FOCAL}",
                            candidate_id=ALTERNATE, fee_limit_minor=3)
                        return bundle.model_copy(update={"network_proposals": [stolen]})
                    return bundle

                with patch.object(world.policy, "decide", inject):
                    await world.run(4)
                self.assertEqual({r["actor_id"]: r["status"] for r in choices(world)},
                                 {FOCAL: expected, INCUMBENT: "unavailable"})
                self.assertEqual(graph(world), initial)
                self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "link_fee"])
                self.assertEqual(world.snapshot.data["closed_economy"]["cooperation_followup_round"]["seats"],
                                 [FOCAL, INCUMBENT])

    async def test_failure_and_natural_stop_keep_pending_exchange_and_fee_distinct(self):
        spec = json.loads(EXAMPLE.read_text())
        spec["seeds"] = [7, 15]
        original_fault = CommitStore.fault

        def abort_first_activation(store, point):
            if (store.root.parent.name == "seed-7" and store.root.name == "evidence"
                    and point == "before_marker" and store.marker_path(11).is_file()):
                raise OSError("partner exchange marker interrupted")
            original_fault(store, point)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            with patch.object(CommitStore, "fault", abort_first_activation):
                failed = await run_pilot(root, spec)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["comparison"]["all_assigned_bounds"], [0, 0.5])
            interrupted = failed["runs"][0]
            self.assertEqual(interrupted["periods"][-1]["tick"], 11)
            self.assertNotIn("partner_replaced", interrupted)
            censored = failed["runs"][2]["partner_selection"]
            self.assertEqual((censored["status"], censored["fee_minor"], censored["replaced"]), ("censored", 0, 0))
            recovered = await run_pilot(root)
            self.assertEqual(recovered, analyze_pilot(root))
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["runs"][1:], failed["runs"][1:])
            measured = recovered["runs"][0]
            self.assertEqual(measured["periods"][:12], interrupted["periods"])
            self.assertEqual((measured["partner_selection"]["fee_minor"], measured["partner_replaced"]), (3, 1))
            self.assertEqual(recovered["comparison"]["mean"], 0.5)
            replay = await replay_run(root / measured["relative_path"], destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 13))
