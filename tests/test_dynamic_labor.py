"""Behavioral checks for a finite, opt-in C/D employment offer."""
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.contracts.economic import LaborAcceptIntent, PurchaseIntent
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy


def labor_config(**changes):
    return ClosedEconomyConfig(labor_transfer={
        "worker_index": 1, "employer_index": 0, "offer_tick": 4,
        "duration_ticks": 2, "min_previous_sales_units": 1,
    }, **changes)


def worker_payroll(world):
    return [(e.tick, e.payload["employer_id"], e.details["paid_minor"])
            for e in world.scenario.economy.journal
            if e.kind == "wage_payment" and e.payload["worker_id"] == "household:0001"]


class DynamicLaborTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_offer_changes_actual_payroll_and_production_on_next_boundary(self):
        config = labor_config()
        dynamic = build_closed_economy(config)
        control = build_closed_economy(ClosedEconomyConfig())
        worker = "household:0001"
        observations = {}

        def observe_at_offer(phase, snapshot):
            if phase.value == "OBSERVE" and snapshot.tick == 4:
                for actor in dynamic.scenario.actors:
                    candidates = dynamic.scenario.project_observation(dynamic._context(snapshot), actor)
                    observations[actor] = dynamic.observer.observe(actor, snapshot, candidates)

        dynamic.phase_hook = observe_at_offer
        await dynamic.run(4)
        await control.run(4)
        receipt, = [r for r in dynamic.snapshot.receipts if r.outcome == "labor_offer_accepted"]
        self.assertEqual((receipt.actor_id, receipt.counterparty_id, receipt.tick), (worker, "firm:00", 4))
        self.assertEqual((receipt.cash_delta_minor, receipt.ledger_tx_ids), (0, []))
        visible_offers = {a for a, obs in observations.items()
                          if any(f.value.get("kind") == "labor_offer" for f in obs.market_view)}
        self.assertEqual(visible_offers, {worker})

        def allocation(world, tick):
            return {e["firm_id"]: e["units"] for e in world.snapshot.data["closed_economy"]["production_events"]
                    if e["tick"] == tick}

        self.assertEqual(allocation(dynamic, 4), allocation(control, 4))
        await dynamic.run(4)
        await control.run(4)
        self.assertEqual(allocation(dynamic, 5), {"firm:00": 20, "firm:01": 12})
        self.assertEqual(allocation(control, 5), {"firm:00": 16, "firm:01": 16})
        self.assertEqual(worker_payroll(dynamic),
            [(1, "firm:01", 12), (2, "firm:01", 12), (3, "firm:01", 12),
             (4, "firm:01", 12), (5, "firm:00", 12), (6, "firm:00", 12)])
        labor = dynamic.snapshot.data["closed_economy"]["labor_events"]
        self.assertEqual([(e["tick"], e["delivered_minutes"]) for e in labor
                          if e.get("contract_id") == receipt.command_id], [(5, 480), (6, 480)])
        self.assertEqual(allocation(dynamic, 7), {"firm:00": 16, "firm:01": 12})
        self.assertEqual(audit_snapshot(dynamic.snapshot)["status"], "passed")

    async def test_forged_early_late_or_repeated_accepts_do_not_duplicate_a_job(self):
        class ProbePolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if (bundle.tick, bundle.actor_id) not in {
                        (3, "household:0001"), (4, "household:0000"),
                        (4, "household:0001"), (5, "household:0001")}:
                    return bundle
                probe = LaborAcceptIntent(intent_id=f"{bundle.decision_id}:probe",
                    offer_id="labor-offer:4:household:0001:firm:00")
                return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, probe]})

        config = labor_config(concurrency=4)
        world = build_closed_economy(config, policy=ProbePolicy(config, AddressedRNG(7, 0)))
        await world.run(5)
        receipts = [r for r in world.snapshot.receipts if r.unit == "labor_minutes"]
        self.assertCountEqual([(r.tick, r.actor_id, list(r.reason_codes)) for r in receipts if r.status == "rejected"],
            [(3, "household:0001", ["labor_offer_unavailable"]),
             (4, "household:0000", ["labor_offer_unavailable"]),
             (4, "household:0001", ["labor_offer_already_accepted"]),
             (5, "household:0001", ["labor_offer_unavailable"])])
        self.assertEqual(sum(r.status == "accepted" for r in receipts), 1)
        self.assertEqual(worker_payroll(world)[-1], (5, "firm:00", 12))

    async def test_demand_and_funding_gates_and_zero_work_at_source_firm(self):
        common = dict(ticks=4, households=2, network_neighbors=1,
            output_per_worker=1, initial_price_minor=6, fault_tick=None)
        for cash, threshold, expected in [(0, 1, ["insufficient_labor_funding"]), (400, 3, []), (400, 1, [])]:
            with self.subTest(cash=cash, threshold=threshold):
                config = ClosedEconomyConfig(**common, initial_firm_cash_minor=cash, labor_transfer={
                    "worker_index": 1, "employer_index": 0, "offer_tick": 2,
                    "duration_ticks": 1, "min_previous_sales_units": threshold,
                })
                world = build_closed_economy(config)
                await world.run(4)
                own = world.snapshot.data["closed_economy"]
                self.assertEqual(own["macro_history"][0]["sold_units_by_seller"]["firm:00"], 1)
                self.assertEqual([reason for r in world.snapshot.receipts if r.unit == "labor_minutes"
                                  for reason in r.reason_codes], expected)
                if cash and threshold == 1:
                    empty_firm = next(e for e in own["production_events"] if (e["tick"], e["firm_id"]) == (3, "firm:01"))
                    self.assertEqual((empty_firm["units"], empty_firm["ledger_tx_id"]), (0, None))
                    self.assertEqual(worker_payroll(world)[-1], (3, "firm:00", 12))
                else:
                    self.assertIsNone(own["labor_contract"])
                    self.assertEqual({payee for _, payee, _ in worker_payroll(world)}, {"firm:01"})
                self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_cash_spent_after_observation_voids_unfunded_activation(self):
        class DrawdownPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if (bundle.actor_id, bundle.tick) != ("firm:00", 4):
                    return bundle
                quote = next(f for f in observation.market_view if f.value.get("kind") == "goods_quote"
                             and f.value["seller_id"] == "firm:01")
                cap = observation.account_view[0].value["available_cash_minor"]
                price = quote.value["unit_price_minor"]
                q = min(quote.value["available_units"], cap // price)
                purchase = PurchaseIntent(intent_id="draw-down", seller_id="firm:01", quote_id=quote.value["quote_id"],
                    quantity_units=q, max_unit_price_minor=price, allow_partial=True)
                return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, purchase]})

        config = labor_config(initial_firm_cash_minor=160)
        world = build_closed_economy(config, policy=DrawdownPolicy(config, AddressedRNG(7, 0)))
        await world.run(4)
        self.assertTrue(any(r.outcome == "labor_offer_accepted" for r in world.snapshot.receipts))
        await world.step()
        self.assertTrue(world.snapshot.data["closed_economy"]["labor_contract"]["activation_failed"])
        self.assertEqual(worker_payroll(world)[-1][:2], (5, "firm:01"))

    async def test_signing_and_first_payroll_roll_back_then_resume(self):
        config = labor_config()
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "recovery"
            world, store = economic_run.create_run(config, root)
            await economic_run.advance(world, store, until=3)
            for end in (4, 5):
                before = content_hash(world.snapshot)
                ledger = content_hash(world.scenario.economy.export_state())

                def before_marker(phase, snapshot):
                    if phase.value == "COMMIT":
                        self.assertIsNotNone(snapshot.data["closed_economy"]["labor_contract"])
                        raise RuntimeError("injected before labor marker")

                world.phase_hook = before_marker
                with self.assertRaisesRegex(RuntimeError, "injected before labor marker"):
                    await economic_run.advance(world, store, until=end)
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), ledger)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual((marker.tick, marker.state_hash), (end - 1, before))
                await economic_run.advance(world, store, until=end)

            expected = build_closed_economy(labor_config(concurrency=4))
            await expected.run(5)
            self.assertEqual(content_hash(world.snapshot), content_hash(expected.snapshot))
