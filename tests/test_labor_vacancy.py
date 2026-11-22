"""One vacancy shares its capacity among privately eligible candidates."""
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.economic import LaborApplyIntent, PurchaseIntent
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy


def vacancy_config(*, transfer=None, **changes):
    return ClosedEconomyConfig(labor_transfer={
        "worker_index": 1, "additional_candidate_indices": [3, 5], "employer_index": 0,
        "offer_tick": 4, "duration_ticks": 2, "min_previous_sales_units": 1,
        **(transfer or {}),
    }, **changes)


class LaborVacancyTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_opportunity_ranks_three_applicants_and_pays_one(self):
        serial = build_closed_economy(vacancy_config())
        parallel = build_closed_economy(vacancy_config(concurrency=4))
        visible = set()

        def capture(phase, snapshot):
            if phase.value == "OBSERVE" and snapshot.tick == 4:
                for actor in serial.scenario.actors:
                    facts = serial.scenario.project_observation(serial._context(snapshot), actor)
                    observation = serial.observer.observe(actor, snapshot, facts)
                    if any(f.value.get("kind") == "labor_vacancy" for f in observation.market_view):
                        visible.add(actor)

        serial.phase_hook = capture
        await serial.run(5)
        await parallel.run(5)
        self.assertEqual(content_hash(serial.snapshot), content_hash(parallel.snapshot))
        self.assertEqual(visible, {"household:0001", "household:0003", "household:0005"})
        receipts = [r for r in serial.snapshot.receipts if r.unit == "labor_minutes"]
        self.assertEqual([r.actor_id for r in receipts],
            ["household:0003", "household:0001", "household:0005"])
        self.assertEqual(receipts[0].outcome, "labor_offer_accepted")
        self.assertTrue(all(list(r.reason_codes) == ["labor_vacancy_filled"] for r in receipts[1:]))
        scenario = serial.snapshot.data["closed_economy"]
        closure = scenario["vacancy_closure"]
        self.assertEqual(closure["status"], "filled")
        self.assertEqual(list(closure["applicant_ids"]), [r.actor_id for r in receipts])
        self.assertEqual(scenario["labor_contract"]["contract_id"], receipts[0].command_id)
        self.assertEqual(closure["first_payroll_minor"], 60)
        for receipt in receipts:
            worker = receipt.actor_id
            labor = [e for e in scenario["labor_events"] if e["worker_id"] == worker and e["tick"] in (4, 5)]
            employer = "firm:00" if worker == receipts[0].actor_id else "firm:01"
            self.assertEqual([(e["employer_id"], e["paid_minor"]) for e in labor], [("firm:01", 12), (employer, 12)])
        self.assertEqual(audit_snapshot(serial.snapshot)["status"], "passed")

    async def test_late_ineligible_and_duplicate_applications_cannot_claim_a_seat(self):
        class ProbingPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                actor, tick = bundle.actor_id, bundle.tick
                if (tick, actor) == (4, "household:0005"):
                    return bundle.model_copy(update={"economic_intents": [
                        i for i in bundle.economic_intents if not isinstance(i, LaborApplyIntent)]})
                if (tick, actor) in {(3, "household:0001"), (4, "household:0003"),
                                      (4, "household:0000"), (5, "household:0001")}:
                    probe = LaborApplyIntent(intent_id=f"{bundle.decision_id}:probe",
                        vacancy_id="labor-vacancy:4:firm:00")
                    return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, probe]})
                return bundle

        config = vacancy_config()
        world = build_closed_economy(config, policy=ProbingPolicy(config, AddressedRNG(7, 0)))
        await world.run(7)
        receipts = [r for r in world.snapshot.receipts if r.unit == "labor_minutes"]
        self.assertCountEqual([(r.tick, r.actor_id, list(r.reason_codes)) for r in receipts],
            [(3, "household:0001", ["labor_vacancy_unavailable"]),
             (4, "household:0000", ["labor_vacancy_unavailable"]),
             (4, "household:0003", []),
             (4, "household:0003", ["duplicate_labor_application"]),
             (4, "household:0001", ["labor_vacancy_filled"]),
             (5, "household:0001", ["labor_vacancy_unavailable"])])
        state = world.snapshot.data["closed_economy"]
        self.assertEqual(list(state["vacancy_closure"]["applicant_ids"]), ["household:0003", "household:0001"])
        winner_work = [e["tick"] for e in state["labor_events"] if e["worker_id"] == "household:0003"]
        self.assertEqual(winner_work, [1, 2, 3, 4, 5, 6])

    async def test_no_demand_no_applications_and_no_cash_close_without_contract(self):
        cases = [
            ("not_opened", {"min_previous_sales_units": 99}, {}),
            ("unfilled", {}, {"disabled_actions": ["apply_labor_vacancy"]}),
            ("unfunded", {"offer_tick": 2}, {"initial_firm_cash_minor": 0, "initial_price_minor": 6}),
        ]
        for status, transfer, config in cases:
            with self.subTest(status=status):
                world = build_closed_economy(vacancy_config(transfer=transfer, **config))
                await world.run(5)
                state = world.snapshot.data["closed_economy"]
                closure = state["vacancy_closure"]
                self.assertEqual(closure["status"], status)
                self.assertIsNone(state["labor_contract"])
                applications = [r for r in world.snapshot.receipts if r.unit == "labor_minutes"]
                self.assertEqual(len(applications), 3 if status == "unfunded" else 0)
                for receipt in applications:
                    self.assertEqual(list(receipt.reason_codes), ["insufficient_labor_funding"])
                self.assertTrue(all(e["employer_id"] == "firm:01" for e in state["labor_events"]
                                    if e["worker_id"] in {"household:0001", "household:0003", "household:0005"}))
                self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_same_batch_purchases_reduce_funding_and_later_arrears_stay_in_the_book(self):
        class PurchasingFirmPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != "firm:00" or bundle.tick != self.spend_tick:
                    return bundle
                quote = next(f.value for f in observation.market_view
                             if f.value.get("kind") == "goods_quote" and f.value["seller_id"] == "firm:01")
                budget = observation.account_view[0].value["available_cash_minor"]
                quantity = min(budget // quote["unit_price_minor"], quote["available_units"])
                purchase = PurchaseIntent(intent_id="payroll-drawdown", seller_id="firm:01",
                    quote_id=quote["quote_id"], quantity_units=quantity, max_unit_price_minor=quote["unit_price_minor"])
                return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, purchase]})

        for spend_tick in (4, 5):
            with self.subTest(spend_tick=spend_tick):
                config = vacancy_config(initial_firm_cash_minor=160)
                policy = PurchasingFirmPolicy(config, AddressedRNG(7, 0))
                policy.spend_tick = spend_tick
                world = build_closed_economy(config, policy=policy)
                await world.run(4)
                previous_claims = world.scenario.economy.export_state()["wage_obligations"]
                await world.run(2)
                state = world.snapshot.data["closed_economy"]
                closure = state["vacancy_closure"]
                if spend_tick == 4:
                    self.assertEqual(closure["status"], "unfunded")
                    self.assertLess(closure["available_cash_minor"], closure["first_payroll_minor"])
                    self.assertIsNone(state["labor_contract"])
                else:
                    self.assertEqual(closure["status"], "filled")
                    winner = state["labor_contract"]["offer"]["worker_id"]
                    labor = [e for e in state["labor_events"] if e["worker_id"] == winner and e["tick"] >= 5]
                    self.assertEqual([(e["tick"], e["accrued_minor"]) for e in labor], [(5, 12), (6, 12)])
                    self.assertEqual(labor[0]["paid_minor"], 12)
                    self.assertLess(labor[1]["paid_minor"], 12)
                    obligations = {(w["employer_id"], w["worker_id"]): w["outstanding_minor"]
                                   for w in world.scenario.economy.export_state()["wage_obligations"]}
                    old_claim, = [w["outstanding_minor"] for w in previous_claims
                                  if (w["employer_id"], w["worker_id"]) == ("firm:01", winner)]
                    claimant = world.scenario.economy.account_view(winner)
                    employer = world.scenario.economy.account_view("firm:00")
                    self.assertEqual(obligations["firm:00", winner], 12 - labor[1]["paid_minor"])
                    self.assertEqual(obligations["firm:01", winner], old_claim)
                    self.assertEqual(claimant.wage_receivable_minor, old_claim + obligations["firm:00", winner])
                    self.assertEqual(employer.wage_payable_minor,
                                     sum(v for (firm, _), v in obligations.items() if firm == "firm:00"))
                self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_vacancy_and_payroll_recover_after_pre_marker_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "vacancy"
            world, store = economic_run.create_run(vacancy_config(concurrency=4), root)
            await economic_run.advance(world, store, until=3)

            def abort_commit(phase, snapshot):
                if phase.value == "COMMIT":
                    self.assertEqual(snapshot.data["closed_economy"]["vacancy_closure"]["status"], "filled")
                    raise RuntimeError("before vacancy marker")

            for tick in (4, 5):
                world.phase_hook = abort_commit
                snapshot_hash = content_hash(world.snapshot)
                ledger_hash = content_hash(world.scenario.economy.export_state())
                with self.assertRaisesRegex(RuntimeError, "before vacancy marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), snapshot_hash)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), ledger_hash)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual((marker.tick, marker.state_hash), (tick - 1, snapshot_hash))
                await economic_run.advance(world, store, until=tick)
            self.assertEqual(world.snapshot.data["closed_economy"]["labor_contract"]["offer"]["worker_id"],
                             "household:0003")
            self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")
