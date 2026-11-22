"""Two pending jobs draw first wages from a single settled cash budget."""
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


def shared_config(*, cash=64, cancel_slot=None, concurrency=1):
    return ClosedEconomyConfig(ticks=5, households=4, network_neighbors=1,
        initial_firm_cash_minor=cash, initial_price_minor=6, fault_tick=None,
        target_units_min=1, target_units_max=1, concurrency=concurrency,
        labor_transfer={"worker_index":1, "employer_index":0, "offer_tick":2,
            "duration_ticks":2, "min_previous_sales_units":1,
            "shared_payroll":{"second_candidate_indices":[3], "cancel_before_start_slot":cancel_slot}})


class SharedPayrollTests(unittest.IsolatedAsyncioTestCase):
    async def test_individually_affordable_slots_cannot_share_the_same_cash(self):
        world = build_closed_economy(shared_config())
        await world.run(2)
        payroll = world.snapshot.data["closed_economy"]["shared_payroll"]
        first, second = payroll["closures"].values()
        self.assertEqual((first["status"], second["status"]), ("filled", "unfunded"))
        self.assertEqual((first["available_cash_minor"], first["first_payroll_minor"]), (40, 36))
        self.assertEqual((second["available_cash_minor"], second["first_payroll_minor"]), (28, 36))
        hold, = payroll["holds"].values()
        self.assertEqual((hold["amount_minor"], hold["status"]), (12, "held"))
        await world.step()
        payroll = world.snapshot.data["closed_economy"]["shared_payroll"]
        hold, = payroll["holds"].values()
        self.assertEqual(hold["status"], "paid")
        paid = [j for j in world.scenario.economy.journal if j.tx_id == hold["payment_tx_id"]]
        self.assertEqual([(j.tick, j.payload["worker_id"], j.details["paid_minor"]) for j in paid],
            [(3, "household:0001", 12)])
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_first_unfilled_leaves_all_funds_available_to_the_second(self):
        class AbstainingPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != "household:0001":
                    return bundle
                return bundle.model_copy(update={"economic_intents": [
                    intent for intent in bundle.economic_intents if not isinstance(intent, LaborApplyIntent)]})

        config = shared_config()
        world = build_closed_economy(config, policy=AbstainingPolicy(config, AddressedRNG(7, 0)))
        await world.run(3)
        payroll = world.snapshot.data["closed_economy"]["shared_payroll"]
        first, second = payroll["closures"].values()
        self.assertEqual((first["status"], second["status"]), ("unfilled", "filled"))
        self.assertEqual((first["available_cash_minor"], second["available_cash_minor"]), (40, 40))
        winner, = payroll["contracts"].values()
        self.assertEqual(winner["offer"]["worker_id"], "household:0003")
        hold, = payroll["holds"].values()
        self.assertEqual(hold["status"], "paid")

    async def test_goods_outflow_alters_shared_approval_without_using_same_tick_sales(self):
        class BuyingFirm(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if (bundle.tick, bundle.actor_id) != (2, "firm:00"):
                    return bundle
                quote = next(f.value for f in observation.market_view
                             if f.value.get("kind") == "goods_quote" and f.value["seller_id"] == "firm:01")
                purchase = PurchaseIntent(intent_id="firm-buy-before-payroll", seller_id="firm:01",
                    quote_id=quote["quote_id"], quantity_units=1, max_unit_price_minor=quote["unit_price_minor"])
                return bundle.model_copy(update={"economic_intents": [*bundle.economic_intents, purchase]})

        config = shared_config(cash=76)
        world = build_closed_economy(config, policy=BuyingFirm(config, AddressedRNG(7, 0)))
        await world.run(2)
        payroll = world.snapshot.data["closed_economy"]["shared_payroll"]
        first, second = payroll["closures"].values()
        purchase, = [r for r in world.snapshot.receipts if r.tick == 2 and r.actor_id == "firm:00"
                     and r.outcome == "purchased"]
        avail = world.scenario.economy.account_view("firm:00")
        self.assertGreater(avail.cash_minor, first["available_cash_minor"])
        self.assertEqual(first["available_cash_minor"], 52 + purchase.cash_delta_minor)
        self.assertEqual(second["available_cash_minor"], first["available_cash_minor"] - 12)
        self.assertEqual((first["status"], second["status"]), ("filled", "unfunded"))
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_two_awards_survive_checkpoint_and_cancellation_releases_one_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "payroll"
            config = shared_config(cash=76, cancel_slot=1)
            await economic_run.run_new(config, root, until=2)
            stopped, _, marker = economic_run.open_run(root)
            payroll = stopped.snapshot.data["closed_economy"]["shared_payroll"]
            self.assertEqual(marker.tick, 2)
            self.assertEqual([c["available_cash_minor"] for c in payroll["closures"].values()], [52, 40])
            self.assertEqual([h["status"] for h in payroll["holds"].values()], ["held", "held"])
            await economic_run.resume_run(root, until=3)
            resumed, _, _ = economic_run.open_run(root)
            state = resumed.snapshot.data["closed_economy"]
            payroll = state["shared_payroll"]
            first, second = payroll["contracts"].values()
            self.assertTrue(first["activation_failed"])
            released = payroll["holds"][first["contract_id"]]
            paid = payroll["holds"][second["contract_id"]]
            self.assertEqual((released["status"], released["payment_tx_id"]), ("released", None))
            self.assertEqual(paid["status"], "paid")
            work = {e["worker_id"]: e for e in state["labor_events"] if e["tick"] == 3}
            self.assertEqual(work["household:0001"]["employer_id"], "firm:01")
            self.assertNotIn("contract_id", work["household:0001"])
            self.assertEqual(work["household:0003"]["payment_tx_id"], paid["payment_tx_id"])
            self.assertEqual(audit_snapshot(resumed.snapshot)["status"], "passed")

    async def test_failed_award_and_payroll_commits_recover_without_duplicate_holds_or_wages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "payroll"
            world, store = economic_run.create_run(shared_config(cash=76), root)
            await economic_run.advance(world, store, until=1)

            def abort(phase, snapshot):
                if phase.value == "COMMIT":
                    raise RuntimeError("before shared payroll marker")

            for tick in (2, 3):
                world.phase_hook = abort
                old_snapshot = content_hash(world.snapshot)
                old_book = content_hash(world.scenario.economy.export_state())
                with self.assertRaisesRegex(RuntimeError, "before shared payroll marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), old_snapshot)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), old_book)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual(marker.state_hash, old_snapshot)
                await economic_run.advance(world, store, until=tick)

            parallel = build_closed_economy(shared_config(cash=76, concurrency=4))
            await parallel.run(3)
            self.assertEqual(content_hash(world.snapshot), content_hash(parallel.snapshot))
            holds = world.snapshot.data["closed_economy"]["shared_payroll"]["holds"]
            self.assertEqual(len(holds), 2)
            self.assertEqual(len({h["payment_tx_id"] for h in holds.values()}), 2)
            self.assertTrue(all(h["status"] == "paid" for h in holds.values()))
