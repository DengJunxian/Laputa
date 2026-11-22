"""Withdrawal, delivery authorization and capacity through real C/D ticks."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.economic import NetworkWithdrawal
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy


AUTHOR = "household:0000"
RECEIVER = "household:0003"


def withdrawal_config(**changes):
    example = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-network-withdrawals.json"
    return ClosedEconomyConfig.model_validate(dict(json.loads(example.read_text()), **changes))


def network(world):
    return world.snapshot.data["closed_social"]["network"]


def edges(world):
    return {(edge["source_id"], edge["target_id"]) for edge in network(world)["communication"]}


def trace(world, kind):
    return [row for row in world.snapshot.data["closed_social"]["trace"] if row["kind"] == kind]


class NetworkWithdrawalTests(unittest.IsolatedAsyncioTestCase):
    async def test_withdrawal_waits_for_boundary_and_cuts_off_inflight_disclosure(self):
        world = build_closed_economy(withdrawal_config())
        await world.run(6)
        removal = next(d for d in network(world)["pending"] if d["operation"] == "delete")
        pair = removal["source_id"], removal["target_id"]
        self.assertEqual((removal["proposed_tick"], removal["effective_tick"]), (6, 7))
        self.assertIn(pair, edges(world))
        packet = next(m for m in world.snapshot.messages
                      if (m.sender_id, m.recipient_id) == pair and m.created_at.tick == 6
                      and m.body.get("kind") == "verified_delivery_experience")
        self.assertEqual(packet.visible_at.tick, 7)
        await world.step()
        self.assertNotIn(pair, edges(world))
        self.assertIn(removal, network(world)["applied"])
        self.assertIn(packet, world.snapshot.messages)
        self.assertNotIn(packet.message_id, world.observe(pair[1]).evidence_ids)
        self.assertNotIn(packet.message_id, [row["message_id"] for row in trace(world, "exposure")])

    async def test_capacity_reopens_on_boundary_and_old_packets_stay_hidden_after_reconnection(self):
        world = build_closed_economy(withdrawal_config())
        await world.run(6)
        old_edges = edges(world)
        old_packet, = [m for m in world.snapshot.messages if (m.sender_id, m.recipient_id) == (AUTHOR, RECEIVER)
                       and m.created_at.tick == 6 and m.body.get("kind") == "verified_delivery_experience"]
        self.assertTrue(all(d["operation"] == "delete" for d in network(world)["pending"]))
        self.assertTrue(any(r["tick"] == 6 and r["status"] == "period_budget_exhausted"
                            for r in trace(world, "network_proposal")))
        await world.step()
        self.assertNotIn((AUTHOR, RECEIVER), edges(world))
        reopened, = [d for d in network(world)["pending"] if (d["source_id"], d["target_id"]) == (AUTHOR, RECEIVER)]
        self.assertEqual((reopened["operation"], reopened["proposed_tick"], reopened["effective_tick"]),
                         ("upsert", 7, 8))
        await world.step()
        self.assertIn((AUTHOR, RECEIVER), edges(world))
        self.assertNotIn(old_packet.message_id, world.observe(RECEIVER).evidence_ids)
        await world.run(2)
        self.assertTrue(any(pair not in old_edges for pair in edges(world)))
        new_packet, = [
            m for m in world.snapshot.messages if (m.sender_id, m.recipient_id) == (AUTHOR, RECEIVER)
            and m.created_at.tick == 8 and m.body.get("kind") == "verified_delivery_experience"]
        exposures = {row["message_id"]: row["tick"] for row in trace(world, "exposure")}
        self.assertEqual(exposures[new_packet.message_id], 9)
        self.assertNotIn(old_packet.message_id, exposures)

    async def test_foreign_and_fixed_ring_claims_fail_and_retries_use_no_slot(self):
        class RetryPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != RECEIVER:
                    return bundle
                if bundle.tick == 6:
                    self.own, = [p for p in bundle.network_proposals if isinstance(p, NetworkWithdrawal)]
                    return bundle.model_copy(update={"network_proposals": [
                        *bundle.network_proposals, self.own, NetworkWithdrawal(edge_delta_id=self.foreign_id),
                        NetworkWithdrawal(edge_delta_id="fixed-ring:household:0001:household:0003")]})
                if bundle.tick == 9:
                    return bundle.model_copy(
                        update={"network_proposals": [self.own, *bundle.network_proposals]})
                return bundle

        config = withdrawal_config()
        policy = RetryPolicy(config, AddressedRNG(7, 0))
        world = build_closed_economy(config, policy=policy)
        await world.run(5)
        policy.foreign_id = next(d["delta_id"] for d in network(world)["applied"] if d["target_id"] != RECEIVER)
        await world.run(4)
        rows = [r for r in trace(world, "network_withdrawal") if r["actor_id"] == RECEIVER]
        self.assertCountEqual([r["status"] for r in rows],
                              ["scheduled", "duplicate", "duplicate", "edge_unavailable", "edge_unavailable"])
        self.assertEqual(sum(r["slots_used"] for r in rows), 1)
        self.assertIn(("household:0001", RECEIVER), edges(world))
        self.assertIn((AUTHOR, RECEIVER), edges(world))

    async def test_valid_edge_id_without_cited_incoming_observation_is_denied(self):
        class UncitedPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick == 6 and bundle.actor_id == RECEIVER:
                    context = next(f for f in observation.market_view
                                   if f.value.get("kind") == "closed_social_context")
                    return bundle.model_copy(update={"evidence_ids": [
                        item for item in bundle.evidence_ids if item != context.fact_id]})
                return bundle

        config = withdrawal_config()
        world = build_closed_economy(config, policy=UncitedPolicy(config, AddressedRNG(7, 0)))
        await world.run(7)
        denied, = [r for r in trace(world, "network_withdrawal") if r["actor_id"] == RECEIVER]
        self.assertEqual((denied["status"], denied["slots_used"]), ("edge_unavailable", 0))
        self.assertIn((AUTHOR, RECEIVER), edges(world))

    async def test_failed_schedule_and_boundary_retry_then_replay_from_disk(self):
        config = withdrawal_config()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            world, store = economic_run.create_run(config, root)
            for tick in (6, 7):
                await economic_run.advance(world, store, until=tick - 1)
                prior = content_hash(world.snapshot)
                book = content_hash(world.scenario.economy.export_state())

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before withdrawal marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "before withdrawal marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), prior)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), book)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual(marker.state_hash, prior)
                await economic_run.advance(world, store, until=tick)
            await economic_run.advance(world, store, until=10)
            direct = build_closed_economy(withdrawal_config(concurrency=4))
            await direct.run(10)
            self.assertEqual(content_hash(world.snapshot), content_hash(direct.snapshot))
            replay = await economic_run.replay_run(root, destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 10))

    async def test_withdrawal_preserves_economy_and_existing_wage_claims(self):
        action = build_closed_economy(withdrawal_config(initial_firm_cash_minor=10))
        control = build_closed_economy(withdrawal_config(initial_firm_cash_minor=10, link_withdrawal_tick=None))
        claims_after_cutoff = []
        for tick in range(1, 11):
            await action.step()
            await control.step()
            self.assertEqual(audit_snapshot(action.snapshot)["status"], "passed")
            self.assertEqual(action.scenario.economy.export_state(), control.scenario.economy.export_state())
            if tick >= 7:
                claims_after_cutoff.extend(row["outstanding_minor"] for row
                    in action.scenario.economy.export_state()["wage_obligations"])
        self.assertTrue(trace(action, "communication_edge_withdrawn"))
        self.assertFalse(trace(control, "communication_edge_withdrawn"))
        self.assertGreater(max(claims_after_cutoff, default=0), 0)
