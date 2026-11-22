"""A witnessed introduction can spend one slot and open a real delivery path."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.economic import NetworkProposal
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy

AUTHOR = "household:0000"
FOLLOWER = "household:0003"


def network_config(**changes):
    example = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-network-links.json"
    return ClosedEconomyConfig.model_validate(dict(json.loads(example.read_text()), **changes))


def attempts(world, actor=None):
    return [r for r in world.snapshot.data["closed_social"]["trace"]
            if r["kind"] == "network_proposal" and (actor is None or r["actor_id"] == actor)]


class NetworkProposalTests(unittest.IsolatedAsyncioTestCase):
    async def test_introduction_queues_a_link_at_t4_and_delivers_on_new_path_at_t6(self):
        world = build_closed_economy(network_config())
        await world.run(4)
        social = world.snapshot.data["closed_social"]
        graph = social["network"]
        self.assertNotIn("neighbors", social)
        pending, = [d for d in graph["pending"] if d["target_id"] == FOLLOWER]
        self.assertEqual((pending["source_id"], pending["proposed_tick"], pending["effective_tick"]),
                         (AUTHOR, 4, 5))
        self.assertFalse(any(e["source_id"] == AUTHOR and e["target_id"] == FOLLOWER
                             for e in graph["communication"]))

        at_boundary = {}

        def capture(phase, snapshot):
            if phase.value == "BOUNDARY" and snapshot.tick == 5:
                seen = world.observer.observe(AUTHOR, snapshot, [])
                social_fact = next(f for f in seen.market_view if f.value["kind"] == "closed_social_context")
                at_boundary["neighbors"] = list(social_fact.value["neighbors"])

        world.phase_hook = capture
        await world.step()
        self.assertIn(FOLLOWER, at_boundary["neighbors"])
        graph = world.snapshot.data["closed_social"]["network"]
        self.assertIn(pending, graph["applied"])
        message, = [m for m in world.snapshot.messages if (m.sender_id, m.recipient_id) == (AUTHOR, FOLLOWER)
                    and m.created_at.tick == 5 and m.body["kind"] == "verified_delivery_experience"]
        self.assertEqual(message.visible_at.tick, 6)
        self.assertNotIn(message.message_id, world.observe(FOLLOWER).evidence_ids)
        await world.step()
        exposure, = [r for r in world.snapshot.data["closed_social"]["trace"] if r["kind"] == "exposure"
                     and r["message_id"] == message.message_id]
        self.assertEqual((exposure["actor_id"], exposure["tick"]), (FOLLOWER, 6))

    async def test_order_and_retry_do_not_change_selection_or_spend_a_second_slot(self):
        class DuplicatePolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if not bundle.network_proposals:
                    return bundle
                first = min(bundle.network_proposals, key=lambda p: p.introduction_message_id)
                return bundle.model_copy(update={"network_proposals": [*reversed(bundle.network_proposals), first]})

        config = network_config(households=8)
        regular = build_closed_economy(config)
        duplicate = build_closed_economy(config, policy=DuplicatePolicy(config, AddressedRNG(7, 0)))
        for world in (regular, duplicate):
            await world.run(4)
        a, b = (w.snapshot.data["closed_social"]["network"]["pending"] for w in (regular, duplicate))
        self.assertEqual(a, b)
        recorded = attempts(duplicate)
        self.assertTrue(any(row["status"] == "period_budget_exhausted" for row in recorded))
        scheduled = {row["delta_id"] for row in recorded if row["status"] == "scheduled"}
        self.assertTrue(scheduled)
        self.assertEqual(sum(row["slots_used"] for row in recorded), len(scheduled))
        self.assertEqual({row["delta_id"] for row in recorded if row["status"] == "duplicate"}, scheduled)
        await duplicate.step()
        self.assertTrue(any(row["status"] == "in_degree_limit" for row in attempts(duplicate)))

    async def test_valid_introduction_without_author_follow_consent_cannot_create_edge(self):
        class NoFollowConsent(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id == AUTHOR:
                    drafts = [d.model_copy(update={"body": {k:v for k,v in d.body.items() if k != "allow_follow"}})
                              if d.body.get("kind") == "verified_delivery_experience" else d
                              for d in bundle.message_drafts]
                    return bundle.model_copy(update={"message_drafts": drafts})
                if bundle.actor_id == FOLLOWER and bundle.tick == 4:
                    message, = [m for m in observation.delivered_messages if m.body.get("candidate_id") == AUTHOR]
                    proposal = NetworkProposal(introduction_message_id=message.message_id, candidate_id=AUTHOR)
                    return bundle.model_copy(update={"network_proposals": [proposal],
                        "evidence_ids": [*bundle.evidence_ids, message.message_id]})
                return bundle

        config = network_config()
        world = build_closed_economy(config, policy=NoFollowConsent(config, AddressedRNG(7, 0)))
        await world.run(4)
        denied, = attempts(world, FOLLOWER)
        self.assertEqual((denied["status"], denied["slots_used"]), ("introduction_unavailable", 0))
        self.assertFalse(any(d["target_id"] == FOLLOWER for d in world.snapshot.data["closed_social"]["network"]["pending"]))

    async def test_wrong_target_missing_evidence_and_expired_candidate_are_denied(self):
        class InvalidProposals(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != FOLLOWER:
                    return bundle
                if bundle.tick == 4:
                    self.old, = bundle.network_proposals
                    wrong = self.old.model_copy(update={"candidate_id": "household:0005"})
                    missing = self.old.model_copy(update={"introduction_message_id": "someone-elses-introduction"})
                    return bundle.model_copy(update={"network_proposals": [wrong, missing]})
                if bundle.tick == 5:
                    return bundle.model_copy(update={"network_proposals": [self.old]})
                return bundle

        config = network_config()
        world = build_closed_economy(config, policy=InvalidProposals(config, AddressedRNG(7, 0)))
        await world.run(5)
        self.assertEqual([(r["tick"], r["status"], r["slots_used"]) for r in attempts(world, FOLLOWER)],
                         [(4, "introduction_unavailable", 0), (4, "introduction_unavailable", 0),
                          (5, "introduction_unavailable", 0)])
        graph = world.snapshot.data["closed_social"]["network"]
        self.assertFalse(any(d["target_id"] == FOLLOWER for d in graph["pending"] + graph["applied"]))

    async def test_budget_zero_preserves_ring_and_economics_with_same_visible_start(self):
        active = build_closed_economy(network_config())
        control = build_closed_economy(network_config(link_budget_per_tick=0))
        for _ in range(6):
            for world in (active, control):
                await world.step()
                self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")
            self.assertEqual(active.scenario.economy.export_state(), control.scenario.economy.export_state())
        self.assertTrue(attempts(control))
        self.assertTrue(all(r["status"] == "period_budget_exhausted" for r in attempts(control)))
        self.assertFalse(control.snapshot.data["closed_social"]["network"]["applied"])
        self.assertFalse(any(m.sender_id == AUTHOR and m.recipient_id == FOLLOWER for m in control.snapshot.messages))
        self.assertTrue(any(m.sender_id == AUTHOR and m.recipient_id == FOLLOWER for m in active.snapshot.messages))

    async def test_pending_and_active_links_recover_and_export_from_verified_cursors(self):
        config = network_config()
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "run"
            world, store = economic_run.create_run(config, root)
            for tick in (4, 5):
                await economic_run.advance(world, store, until=tick - 1)
                prior = content_hash(world.snapshot)
                book = content_hash(world.scenario.economy.export_state())

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before link marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "before link marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), prior)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), book)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual(marker.state_hash, prior)
                await economic_run.advance(world, store, until=tick)
            await economic_run.advance(world, store, until=6)
            direct = build_closed_economy(network_config(concurrency=4))
            await direct.run(6)
            self.assertEqual(content_hash(world.snapshot), content_hash(direct.snapshot))
            replay = await economic_run.replay_run(root, destination=Path(scratch) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 6))
            for tick, key in ((4, "pending"), (5, "applied")):
                output = Path(scratch) / str(tick)
                exported = economic_run.export_projection(root, destination=output, tick=tick)
                graph = json.loads((output / "relations.json").read_text())
                self.assertEqual(graph["cursor"], exported["cursor"])
                self.assertTrue(any((d["source_id"], d["target_id"], d["effective_tick"])
                                    == (AUTHOR, FOLLOWER, 5) for d in graph["communication_graph"][key]))
