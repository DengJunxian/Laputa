"""Two-hop opportunities enter the ordinary C/D observation and commit path."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.codecs import decode_snapshot
from civitas.contracts.decisions import MessageDraft
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.relations.store import RelationStore
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy
from civitas.scenarios.closed_economy_relations import delivery_disclosure, social_context


AUTHOR = "household:0000"
RECIPIENT = "household:0003"
KIND = "candidate_introduction"


def intro_config(**changes):
    example = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-introductions.json"
    return ClosedEconomyConfig.model_validate(dict(json.loads(example.read_text()), **changes))


def introductions(observation):
    return [message for message in observation.delivered_messages if message.body.get("kind") == KIND]


class CandidateIntroductionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_reuses_one_graph_advance_without_publishing_it_before_feedback(self):
        barriers = {}
        world = build_closed_economy(intro_config(link_budget_per_tick=1),
                                    phase_hook=lambda phase, snapshot: barriers.update({phase.value: snapshot}))
        with patch.object(RelationStore, "advance", autospec=True, side_effect=RelationStore.advance) as advances:
            await world.step()
            advances.assert_called_once()
        original = barriers["OBSERVE"]
        cold = decode_snapshot(original.model_dump(mode="json"))
        self.assertEqual(original.data["closed_social"]["network"]["tick"], 0)
        self.assertEqual(world.snapshot.data["closed_social"]["network"]["tick"], 1)
        self.assertEqual(original.state_hash, cold.state_hash)
        for actor in original.actors:
            self.assertEqual(world.observer.observe(actor, original, []), world.observer.observe(actor, cold, []))
        original_scores = original.data["closed_social"]["current"][AUTHOR]["scores_bps"]
        with self.assertRaises(TypeError):
            original_scores["firm:00"] = 42
        replacement = cold.model_dump(mode="json")
        replacement["data"]["closed_social"]["current"][AUTHOR]["scores_bps"]["firm:00"] = 42
        changed = world.observer.observe(AUTHOR, decode_snapshot(replacement), [])
        self.assertEqual(social_context(changed).value["scores_bps"]["firm:00"], 42)
        self.assertEqual(original_scores["firm:00"], 10000)

    async def test_settled_purchase_yields_one_private_opportunity_across_two_paths(self):
        world = build_closed_economy(intro_config())
        await world.run(3)
        self.assertEqual(introductions(world.observe(RECIPIENT)), [])

        await world.step()
        trace = world.snapshot.data["closed_social"]["trace"]
        opportunities = [row for row in trace if row["kind"] == "introduction_opportunity"
                         and row["observer_id"] == RECIPIENT and row["candidate_id"] == AUTHOR]
        self.assertEqual(len(opportunities), 1)
        opportunity, = opportunities
        self.assertEqual((opportunity["tick"], opportunity["issued_tick"], opportunity["expires_tick"]), (4, 3, 5))
        self.assertEqual({row["intermediary_id"] for row in trace if row["kind"] == "introduction_issued"
                          and row["observer_id"] == RECIPIENT and row["root_claim_id"] == opportunity["root_claim_id"]},
                         {"household:0001", "household:0002"})
        messages = {message.message_id: message for message in world.snapshot.messages}
        source = messages[opportunity["source_message_id"]]
        self.assertEqual((source.sender_id, source.recipient_id), (AUTHOR, opportunity["intermediary_id"]))
        self.assertEqual(source.body["root_claim_id"], opportunity["root_claim_id"])
        receipt, = [r for r in world.snapshot.receipts if r.command_id == source.body["receipt_command_id"]]
        self.assertEqual((receipt.actor_id, receipt.tick, receipt.outcome), (AUTHOR, 1, "purchased"))
        self.assertTrue(receipt.ledger_tx_ids)
        packet = messages[opportunity["message_id"]].body
        self.assertFalse({"cash_minor", "wage_minor", "seller_id", "outcome", "receipt_command_id"} & packet.keys())
        self.assertFalse(any(row["kind"] == "introduction_opportunity" and row["candidate_id"] == AUTHOR
                             and row["observer_id"] == "household:0002" for row in trace))

    async def test_introductions_use_only_the_attention_left_after_direct_disclosure(self):
        world = build_closed_economy(intro_config(attention_limit=2))
        await world.run(4)
        trace = world.snapshot.data["closed_social"]["trace"]
        self.assertTrue(any(row["kind"] == "introduction_issued" for row in trace))
        self.assertFalse(any(row["kind"] == "introduction_opportunity" for row in trace))
        direct = [row for row in trace if row["kind"] == "exposure"
                  and row["tick"] == 4 and row["actor_id"] == RECIPIENT]
        self.assertEqual(len(direct), 2)

    async def test_expired_future_missing_and_hidden_paths_leave_no_opportunity(self):
        world = build_closed_economy(intro_config())
        frozen = {}

        def capture(phase, snapshot):
            if phase.value == "OBSERVE" and snapshot.tick in (3, 4):
                frozen[snapshot.tick] = snapshot

        world.phase_hook = capture
        await world.run(4)
        snapshot = frozen[4]
        view = world.observer.observe(RECIPIENT, snapshot, [])
        target, = [msg for msg in introductions(view) if msg.body["candidate_id"] == AUTHOR]
        paths = [msg for msg in snapshot.messages if msg.body.get("kind") == KIND
                 and msg.recipient_id == RECIPIENT and msg.body["root_claim_id"] == target.body["root_claim_id"]]
        self.assertEqual(len(paths), 2)
        source_ids = {msg.body["source_message_id"] for msg in paths}
        filtered = [m for m in snapshot.messages if m.message_id not in source_ids]
        no_sources = snapshot.model_copy(update={"messages": filtered})
        graph = snapshot.model_dump()["data"]
        graph["closed_social"]["neighbors"][AUTHOR] = []
        hidden_path = snapshot.model_copy(update={"data": graph})
        expired = snapshot.model_copy(update={"tick": 5})
        changed = [m.model_copy(update={"body": {**m.body, "candidate_id": "household:0004"}})
                   if m in paths else m for m in snapshot.messages]
        wrong_origin = snapshot.model_copy(update={"messages": changed})
        for label, state in (("future", frozen[3]), ("missing", no_sources), ("hidden", hidden_path),
                             ("expired", expired), ("wrong_target", wrong_origin)):
            with self.subTest(label=label):
                observed = world.observer.observe(RECIPIENT, state, [])
                self.assertNotIn(target.body["root_claim_id"],
                                 {m.body["root_claim_id"] for m in introductions(observed)})
        self.assertNotIn(target.message_id, world.observer.observe("household:0004", snapshot, []).evidence_ids)

    async def test_consent_is_required_and_an_explicit_repeat_does_not_add_an_opportunity(self):
        class ConsentPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != AUTHOR:
                    return bundle
                drafts = list(bundle.message_drafts)
                evidence = set(bundle.evidence_ids)
                if self.withhold:
                    drafts = [d.model_copy(update={"body": {k: v for k, v in d.body.items()
                                                 if k != "allow_introduction"}})
                              for d in drafts]
                elif bundle.tick == 3:
                    fact = next(f for f in observation.private_memory_refs
                                if f.value.get("outcome") == "purchased" and f.value["tick"] == 1)
                    receipt = fact.value
                    drafts.append(MessageDraft(message_id="aaa-repeated-authorized-disclosure",
                        recipient_id="household:0001",
                        body=delivery_disclosure(receipt["command_id"], receipt["counterparty_id"],
                                                 "purchased", allow_introduction=True)))
                    evidence.add(fact.fact_id)
                return bundle.model_copy(update={"message_drafts": drafts, "evidence_ids": sorted(evidence)})

        config = intro_config()
        for withhold in (False, True):
            with self.subTest(withhold=withhold):
                policy = ConsentPolicy(config, AddressedRNG(7, 0))
                policy.withhold = withhold
                world = build_closed_economy(config, policy=policy)
                await world.run(6)
                trace = world.snapshot.data["closed_social"]["trace"]
                events = [r for r in trace if r["kind"] == "introduction_opportunity"
                          and r["observer_id"] == RECIPIENT and r["candidate_id"] == AUTHOR]
                if withhold:
                    self.assertEqual(events, [])
                else:
                    root, = [r for r in events if r["tick"] == 4]
                    self.assertTrue(any(row["kind"] == "introduction_issued" and row["tick"] == 4
                        and row["observer_id"] == RECIPIENT and row["root_claim_id"] == root["root_claim_id"]
                        for row in trace))
                    self.assertEqual(sum(row["root_claim_id"] == root["root_claim_id"] for row in events), 1)

    async def test_unseen_or_uncited_source_rejects_the_tick_and_can_retry(self):
        class InvalidPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                candidate = next((d for d in bundle.message_drafts
                                  if d.body.get("kind") == KIND and d.recipient_id == RECIPIENT), None)
                if self.invalid and bundle.actor_id == "household:0001" and candidate is not None:
                    if self.invalid == "uncited":
                        return bundle.model_copy(update={"evidence_ids": [key for key in bundle.evidence_ids
                                                     if key != candidate.body["source_message_id"]]})
                    wrong = candidate.model_copy(update={"body": {**candidate.body,
                        "source_message_id": self.unseen_source_id}})
                    return bundle.model_copy(update={"message_drafts": [wrong if d == candidate else d
                                                                         for d in bundle.message_drafts]})
                return bundle

        config = intro_config()
        policy = InvalidPolicy(config, AddressedRNG(7, 0))
        policy.invalid = False
        world = build_closed_economy(config, policy=policy)
        await world.run(2)
        before = content_hash(world.snapshot)
        outsider, = [m for m in world.snapshot.messages if m.sender_id == AUTHOR
                     and m.recipient_id == "household:0002"]
        policy.unseen_source_id = outsider.message_id
        for misuse in ("uncited", "unseen"):
            with self.subTest(misuse=misuse):
                policy.invalid = misuse
                with self.assertRaisesRegex(ValueError, "authorized, visible two-hop path"):
                    await world.step()
                self.assertEqual(content_hash(world.snapshot), before)
        policy.invalid = False
        await world.run(2)
        fresh = build_closed_economy(config)
        await fresh.run(4)
        self.assertEqual(content_hash(world.snapshot), content_hash(fresh.snapshot))

    async def test_failed_issue_and_exposure_recover_and_replay_with_economic_state_intact(self):
        config = intro_config()
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "run"
            world, store = economic_run.create_run(config, root)
            for tick in (3, 4):
                await economic_run.advance(world, store, until=tick - 1)
                prior = content_hash(world.snapshot)
                book = content_hash(world.scenario.economy.export_state())

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before introduction marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "before introduction marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), prior)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), book)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual(marker.state_hash, prior)
                await economic_run.advance(world, store, until=tick)
            await economic_run.advance(world, store, until=6)
            replay = await economic_run.replay_run(root, destination=Path(scratch) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 6))
            for fresh_config in (intro_config(concurrency=4), intro_config(introductions=False)):
                fresh = build_closed_economy(fresh_config)
                await fresh.run(6)
                self.assertEqual(world.scenario.economy.export_state(), fresh.scenario.economy.export_state())
                self.assertEqual(audit_snapshot(fresh.snapshot)["status"], "passed")
                if fresh_config.introductions:
                    self.assertEqual(content_hash(world.snapshot), content_hash(fresh.snapshot))
                else:
                    self.assertFalse(any(row["kind"] == "introduction_opportunity"
                                         for row in fresh.snapshot.data["closed_social"]["trace"]))
