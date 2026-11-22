"""A private wage score can change an authorized, funded rehire decision."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.decisions import HoldIntent
from civitas.contracts.economic import LaborAcceptIntent, PurchaseIntent
from civitas.contracts.observations import Observation
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy
from civitas.scenarios.closed_economy_relations import social_context


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-rehire.json"
WORKER = "household:0001"
EMPLOYER = "firm:00"


def config():
    return json.loads(EXAMPLE.read_text())


def rehire_record(world):
    return next(r for r in world.snapshot.data["closed_social"]["trace"] if r["kind"] == "rehire_choice")


def worker_work(world, tick):
    return [r for r in world.snapshot.data["closed_economy"]["labor_events"]
            if r["worker_id"] == WORKER and r["tick"] == tick]


class RehireChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_offer_and_private_score_lead_to_different_work_by_policy(self):
        raw = config()
        treated = build_closed_economy(raw)
        raw["labor_transfer"]["rehire"].pop("minimum_trust_bps")
        control = build_closed_economy(raw)
        await treated.run(5)
        await control.run(5)
        offer, = [fact for fact in treated.observe(WORKER).market_view
                  if fact.value.get("kind") == "labor_rehire_offer"]
        self.assertIn(offer, control.observe(WORKER).market_view)
        self.assertEqual(rehire_record(treated)["status"], "skipped_low_trust")
        self.assertEqual(rehire_record(control)["status"], "accepted")
        self.assertEqual(rehire_record(treated)["score"], {"a": 2, "b": 1})
        self.assertEqual(len(rehire_record(treated)["score_roots"]), 1)
        left, right = (w.scenario.economy.export_state() for w in (treated, control))
        self.assertEqual({k: v for k, v in left.items() if k not in {"commands", "receipts"}},
                         {k: v for k, v in right.items() if k not in {"commands", "receipts"}})
        self.assertIsNone(treated.snapshot.data["closed_economy"]["rehire_contract"])
        self.assertEqual(worker_work(control, 5), [])
        await treated.step()
        await control.run(7)
        for world, expected in ((treated, []), (control, [(480, 12, 12)])):
            work = [(r["delivered_minutes"], r["accrued_minor"], r["paid_minor"])
                    for r in worker_work(world, 6)]
            self.assertEqual(work, expected)
        self.assertEqual(worker_work(control, 7), [])

    async def test_visible_but_pending_and_nonmatching_scores_do_not_block(self):
        raw = config()
        raw["labor_transfer"]["rehire"]["offer_tick"] = 4
        early = build_closed_economy(raw)
        await early.run(4)
        self.assertEqual((rehire_record(early)["status"], rehire_record(early)["score"]), ("accepted", None))
        self.assertTrue(any(r["kind"] == "wage_trust_update" and r["effective_tick"] == 5
                            for r in early.snapshot.data["closed_social"]["trace"]))

        world = build_closed_economy(config())
        await world.run(5)
        observation = world.observe(WORKER)
        social = social_context(observation)
        facts = [social.model_copy(update={"value": {**social.value,
                        "wage_performance_trust": {"firm:01": {"a": 1, "b": 99}}}})
                 if f.fact_id == social.fact_id else f for f in observation.market_view]
        payload = {**observation.payload(), "market_view": [f.model_dump(mode="json") for f in facts]}
        digest = content_hash(payload)
        choice = await world.policy.decide(Observation(**payload, payload_hash=digest,
                                                       observation_id=f"observation:{digest}"))
        self.assertEqual(len([i for i in choice.economic_intents if isinstance(i, LaborAcceptIntent)]), 1)

    async def test_no_prior_sales_hides_offer_and_guessed_id_is_rejected(self):
        class NoDemandBeforeRehire(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick == 4:
                    intents = [i for i in bundle.economic_intents if not isinstance(i, PurchaseIntent)
                               and not i.intent_id.endswith(":consume-purchase")]
                    if intents:
                        return bundle.model_copy(update={"economic_intents": intents})
                    return bundle.model_copy(update={"economic_intents": [HoldIntent(intent_id=bundle.decision_id + ":hold")]})
                if bundle.tick == 5 and bundle.actor_id == WORKER:
                    guess = LaborAcceptIntent(intent_id="guessed-hidden-offer",
                        offer_id=f"labor-rehire:5:{WORKER}:{EMPLOYER}")
                    return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, guess]})
                return bundle

        settings = ClosedEconomyConfig.model_validate(config())
        world = build_closed_economy(settings, policy=NoDemandBeforeRehire(settings, AddressedRNG(7, 0)))
        await world.run(6)
        self.assertTrue(worker_work(world, 3))
        self.assertEqual(rehire_record(world)["status"], "not_visible")
        receipt, = [r for r in world.snapshot.receipts if r.command_id == "command:guessed-hidden-offer"]
        self.assertEqual(receipt.reason_codes, ["labor_offer_unavailable"])
        self.assertEqual(worker_work(world, 6), [])

    async def test_offer_requires_named_worker_and_actual_observation_citation(self):
        class AlteredPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick != 5:
                    return bundle
                offer_id = f"labor-rehire:5:{WORKER}:{EMPLOYER}"
                if bundle.actor_id == WORKER and self.omit_quote:
                    return bundle.model_copy(update={"evidence_ids": [f for f in bundle.evidence_ids if f != offer_id]})
                if bundle.actor_id == "household:0002":
                    guess = LaborAcceptIntent(intent_id="foreign-rehire", offer_id=offer_id)
                    return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, guess]})
                return bundle

        raw = config()
        raw["labor_transfer"]["rehire"].pop("minimum_trust_bps")
        settings = ClosedEconomyConfig.model_validate(raw)
        policy = AlteredPolicy(settings, AddressedRNG(7, 0))
        policy.omit_quote = True
        world = build_closed_economy(settings, policy=policy)
        await world.run(4)
        before = content_hash(world.snapshot)
        with self.assertRaisesRegex(ValueError, "citing the visible offer"):
            await world.step()
        self.assertEqual(content_hash(world.snapshot), before)
        policy.omit_quote = False
        await world.step()
        for actor in (EMPLOYER, "household:0002"):
            self.assertFalse(any(f.value.get("kind") == "labor_rehire_offer" for f in world.observe(actor).market_view))
        receipt, = [r for r in world.snapshot.receipts if r.command_id == "command:foreign-rehire"]
        self.assertEqual(receipt.reason_codes, ["labor_offer_unavailable"])
        self.assertEqual(rehire_record(world)["status"], "accepted")

    async def test_underfunded_rehire_is_a_funding_rejection_that_preserves_existing_arrears(self):
        raw = json.loads(EXAMPLE.with_name("closed-economy-wage-trust.json").read_text())
        raw["ticks"] = 8
        raw["labor_transfer"]["rehire"] = {"offer_tick": 7}
        world = build_closed_economy(raw)
        await world.run(7)
        decision = rehire_record(world)
        self.assertEqual((decision["status"], decision["reason_codes"]),
                         ("rejected", ["insufficient_labor_funding"]))
        self.assertEqual(decision["score"], {"a": 3, "b": 2})
        await world.step()
        self.assertEqual(worker_work(world, 8), [])
        wages = [w for w in world.scenario.economy.export_state()["wage_obligations"]
                 if w["worker_id"] == WORKER and w["employer_id"] == EMPLOYER]
        self.assertEqual(wages[0]["outstanding_minor"], 23)
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_failure_at_signing_and_first_work_recovers_and_replays(self):
        raw = config()
        raw["labor_transfer"]["rehire"].pop("minimum_trust_bps")
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "run"
            world, store = economic_run.create_run(raw, root)
            for tick in (5, 6):
                await economic_run.advance(world, store, until=tick - 1)
                before = content_hash(world.snapshot)
                book_before = world.scenario.economy.export_state()

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before rehire marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "before rehire marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(world.scenario.economy.export_state(), book_before)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual(marker.state_hash, before)
                await economic_run.advance(world, store, until=tick)
            await economic_run.advance(world, store, until=8)
            direct = build_closed_economy(dict(raw, concurrency=4))
            await direct.run(8)
            self.assertEqual(content_hash(world.snapshot), content_hash(direct.snapshot))
            replay = await economic_run.replay_run(root, destination=Path(scratch) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 8))
