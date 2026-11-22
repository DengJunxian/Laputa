"""Authorized private labor choice and settlement at the world boundary."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.decisions import HoldIntent
from civitas.contracts.economic import LaborAcceptIntent, PurchaseIntent
from civitas.core.rng import AddressedRNG
from civitas.economy import EconomicRuntime
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-labor-switch.json"
WORKER = "household:0001"
INCUMBENT = "firm:00"
ALTERNATIVE = "firm:01"
ALT_ID = f"labor-switch:5:{WORKER}:{ALTERNATIVE}"


def config():
    return json.loads(EXAMPLE.read_text())


def fees(world):
    return [entry for entry in world.scenario.economy.journal if entry.kind == "labor_switch_fee"]


def candidate(world, employer=ALTERNATIVE):
    return next(r for r in world.snapshot.data["closed_social"]["trace"]
                if r["kind"] == "rehire_choice" and r["candidate_employer_id"] == employer)


def build_with_policy(raw, policy):
    settings = ClosedEconomyConfig.model_validate(raw)
    return build_closed_economy(settings, policy=policy(settings, AddressedRNG(settings.root_seed, 0)))


class LaborSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_offers_and_single_fee_on_actual_first_work(self):
        world = build_closed_economy(config())
        await world.run(5)
        self.assertEqual(fees(world), [])
        quotes = [f for f in world.observe(WORKER).market_view
                  if f.value.get("kind") in ("labor_rehire_offer", "labor_switch_offer")]
        self.assertEqual({f.value["employer_id"] for f in quotes}, {INCUMBENT, ALTERNATIVE})
        self.assertTrue(all(f.owner_id == WORKER and not f.public for f in quotes))
        self.assertFalse(any(f.fact_id in {q.fact_id for q in quotes}
                             for f in world.observe("household:0002").market_view))
        self.assertEqual(candidate(world, INCUMBENT)["status"], "skipped_low_trust")
        self.assertIsNone(candidate(world)["score"])
        contract = world.snapshot.data["closed_economy"]["rehire_contract"]
        self.assertEqual(contract["offer"]["employer_id"], ALTERNATIVE)
        await world.step()
        entry, = fees(world)
        self.assertEqual((entry.tick, dict(entry.payload)), (6, {"contract_id": contract["contract_id"],
            "payer_id": WORKER, "recipient_id": INCUMBENT, "amount_minor": 3}))
        work, = [r for r in world.snapshot.data["closed_economy"]["labor_events"]
                 if r["tick"] == 6 and r["worker_id"] == WORKER]
        self.assertEqual((work["employer_id"], work["delivered_minutes"], work["paid_minor"]),
                         (ALTERNATIVE, 480, 12))
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_missing_low_and_uncited_switch_authorizations_do_not_admit_contract(self):
        class AuthorizationPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick != 5 or bundle.actor_id != WORKER:
                    return bundle
                if self.omit_citation:
                    return bundle.model_copy(update={"evidence_ids": [v for v in bundle.evidence_ids if v != ALT_ID]})
                intents = [i.model_copy(update={"max_switch_fee_minor": self.cap}) if isinstance(i, LaborAcceptIntent)
                           else i for i in bundle.economic_intents]
                return bundle.model_copy(update={"economic_intents": intents})

        world = build_with_policy(config(), AuthorizationPolicy)
        world.policy.omit_citation = True
        await world.run(4)
        before = content_hash(world.snapshot)
        with self.assertRaisesRegex(ValueError, "citing the visible offer"):
            await world.step()
        self.assertEqual(content_hash(world.snapshot), before)
        for cap in (None, 2):
            world = build_with_policy(config(), AuthorizationPolicy)
            world.policy.omit_citation, world.policy.cap = False, cap
            await world.run(6)
            self.assertEqual(candidate(world)["reason_codes"], ["labor_switch_fee_not_authorized"])
            self.assertIsNone(world.snapshot.data["closed_economy"]["rehire_contract"])
            self.assertFalse(fees(world))

    async def test_after_goods_cash_check_blocks_a_fee_affordable_at_observation(self):
        control = build_closed_economy(config())
        await control.run(4)
        opening_cash = control.scenario.economy.account_view(WORKER).cash_minor
        raw = config()
        raw["labor_transfer"]["rehire"]["switch_fee_minor"] = opening_cash
        world = build_closed_economy(raw)
        await world.run(5)
        self.assertGreater(opening_cash, 0)
        self.assertLess(world.scenario.economy.account_view(WORKER).available_cash_minor, opening_cash)
        self.assertEqual(candidate(world)["reason_codes"], ["insufficient_labor_switch_fee_cash"])
        await world.step()
        self.assertFalse(fees(world))
        self.assertIsNone(world.snapshot.data["closed_economy"]["rehire_contract"])

    async def test_two_offers_retries_and_foreign_guess_can_only_activate_one_contract(self):
        class RepeatingPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                extra = []
                if bundle.tick == 5 and bundle.actor_id == WORKER:
                    extra = [LaborAcceptIntent(intent_id="duplicate-switch", offer_id=ALT_ID, max_switch_fee_minor=3),
                             LaborAcceptIntent(intent_id="also-incumbent", offer_id=f"labor-rehire:5:{WORKER}:{INCUMBENT}")]
                elif bundle.tick == 5 and bundle.actor_id == "household:0002":
                    extra = [LaborAcceptIntent(intent_id="foreign-switch", offer_id=ALT_ID, max_switch_fee_minor=3)]
                elif bundle.tick == 6 and bundle.actor_id == WORKER:
                    extra = [LaborAcceptIntent(intent_id="expired-switch", offer_id=ALT_ID, max_switch_fee_minor=3)]
                return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, *extra]})

        world = build_with_policy(config(), RepeatingPolicy)
        await world.run(6)
        by_id = {r.command_id: r for r in world.snapshot.receipts}
        self.assertEqual(candidate(world)["status"], "accepted")
        for name, error in (("duplicate-switch", "labor_offer_already_accepted"),
                            ("also-incumbent", "labor_offer_already_accepted"),
                            ("foreign-switch", "labor_offer_unavailable"),
                            ("expired-switch", "labor_offer_unavailable")):
            self.assertEqual(by_id[f"command:{name}"].reason_codes, [error])
        self.assertEqual(len(fees(world)), 1)

    async def test_alternative_needs_its_own_prior_sales_and_is_not_available_by_id_alone(self):
        class HiddenAlternativePolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick == 4 and any(isinstance(i, PurchaseIntent) and i.seller_id == ALTERNATIVE
                                            for i in bundle.economic_intents):
                    intents = [i for i in bundle.economic_intents if not isinstance(i, PurchaseIntent)
                               and not i.intent_id.endswith(":consume-purchase")]
                    return bundle.model_copy(update={"economic_intents": intents or [HoldIntent(intent_id="hold:" + bundle.decision_id)]})
                if bundle.tick == 5 and bundle.actor_id == WORKER:
                    guess = LaborAcceptIntent(intent_id="guess-hidden-switch", offer_id=ALT_ID, max_switch_fee_minor=3)
                    return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, guess]})
                return bundle

        world = build_with_policy(config(), HiddenAlternativePolicy)
        await world.run(6)
        self.assertEqual(candidate(world, INCUMBENT)["status"], "skipped_low_trust")
        self.assertEqual(candidate(world)["status"], "not_visible")
        guess, = [r for r in world.snapshot.receipts if r.command_id == "command:guess-hidden-switch"]
        self.assertEqual(guess.reason_codes, ["labor_offer_unavailable"])
        self.assertEqual(fees(world), [])

    def test_switch_fee_rebuild_and_idempotence_leave_old_wage_claim_intact(self):
        cash = {WORKER: 10, INCUMBENT: 0, ALTERNATIVE: 30}
        book = EconomicRuntime(cash)
        book.begin_tick(1, 0)
        book.accrue_wage("earned-earlier", INCUMBENT, WORKER, 12)
        event = book.settle_labor_switch_fee("new-contract", WORKER, INCUMBENT, 3)
        self.assertEqual(book.settle_labor_switch_fee("new-contract", WORKER, INCUMBENT, 3), event)
        self.assertEqual([(p.entity_id, p.account_code, p.side, p.amount_minor) for p in event.postings], [
            (WORKER, "labor_switch_fee_expense", "debit", 3), (WORKER, "cash", "credit", 3),
            (INCUMBENT, "cash", "debit", 3), (INCUMBENT, "labor_switch_fee_income", "credit", 3)])
        self.assertEqual(book.account_view(WORKER).wage_receivable_minor, 12)
        self.assertEqual(book.account_view(INCUMBENT).wage_payable_minor, 12)
        self.assertEqual(book.account_view(WORKER).cash_minor, 7)
        rebuilt = EconomicRuntime(cash)
        rebuilt.import_state(book.export_state())
        self.assertEqual(rebuilt.export_state(), book.export_state())

    async def test_rollback_and_replay_keep_switch_fee_and_first_work_together(self):
        raw = config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run"
            world, store = economic_run.create_run(raw, path)
            for tick in (5, 6):
                await economic_run.advance(world, store, until=tick - 1)
                before = content_hash(world.snapshot)
                book_before = world.scenario.economy.export_state()

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before labor switch marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "before labor switch marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(world.scenario.economy.export_state(), book_before)
                world, store, marker = economic_run.open_run(path)
                self.assertEqual(marker.state_hash, before)
                await economic_run.advance(world, store, until=tick)
                self.assertEqual(len(fees(world)), int(tick == 6))
            await economic_run.advance(world, store, until=8)
            direct = build_closed_economy(dict(raw, concurrency=4))
            await direct.run(8)
            self.assertEqual(content_hash(world.snapshot), content_hash(direct.snapshot))
            replay = await economic_run.replay_run(path, destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 8))
