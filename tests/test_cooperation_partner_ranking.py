"""Equal actual rewiring with distinguishable, settled candidate evidence."""
import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import replay_run
from civitas.contracts.economic import CooperationPartnerProposal
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-partner-ranking.json"
FOCAL, INCUMBENT, BETTER, WORSE = (f"household:{i:04d}" for i in (1, 2, 5, 7))


def graph(world):
    return {(e["source_id"], e["target_id"])
            for e in world.snapshot.data["closed_social"]["network"]["communication"]}


def choice(world):
    return next(r for r in world.snapshot.data["closed_social"]["trace"]
                if r["kind"] == "cooperation_partner_choice" and r["actor_id"] == FOCAL)


class CooperationPartnerRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_evidence_ranks_two_real_alternates_and_uniform_uses_same_exchange_capacity(self):
        inspections = []
        for uniform, expected in ((False, BETTER), (True, WORSE)):
            raw = json.loads(EXAMPLE.read_text())["base_config"]
            raw["root_seed"] = 137
            raw["cooperation"]["followup"]["partner_choice"]["random_choice"] = uniform
            world = build_closed_economy(raw)
            initial = graph(world)
            await world.run(11)
            view = world.observe(FOCAL)
            offer, = [f for f in view.market_view if f.value.get("kind") == "cooperation_partner_offer"]
            self.assertEqual(offer.value["candidate_ids"], [BETTER, WORSE])
            self.assertNotIn(INCUMBENT, offer.value["candidate_ids"])
            facts = [f for f in view.private_memory_refs if f.value.get("kind") == "cooperation_outcome"]
            self.assertEqual({f.value["partner_id"]: f.value["partner_action"] for f in facts},
                             {BETTER: "cooperate", WORSE: "defect"})
            settlements = {e.tx_id for e in world.scenario.economy.journal if e.kind == "cooperation_settlement"}
            self.assertEqual({f.value["ledger_tx_id"] for f in facts}, settlements)
            decision = next(d for d in world.snapshot.decisions if d.actor_id == FOCAL and d.tick == 11)
            used = [r for r in decision.evidence_ids if r.startswith("cooperation-outcome:")]
            self.assertEqual(set(used), set() if uniform else {f.fact_id for f in facts})
            self.assertEqual(graph(world), initial)
            await world.step()
            after = graph(world)
            self.assertEqual((len(initial - after), len(after - initial)), (2, 2))
            self.assertIn((FOCAL, expected), after)
            for direction in (0, 1):
                self.assertEqual(Counter(e[direction] for e in initial), Counter(e[direction] for e in after))
            fee, = [e for e in world.scenario.economy.journal if e.kind == "link_fee"]
            self.assertEqual((fee.tick, fee.payload["payer_id"], fee.payload["amount_minor"]), (12, FOCAL, 3))
            following = world.snapshot.data["closed_economy"]["cooperation_followup_round"]
            self.assertEqual(following["seats"], [FOCAL, expected])
            self.assertEqual(following["choices"][FOCAL]["action"], "defect" if uniform else "cooperate")
            await world.step()
            self.assertEqual(world.snapshot.data["closed_economy"]["cooperation_followup_round"]["status"], "settled")
            inspections.append(view.observation_id)
        self.assertEqual(*inspections)

    async def test_admission_needs_both_settlements_and_rejects_incumbent(self):
        for mode in ("missing_result", "unaffordable", "incumbent", "reverse_evidence"):
            with self.subTest(mode=mode):
                raw = json.loads(EXAMPLE.read_text())["base_config"]
                raw.update(ticks=7, labor_transfer=None)
                raw["cooperation"]["offer_tick"] = 3
                followup = raw["cooperation"]["followup"]
                followup["offer_tick"] = 6
                setup = followup["partner_choice"]
                setup["comparison_trial"]["offer_tick"] = 1
                if mode == "missing_result":
                    setup["comparison_trial"]["action"] = "decline"
                if mode == "unaffordable":
                    setup["fee_minor"] = 1000000
                if mode == "reverse_evidence":
                    setup["comparison_trial"]["action"] = "cooperate"
                    raw["cooperation"]["actions"][1] = "defect"
                world = build_closed_economy(raw)
                initial = graph(world)
                decide = world.policy.decide

                async def inject(observation):
                    bundle = await decide(observation)
                    if mode == "incumbent" and bundle.actor_id == FOCAL and bundle.tick == 5:
                        proposal, = bundle.network_proposals
                        self.assertIsInstance(proposal, CooperationPartnerProposal)
                        return bundle.model_copy(update={"network_proposals": [
                            proposal.model_copy(update={"candidate_id": INCUMBENT})]})
                    return bundle

                with patch.object(world.policy, "decide", inject):
                    await world.run(6)
                row = choice(world)
                follow = world.snapshot.data["closed_economy"]["cooperation_followup_round"]
                if mode == "reverse_evidence":
                    self.assertEqual((row["status"], row["candidate_id"]), ("scheduled", WORSE))
                    self.assertEqual(follow["seats"], [FOCAL, WORSE])
                else:
                    status = "unavailable" if mode == "incumbent" else "admission_unavailable"
                    self.assertEqual(row["status"], status)
                    self.assertEqual((follow["status"], follow["offered"]), ("no_active_exchange", []))
                    self.assertEqual(graph(world), initial)
                    self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "link_fee"])

    async def test_ordinary_market_spend_can_lapse_shared_funding(self):
        raw = json.loads(EXAMPLE.read_text())["base_config"]
        raw["root_seed"] = 137
        raw["cooperation"]["followup"]["partner_choice"]["fee_minor"] = 58
        world = build_closed_economy(raw)
        initial = graph(world)
        await world.run(12)
        row = choice(world)
        self.assertEqual(row["offers"]["candidate_ids"], [BETTER, WORSE])
        self.assertEqual(row["status"], "funding_rejected")
        self.assertEqual(graph(world), initial)
        self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "link_fee"])
        self.assertEqual(world.snapshot.data["closed_economy"]["cooperation_followup_round"]["offered"], [])

    async def test_matched_recovery_does_not_confuse_failure_and_natural_censoring(self):
        spec = json.loads(EXAMPLE.read_text())
        spec["seeds"] = [137, 15]
        original = CommitStore.fault

        def fail(store, point):
            if (point == "before_marker" and store.root.parent.name == "seed-137"
                    and store.root.name == "evidence" and store.marker_path(11).is_file()):
                raise OSError("ranking activation interrupted")
            original(store, point)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            with patch.object(CommitStore, "fault", fail):
                interrupted = await run_pilot(root, spec)
            self.assertEqual(interrupted["status"], "failed")
            self.assertEqual(interrupted["comparison"]["all_assigned_bounds"], [-0.25, 0.25])
            self.assertEqual(interrupted["runs"][0]["periods"][-1]["tick"], 11)
            for row in interrupted["runs"][2:]:
                self.assertEqual(row["partner_selection"]["status"], "censored")
                self.assertEqual(row["partner_selection"]["fee_minor"], 0)
            complete = await run_pilot(root)
            self.assertEqual(complete, analyze_pilot(root))
            self.assertEqual(complete["status"], "completed")
            self.assertEqual(complete["comparison"]["exchange_mismatches"], [])
            self.assertEqual(complete["comparison"]["all_assigned_share"]["mean"], 0.25)
            self.assertEqual(complete["runs"][1:], interrupted["runs"][1:])
            self.assertEqual(complete["runs"][0]["periods"][:12], interrupted["runs"][0]["periods"])
            for row in complete["runs"][:2]:
                self.assertEqual((row["partner_replaced"], row["partner_selection"]["fee_minor"]), (1, 3))
            replay = await replay_run(root / complete["runs"][0]["relative_path"], destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 13))
