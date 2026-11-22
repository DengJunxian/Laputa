"""Journal-backed labor evidence at the ordinary observation and run entry points."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.decisions import MessageDraft
from civitas.contracts.economic import PurchaseIntent
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy


WORKER = "household:0001"
EMPLOYER = "firm:00"
NEIGHBOR = "household:0002"
KIND = "labor_contract_outcome"


def outcome_config(*, cancel=None, **overrides):
    path = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-labor-outcomes.json"
    fields = json.loads(path.read_text())
    transfer = fields["labor_transfer"]
    transfer["shared_payroll"]["payroll_window"]["cancel_second_wage_slot"] = cancel
    return ClosedEconomyConfig.model_validate(dict(fields, **overrides))


def outcomes(observation):
    return [f.value for f in observation.private_memory_refs if f.value.get("kind") == KIND]


class LaborContractOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_payments_are_distinct_private_results_mature_at_t_plus_one(self):
        world = build_closed_economy(outcome_config())
        await world.run(4)
        self.assertEqual(outcomes(world.observe(WORKER)), [])
        await world.step()
        first, = outcomes(world.observe(WORKER))
        self.assertEqual(outcomes(world.observe(EMPLOYER)), [first])
        self.assertEqual(outcomes(world.observe(NEIGHBOR)), [])
        self.assertEqual((first["due_tick"], first["outcome_tick"], first["outcome"]), (4, 4, "paid"))
        self.assertEqual((first["delivered_minutes"], first["accrued_minor"], first["paid_minor"]), (480, 12, 12))
        self.assertEqual(first["ledger_tx_ids"],
            ["econ:labor:4:household:0001:accrual", "econ:labor:4:household:0001:payment"])
        await world.step()
        result = {record["due_tick"]: record for record in outcomes(world.observe(WORKER))}
        self.assertEqual(result[4], first)
        self.assertEqual(result[5]["contract_id"], first["contract_id"])
        self.assertNotEqual(result[5]["target_id"], first["target_id"])
        self.assertNotEqual(result[5]["root_claim_id"], first["root_claim_id"])

    async def test_each_party_consumes_each_root_only_once_after_rereading(self):
        class RereadingPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != WORKER or bundle.tick < 6:
                    return bundle
                old = next(f for f in observation.private_memory_refs if f.value.get("kind") == KIND
                           and f.value["due_tick"] == 4)
                prior = {"fact_id": old.fact_id, **{key: old.value[key]
                         for key in ("root_claim_id", "target_id", "outcome")}}
                patch = dict(bundle.belief_patch)
                patch["labor_outcome_assessment"] = [*patch.get("labor_outcome_assessment", []), prior]
                return bundle.model_copy(update={"belief_patch": patch,
                    "evidence_ids": sorted(set(bundle.evidence_ids) | {old.fact_id})})

        config = outcome_config(ticks=7, social_mode="none")
        world = build_closed_economy(config, policy=RereadingPolicy(config, AddressedRNG(7, 0)))
        await world.run(7)
        decisions = [bundle for bundle in world.snapshot.decisions if bundle.actor_id == WORKER and bundle.tick >= 5]
        repeated_fact, = decisions[0].belief_patch["labor_outcome_assessment"]
        for bundle in decisions:
            self.assertIn(repeated_fact["fact_id"], bundle.evidence_ids)
        self.assertEqual(len(outcomes(world.observe(WORKER))), 2)
        consumed = [row for row in world.snapshot.data["closed_social"]["trace"]
                    if row["kind"] == "labor_outcome_consumption"]
        self.assertEqual(sorted((row["actor_id"], row["tick"], row["outcome"]) for row in consumed), [
            (EMPLOYER, 5, "paid"), (EMPLOYER, 6, "paid"), (WORKER, 5, "paid"), (WORKER, 6, "paid")])

    async def test_cancellation_records_no_work_and_survives_disabling_social_access(self):
        results = []
        for social_mode, neighbors in (("adaptive", 1), ("none", 0)):
            world = build_closed_economy(outcome_config(cancel=1, social_mode=social_mode,
                                                       network_neighbors=neighbors))
            await world.run(6)
            evidence = {r["due_tick"]: r for r in outcomes(world.observe(WORKER))}
            self.assertEqual(evidence[4]["outcome"], "paid")
            cancellation = evidence[5]
            self.assertEqual((cancellation["outcome_tick"], cancellation["outcome"]),
                             (4, "cancelled_before_work"))
            self.assertEqual([cancellation[key] for key in ("delivered_minutes", "accrued_minor", "paid_minor",
                                                           "outstanding_after_minor")], [0, 0, 0, None])
            journal = {entry.tx_id: entry for entry in world.scenario.economy.journal}
            self.assertEqual([journal[tx].kind for tx in cancellation["ledger_tx_ids"]],
                             ["payroll_reserve", "payroll_release"])
            self.assertEqual(outcomes(world.observe(NEIGHBOR)), [])
            results.append((evidence, world.scenario.economy.export_state()))
            self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")
        self.assertEqual(*results)

    async def test_partial_payment_preserves_the_earned_arrears_in_its_own_domain(self):
        class LowLiquidityPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != EMPLOYER or bundle.tick != 5:
                    return bundle
                quote = next(f.value for f in observation.market_view
                             if f.value.get("kind") == "goods_quote" and f.value["seller_id"] == "firm:01")
                budget = observation.account_view[0].value["available_cash_minor"]
                qty = min(budget // quote["unit_price_minor"], quote["available_units"])
                purchase = PurchaseIntent(intent_id="arrears-drawdown", seller_id="firm:01",
                    quote_id=quote["quote_id"], max_unit_price_minor=quote["unit_price_minor"], quantity_units=qty)
                return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, purchase]})

        config = ClosedEconomyConfig(initial_firm_cash_minor=160, labor_transfer={
            "worker_index": 1, "additional_candidate_indices": [3, 5], "employer_index": 0,
            "offer_tick": 4, "duration_ticks": 2, "min_previous_sales_units": 1, "outcome_evidence": True,
        })
        world = build_closed_economy(config, policy=LowLiquidityPolicy(config, AddressedRNG(7, 0)))
        await world.run(7)
        results = {r["due_tick"]: r for r in outcomes(world.observe(EMPLOYER))}
        first, second = results[5], results[6]
        self.assertEqual((first["outcome"], first["paid_minor"]), ("paid", 12))
        self.assertEqual((second["outcome"], second["delivered_minutes"], second["accrued_minor"]),
                         ("arrears", 480, 12))
        claims = {(r["employer_id"], r["worker_id"]): r["outstanding_minor"]
                  for r in world.scenario.economy.export_state()["wage_obligations"]}
        self.assertGreater(second["outstanding_after_minor"], 0)
        self.assertEqual(12 - second["paid_minor"], second["outstanding_after_minor"])
        self.assertEqual(claims[EMPLOYER, second["worker_id"]], second["outstanding_after_minor"])

    async def test_forged_targets_foreign_and_future_facts_and_wage_disclosure_are_rejected(self):
        class InvalidPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if observation.actor_id != self.actor:
                    return bundle
                if self.misuse == "target":
                    patch = dict(bundle.belief_patch)
                    assessment, = patch["labor_outcome_assessment"]
                    patch["labor_outcome_assessment"] = [{**assessment, "target_id": "different-obligation"}]
                    return bundle.model_copy(update={"belief_patch": patch})
                if self.misuse == "share":
                    fact, = outcomes(observation)
                    draft = MessageDraft(message_id="misuse-private-wage", recipient_id=NEIGHBOR, body=fact)
                    return bundle.model_copy(update={"message_drafts": [*bundle.message_drafts, draft]})
                return bundle.model_copy(update={"evidence_ids": [*bundle.evidence_ids, self.cite]})

        config = outcome_config()
        source = build_closed_economy(config)
        await source.run(5)
        worker_facts = {f.value["due_tick"]: f.fact_id for f in source.snapshot.memories
                        if f.owner_id == WORKER and f.value.get("kind") == KIND}
        for mode, actor, cite_tick, message in (
            ("foreign", NEIGHBOR, 4, "unavailable evidence"),
            ("future", WORKER, 5, "unavailable evidence"),
            ("target", WORKER, 4, "matching visible evidence"),
            ("share", WORKER, 4, "private to the contract parties"),
        ):
            with self.subTest(mode=mode):
                world = build_closed_economy(config)
                await world.run(4)
                before = content_hash(world.snapshot)
                override = InvalidPolicy(config, AddressedRNG(7, 0))
                override.misuse, override.actor, override.cite = mode, actor, worker_facts[cite_tick]
                world.policy = override
                with self.assertRaisesRegex(ValueError, message):
                    await world.step()
                self.assertEqual(content_hash(world.snapshot), before)

    async def test_fault_recovery_preserves_projected_facts_and_consumption_at_the_commit_cursor(self):
        config = outcome_config(cancel=1)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "run"
            world, store = economic_run.create_run(config, root)
            for tick in (4, 5):
                await economic_run.advance(world, store, until=tick - 1)
                before = content_hash(world.snapshot)
                book = content_hash(world.scenario.economy.export_state())

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before labor outcome marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "before labor outcome marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), book)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual(marker.state_hash, before)
                await economic_run.advance(world, store, until=tick)
            await economic_run.advance(world, store, until=6)
            direct = build_closed_economy(outcome_config(cancel=1, concurrency=4))
            await direct.run(6)
            self.assertEqual(content_hash(direct.snapshot), content_hash(world.snapshot))
            replay = await economic_run.replay_run(root, destination=Path(scratch) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 6))
            export = Path(scratch) / "export"
            economic_run.export_projection(root, destination=export, tick=4)
            table = json.loads((export / "scenario.json").read_text())
            self.assertEqual(table["cursor"]["tick"], 4)
            self.assertEqual([(r["due_tick"], r["outcome"]) for r in table["contract_outcomes"]],
                             [(4, "paid"), (5, "cancelled_before_work")])
