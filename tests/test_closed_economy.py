"""Offline integration tests of the actual v4 household/firm feedback loop."""
import asyncio
import unittest
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.decisions import MessageDraft
from civitas.contracts.economic import PurchaseIntent
from civitas.contracts.observations import Message
from civitas.core.rng import AddressedRNG
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy
from civitas.scenarios.closed_economy_relations import (
    SOCIAL_FIELD, appraise_social, materialize_social, outcome_root, social_context,
)
from civitas.scenarios.closed_economy_rules import (
    ActionCapabilities, affordable_quantity, household_budget_minor, production_from_minutes,
)


async def run_with_snapshots(config):
    world = build_closed_economy(config)
    snapshots = [world.snapshot]
    for _ in range(config.ticks):
        await world.step()
        snapshots.append(world.snapshot)
    return world, snapshots


class ExtractedRulesTests(unittest.TestCase):
    def test_extracted_budget_and_feasible_order(self):
        self.assertEqual(household_budget_minor(101, 10, 6000), 60)
        self.assertEqual(household_budget_minor(3, 5, 6000), 0)
        self.assertEqual(affordable_quantity(3, 1, 2, 19, 10), 1)
        with self.assertRaises(ValueError):
            household_budget_minor(True, 0, 5000)
        with self.assertRaises(ValueError):
            affordable_quantity(2, 0, 2, 20, 0)

    def test_extracted_capabilities_and_fractional_labor_carry(self):
        cap = ActionCapabilities.from_config({"disabledActions": ["purchase"]})
        self.assertEqual(cap.filter({"purchase": 1, "consume": 2}), {"consume": 2})
        with self.assertRaises(TypeError):
            ActionCapabilities.from_config({"disabledActions": "purchase"})
        first, carry = production_from_minutes(240, 3)
        second, carry = production_from_minutes(240, 3, carry_numerator=carry)
        self.assertEqual((first, second, carry), (1, 2, 0))

    def test_configuration_rejects_incompatible_recipe(self):
        with self.assertRaises(ValueError):
            ClosedEconomyConfig(wage_minor=10, output_per_worker=4)
        with self.assertRaises(ValueError):
            ClosedEconomyConfig(households=2, network_neighbors=2)


class ResourceRejectionTests(unittest.TestCase):
    def test_competing_stock_shortage_does_not_reduce_fulfillment(self):
        config = ClosedEconomyConfig(ticks=2, households=4, network_neighbors=1,
            output_per_worker=1, target_units_min=2, target_units_max=2, fault_tick=None)
        world, _ = asyncio.run(run_with_snapshots(config))
        rejected = [r for r in world.snapshot.receipts if "insufficient_goods" in r.reason_codes]
        self.assertTrue(rejected)
        self.assertFalse(any(r.quality_failure for r in world.snapshot.receipts))
        state = materialize_social(world.snapshot.data[SOCIAL_FIELD], 2)
        self.assertTrue(all(v == 10000 for a in state.current.values() for v in a.scores_bps.values()))

    def test_overbudget_proposal_is_rejection_not_bad_supplier_evidence(self):
        class OverbudgetPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                return bundle.model_copy(update={"economic_intents": [
                    intent.model_copy(update={"quantity_units": 10000})
                    if isinstance(intent, PurchaseIntent) else intent
                    for intent in bundle.economic_intents]})
        config = ClosedEconomyConfig(ticks=2, households=4, network_neighbors=1, fault_tick=None)
        world = build_closed_economy(config,
            policy=OverbudgetPolicy(config, AddressedRNG(config.root_seed, config.replica)))
        asyncio.run(world.run(2))
        self.assertTrue(any("insufficient_cash" in r.reason_codes for r in world.snapshot.receipts))
        self.assertFalse(any(r.quality_failure for r in world.snapshot.receipts))
        state = materialize_social(world.snapshot.data[SOCIAL_FIELD], 2)
        self.assertTrue(all(v == 10000 for a in state.current.values() for v in a.scores_bps.values()))


class ObservationBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_social_materialization_per_barrier_and_fresh_state_after_retry(self):
        config = ClosedEconomyConfig(ticks=2, households=4, network_neighbors=1, fault_tick=None)
        world = build_closed_economy(config)
        clean = build_closed_economy(config)
        project = world.scenario.project_observation

        def fail_during_observe(context, actor):
            if actor == "household:0001":
                raise RuntimeError("interrupted observation")
            return project(context, actor)

        with patch("civitas.scenarios.closed_economy_relations.materialize_social",
                   wraps=materialize_social) as reads:
            with patch.object(world.scenario, "project_observation", side_effect=fail_during_observe):
                with self.assertRaisesRegex(RuntimeError, "interrupted observation"):
                    await world.step()
            self.assertEqual(world.snapshot.tick, 0)
            self.assertEqual(reads.call_count, 1)
            await world.step()
            await world.step()
            self.assertEqual(reads.call_count, 5)
        await clean.run(2)
        self.assertEqual(content_hash(world.snapshot), content_hash(clean.snapshot))


class ClosedEconomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {}
        for mode in ("adaptive", "static", "none"):
            cls.runs[mode] = asyncio.run(run_with_snapshots(
                ClosedEconomyConfig(social_mode=mode)))

    def test_real_wages_production_purchase_consumption_and_conservation(self):
        world, snapshots = self.runs["adaptive"]
        m = world.scenario.metrics(world._context(world.snapshot))
        initial_cash = sum(world.scenario.economy.export_state()["initial_cash"].values())
        self.assertEqual(m["cash_total_minor"], initial_cash)
        self.assertEqual(m["produced_units_total"], 8 * 8 * 4)
        self.assertEqual(m["wage_paid_minor_total"], 8 * 8 * 12)
        self.assertGreater(m["consumed_units_total"], 0)
        self.assertEqual(m["goods_total_units"],
            m["produced_units_total"] - m["consumed_units_total"] - m["scrapped_units_total"])
        self.assertTrue(all(v >= 0 for v in m["inventory_units_by_actor"].values()))
        kinds = {entry.kind for entry in world.scenario.economy.journal}
        self.assertTrue({"opening", "wage_accrual", "wage_payment", "production",
                         "purchase", "consumption", "delivery_failure"} <= kinds)
        self.assertEqual(len(world.participants), 1)
        self.assertEqual(world.participants[0].participant_id, "economic_runtime")
        prices = [s.data["closed_economy"]["last_macro"].get("price_minor_by_seller")
                  for s in snapshots[1:]]
        self.assertNotEqual(prices[0], prices[-1])

    def test_fault_refund_and_t_plus_one_private_feedback(self):
        world, snapshots = self.runs["adaptive"]
        faults = [r for r in world.snapshot.receipts if r.quality_failure]
        self.assertEqual(len(faults), 1)
        receipt = faults[0]
        self.assertEqual((receipt.tick, receipt.status, receipt.outcome),
                         (3, "accepted", "delivery_failed_refunded"))
        self.assertGreater(receipt.refunded_minor, 0)
        self.assertGreater(receipt.accepted_quantity, 0)
        self.assertEqual((receipt.cash_delta_minor, receipt.goods_delta_units,
                          receipt.executed_quantity), (0, 0, 0))
        self.assertTrue(receipt.ledger_tx_ids)
        actor, seller = receipt.actor_id, receipt.counterparty_id
        before = world.observer.observe(actor, snapshots[3], [])
        after = world.observer.observe(actor, snapshots[4], [])
        self.assertEqual(social_context(before).value["scores_bps"][seller], 10000)
        self.assertLess(social_context(after).value["scores_bps"][seller], 10000)
        memory_id = "memory:" + receipt.command_id
        self.assertNotIn(memory_id, before.evidence_ids)
        self.assertIn(memory_id, after.evidence_ids)

    def test_normal_rejections_are_not_negative_fulfillment_evidence(self):
        world, _ = self.runs["adaptive"]
        rejections = [r for r in world.snapshot.receipts if r.status == "rejected"]
        self.assertGreater(len(rejections), 0)  # failed purchase's unavailable consumption
        negative = [e for e in world.snapshot.data[SOCIAL_FIELD]["trace"]
                    if e.get("kind") == "personal_fulfillment"
                    and e["new_score_bps"] < e["old_score_bps"]]
        self.assertEqual(len(negative), 1)
        self.assertEqual(negative[0]["outcome"], "delivery_failed_refunded")
        self.assertFalse({outcome_root(r.command_id) for r in rejections}
                         & {e["root_claim_id"] for e in negative})

    def test_exposure_and_consumption_are_distinct_and_roots_not_reused(self):
        adaptive, _ = self.runs["adaptive"]
        static, _ = self.runs["static"]
        trace = adaptive.snapshot.data[SOCIAL_FIELD]["trace"]
        uses = [(e["actor_id"], e["root_claim_id"]) for e in trace
                if e.get("kind") in {"peer_consumption", "personal_fulfillment"}]
        self.assertEqual(len(uses), len(set(uses)))
        static_trace = static.snapshot.data[SOCIAL_FIELD]["trace"]
        self.assertTrue(any(e.get("kind") == "exposure" for e in static_trace))
        self.assertFalse(any(e.get("kind") == "peer_consumption" for e in static_trace))
        for state in materialize_social(static.snapshot.data[SOCIAL_FIELD], 8).current.values():
            self.assertTrue(all(v == 10000 for v in state.scores_bps.values()))
            self.assertEqual(list(state.consumed_roots), [])
        none, _ = self.runs["none"]
        self.assertEqual(len(none.snapshot.messages), 0)
        self.assertTrue(any(e.get("kind") == "personal_fulfillment"
                            for e in none.snapshot.data[SOCIAL_FIELD]["trace"]))

    def test_reforwarded_same_root_does_not_become_new_information(self):
        world, _ = self.runs["adaptive"]
        message = world.snapshot.messages[0]
        duplicate = message.model_copy(update={"message_id": message.message_id + ":repeat"})
        snapshot = world.snapshot.model_copy(update={"messages": [*world.snapshot.messages, duplicate]})
        obs = world.observer.observe(message.recipient_id, snapshot, [])
        self.assertIn(duplicate.message_id, {m.message_id for m in obs.delivered_messages})
        _, used = appraise_social(obs, world.scenario.config)
        self.assertNotIn(message.body["root_claim_id"], {u["root_claim_id"] for u in used})

    def test_observer_accepts_only_the_exact_visible_receipt_disclosure(self):
        world, snapshots = self.runs["adaptive"]
        snapshot = snapshots[3]
        valid = next(m for m in snapshot.messages if m.visible_at.tick == 3)
        variants = [
            {"body": {**valid.body, "cash_minor": 100}},
            {"body": {**valid.body, "receipt_command_id": ["invalid"]}},
            {"body": {**valid.body, "seller_id": "someone-else"}},
            {"body": {k: v for k, v in valid.body.items() if k != "outcome"}},
            {"sender_id": valid.recipient_id},
            {"created_at": {"tick": 1, "phase": "FEEDBACK"}},
            {"visible_at": {"tick": 4, "phase": "OBSERVE"}},
        ]
        invalid = [Message.model_validate({**valid.model_dump(), **changes, "message_id": f"bad:{i}"})
                   for i, changes in enumerate(variants)]
        corrupted = snapshot.model_copy(update={"messages": [*invalid, valid]})
        observation = world.observer.observe(valid.recipient_id, corrupted, [])
        self.assertEqual([m.message_id for m in observation.delivered_messages], [valid.message_id])

    def test_unverified_draft_rolls_back_and_valid_retry_matches(self):
        class InvalidDisclosurePolicy(ClosedEconomyPolicy):
            error = None

            async def decide(self, observation):
                bundle = await super().decide(observation)
                if not bundle.message_drafts or self.error is None:
                    return bundle
                draft = bundle.message_drafts[0]
                if self.error == "missing_citation":
                    evidence = "memory:" + draft.body["receipt_command_id"]
                    return bundle.model_copy(update={"evidence_ids": [
                        item for item in bundle.evidence_ids if item != evidence]})
                changes = ({"recipient_id": bundle.actor_id} if self.error == "non_neighbor"
                           else {"body": {**draft.body, "cash_minor": 100}})
                invalid = MessageDraft.model_validate({**draft.model_dump(), **changes})
                return bundle.model_copy(update={"message_drafts": [invalid, *bundle.message_drafts[1:]]})

        config = ClosedEconomyConfig(ticks=3, fault_tick=None)
        policy = InvalidDisclosurePolicy(config, AddressedRNG(config.root_seed, config.replica))
        world = build_closed_economy(config, policy=policy)
        asyncio.run(world.step())
        world_hash = content_hash(world.snapshot)
        economy_hash = content_hash(world.scenario.economy.export_state())
        for error in ("private_field", "missing_citation", "non_neighbor"):
            with self.subTest(error=error):
                policy.error = error
                with self.assertRaisesRegex(ValueError, "Unauthorized fulfillment disclosure"):
                    asyncio.run(world.step())
                self.assertEqual(content_hash(world.snapshot), world_hash)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), economy_hash)
        policy.error = None
        asyncio.run(world.step())
        fresh = build_closed_economy(config)
        asyncio.run(fresh.run(2))
        self.assertEqual(content_hash(world.snapshot), content_hash(fresh.snapshot))

    def test_same_tick_duplicate_marks_only_the_message_used(self):
        class DuplicateDisclosurePolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick == 2 and bundle.message_drafts:
                    draft = bundle.message_drafts[0]
                    duplicate = draft.model_copy(update={"message_id": draft.message_id + ":repeat"})
                    return bundle.model_copy(update={"message_drafts": [*bundle.message_drafts, duplicate]})
                return bundle

        config = ClosedEconomyConfig(ticks=3, fault_tick=None)
        policy = DuplicateDisclosurePolicy(config, AddressedRNG(config.root_seed, config.replica))
        world = build_closed_economy(config, policy=policy)
        asyncio.run(world.run(3))
        trace = world.snapshot.data[SOCIAL_FIELD]["trace"]
        repeat_ids = {m.message_id for m in world.snapshot.messages if m.message_id.endswith(":repeat")}
        self.assertTrue(repeat_ids)
        for message_id in repeat_ids:
            exposures = {e["message_id"]: e["consumed"] for e in trace
                         if e["kind"] == "exposure" and e["message_id"] in {
                             message_id, message_id.removesuffix(":repeat")}}
            self.assertEqual(exposures, {message_id.removesuffix(":repeat"): True, message_id: False})

    def test_permissions_hide_other_accounts_and_private_state(self):
        world, _ = self.runs["adaptive"]
        for actor in world.scenario.households:
            obs = world.observe(actor)
            self.assertEqual({f.owner_id for f in obs.account_view}, {actor})
            self.assertTrue(all(f.owner_id == actor for f in obs.private_memory_refs))
            self.assertTrue(all(f.owner_id == actor or f.public for f in obs.market_view))
            social = social_context(obs).value
            self.assertNotIn("current", social)  # no other households' score map
            self.assertNotIn("active_fault_seller", str([f.value for f in obs.market_view]))
            self.assertTrue(all(m.recipient_id == actor for m in obs.delivered_messages))

    def test_heterogeneity_paired_ablations_and_actual_social_choice_path(self):
        adaptive, _ = self.runs["adaptive"]
        static, _ = self.runs["static"]
        none, _ = self.runs["none"]
        hashes = {w.snapshot.data["closed_economy"]["population_hash"]
                  for w in (adaptive, static, none)}
        self.assertEqual(len(hashes), 1)
        population = adaptive.snapshot.data["closed_economy"]["population"]
        self.assertGreater(len({p["basket_share_bps"] for p in population.values()}), 1)
        self.assertGreater(len({p["initial_cash_minor"] for p in population.values()}), 1)
        self.assertEqual(adaptive.snapshot.data["closed_economy"]["fault_trace"],
                         static.snapshot.data["closed_economy"]["fault_trace"])
        at = adaptive.snapshot.data[SOCIAL_FIELD]["trace"]
        nt = none.snapshot.data[SOCIAL_FIELD]["trace"]
        fault = next(r for r in adaptive.snapshot.receipts if r.quality_failure)
        failed_root = outcome_root(fault.command_id)
        listeners = {e["actor_id"] for e in at if e.get("kind") == "peer_consumption"
                     and e["root_claim_id"] == failed_root}
        self.assertTrue(listeners)
        ac = {(e["tick"], e["actor_id"]): e for e in at if e.get("kind") == "decision"}
        nc = {(e["tick"], e["actor_id"]): e for e in nt if e.get("kind") == "decision"}
        changed = [key for key in ac if key[1] in listeners and key in nc
                   and ac[key]["chosen_seller"] != nc[key]["chosen_seller"]]
        self.assertTrue(changed, "Peer evidence must change a later choice, not just a diagnostic")
        self.assertTrue(any(ac[k]["effective_scores_bps"] != nc[k]["effective_scores_bps"]
                            for k in changed))

    def test_capability_ablation_uses_same_economic_engine(self):
        config = ClosedEconomyConfig(ticks=2, households=4, network_neighbors=1,
            fault_tick=None, disabled_actions=["purchase", "quote"])
        world, _ = asyncio.run(run_with_snapshots(config))
        self.assertFalse(any(r.outcome == "purchased" for r in world.snapshot.receipts))
        self.assertTrue(any(e.kind == "production" for e in world.scenario.economy.journal))

    def test_concurrency_preserves_state_and_feedback_rollback(self):
        config = ClosedEconomyConfig(ticks=3, households=4, network_neighbors=1, fault_tick=2)
        one, _ = asyncio.run(run_with_snapshots(config))
        many, _ = asyncio.run(run_with_snapshots(config.model_copy(update={"concurrency": 4})))
        self.assertEqual(content_hash(one.snapshot), content_hash(many.snapshot))
        failed = build_closed_economy(config)
        before_world = content_hash(failed.snapshot)
        before_economy = content_hash(failed.scenario.economy.export_state())

        def fail_after_feedback(phase, snapshot):
            if phase.value == "FEEDBACK":
                raise RuntimeError("injected social feedback failure")

        failed.phase_hook = fail_after_feedback
        with self.assertRaisesRegex(RuntimeError, "injected"):
            asyncio.run(failed.step())
        self.assertEqual(content_hash(failed.snapshot), before_world)
        self.assertEqual(content_hash(failed.scenario.economy.export_state()), before_economy)
        failed.phase_hook = None
        asyncio.run(failed.step())
        fresh = build_closed_economy(config)
        asyncio.run(fresh.step())
        self.assertEqual(content_hash(failed.snapshot), content_hash(fresh.snapshot))


if __name__ == "__main__":
    unittest.main()
