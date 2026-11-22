"""Payroll reservations restrict settled cash across one goods window."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.economy.runtime import EconomicReject, EconomicRuntime
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig


def window_config(*, cancel=None, concurrency=1):
    path = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-payroll-window.json"
    fields = json.loads(path.read_text())
    fields["labor_transfer"]["shared_payroll"]["cancel_before_start_slot"] = cancel
    return ClosedEconomyConfig.model_validate(dict(fields, concurrency=concurrency))


class PayrollWindowBookTests(unittest.TestCase):
    def test_reserved_cash_survives_restore_and_protects_first_wage(self):
        book = EconomicRuntime({"firm": 40, "worker": 0, "incumbent": 0})
        book.begin_tick(1, 0)
        book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3)
        self.assertEqual(book.account_view("firm").available_cash_minor, 28)
        restored = EconomicRuntime({"firm": 40, "worker": 0, "incumbent": 0})
        restored.import_state(book.export_state())
        restored.begin_tick(2, 1)
        restored.accrue_wage("incumbent-accrual", "firm", "incumbent", 35)
        paid = restored.pay_wage("incumbent-payment", "firm", "incumbent", 35)
        self.assertEqual(paid.details["paid_minor"], 28)
        self.assertEqual(restored.account_view("firm").wage_payable_minor, 7)
        self.assertEqual(restored.account_view("firm").available_cash_minor, 0)
        restored.begin_tick(3, 2)
        restored.accrue_wage("new-accrual", "firm", "worker", 12)
        paid = restored.pay_wage("first-pay", "firm", "worker", 12, reservation_id="contract")
        self.assertEqual(paid.details["paid_minor"], 12)
        self.assertEqual(restored.account_view("worker").cash_minor, 12)
        self.assertEqual(restored.pay_wage("first-pay", "firm", "worker", 12, reservation_id="contract"), paid)
        self.assertFalse(restored.payroll_reservations)
        restored.validate_state()

    def test_conflicting_reserve_and_mistimed_release_do_not_change_balances(self):
        book = EconomicRuntime({"firm": 40, "worker": 0})
        book.begin_tick(1, 0)
        first = book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3)
        self.assertEqual(book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3), first)
        before = content_hash(book.export_state())
        with self.assertRaisesRegex(ValueError, "payload conflict"):
            book.reserve_payroll("contract", "firm", "worker", 13, due_tick=3)
        with self.assertRaisesRegex(EconomicReject, "release_mismatch"):
            book.release_payroll("contract")
        self.assertEqual(content_hash(book.export_state()), before)
        tampered = book.export_state()
        tampered.pop("payroll_reservations")
        with self.assertRaisesRegex(ValueError, "differ from journal"):
            book.import_state(tampered)
        self.assertEqual(content_hash(book.export_state()), before)
        book.begin_tick(2, 1)
        released = book.release_payroll("contract")
        self.assertEqual(book.release_payroll("contract"), released)
        self.assertEqual(book.account_view("firm").available_cash_minor, 40)
        self.assertEqual(book.account_view("firm").wage_payable_minor, 0)
        book.validate_state()

    def test_due_payment_requires_the_reserved_party_amount_and_due_period(self):
        book = EconomicRuntime({"firm": 40, "worker": 0, "other": 0})
        book.begin_tick(1, 0)
        book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3)
        book.begin_tick(2, 1)
        book.accrue_wage("early-accrual", "firm", "worker", 12)
        with self.assertRaisesRegex(EconomicReject, "payment_mismatch"):
            book.pay_wage("early-payment", "firm", "worker", 12, reservation_id="contract")
        book.begin_tick(3, 2)
        book.accrue_wage("other-accrual", "firm", "other", 12)
        before = content_hash(book.export_state())
        for worker, amount in (("other", 12), ("worker", 11)):
            with self.assertRaisesRegex(EconomicReject, "payment_mismatch"):
                book.pay_wage("invalid-payment", "firm", worker, amount, reservation_id="contract")
        self.assertEqual(content_hash(book.export_state()), before)
        with self.assertRaisesRegex(ValueError, "not settled on time"):
            book.validate_state()


class PayrollWindowScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_held_funds_block_same_bid_that_succeeds_after_release(self):
        results = {}
        for cancel in (None, 1):
            world = build_closed_economy(window_config(cancel=cancel))
            observed = {}

            def freeze(phase, snapshot):
                if phase.value == "OBSERVE" and snapshot.tick == 3:
                    facts = world.scenario.project_observation(world._context(snapshot), "firm:00")
                    view = world.observer.observe("firm:00", snapshot, facts).account_view[0].value
                    observed.update(view)

            world.phase_hook = freeze
            await world.run(4)
            receipt, = [r for r in world.snapshot.receipts if r.command_id.endswith(":window-purchase")]
            closing = world.snapshot.data["closed_economy"]["shared_payroll"]
            hold, = closing["holds"].values()
            due = [line for line in world.scenario.economy.journal
                   if line.kind == "wage_payment" and "reservation_id" in line.payload]
            results[cancel] = observed, receipt, hold, due
            self.assertFalse(world.scenario.economy.payroll_reservations)
            self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

        held, rejected, paid, due = results[None]
        available, approved, released, no_due = results[1]
        self.assertEqual((held["cash_minor"], available["cash_minor"]), (40, 40))
        self.assertEqual((held["available_cash_minor"], available["available_cash_minor"]), (28, 40))
        self.assertEqual(held["reserved_payroll_minor"], 12)
        self.assertEqual((rejected.status, list(rejected.reason_codes), rejected.cash_delta_minor),
                         ("rejected", ["insufficient_cash"], 0))
        self.assertEqual((approved.outcome, approved.cash_delta_minor, approved.goods_delta_units), ("purchased", -35, 5))
        self.assertEqual((paid["status"], len(due), due[0].tick, due[0].details["paid_minor"]),
                         ("paid", 1, 4, 12))
        self.assertEqual((released["status"], no_due), ("released", []))

    async def test_failed_reservation_release_and_settlement_recover_atomically(self):
        for cancel, failing_ticks in ((None, (2, 4)), (1, (3,))):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch) / "window"
                world, store = economic_run.create_run(window_config(cancel=cancel), root)
                for tick in failing_ticks:
                    await economic_run.advance(world, store, until=tick - 1)
                    before = content_hash(world.snapshot)
                    book = content_hash(world.scenario.economy.export_state())

                    def abort(phase, snapshot):
                        if phase.value == "COMMIT":
                            raise RuntimeError("before payroll-window marker")

                    world.phase_hook = abort
                    with self.assertRaisesRegex(RuntimeError, "before payroll-window marker"):
                        await economic_run.advance(world, store, until=tick)
                    self.assertEqual(content_hash(world.snapshot), before)
                    self.assertEqual(content_hash(world.scenario.economy.export_state()), book)
                    world, store, marker = economic_run.open_run(root)
                    self.assertEqual(marker.state_hash, before)
                    await economic_run.advance(world, store, until=tick)
                await economic_run.advance(world, store, until=4)
                direct = build_closed_economy(window_config(cancel=cancel, concurrency=4))
                await direct.run(4)
                self.assertEqual(content_hash(direct.snapshot), content_hash(world.snapshot))
