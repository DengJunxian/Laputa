"""Two dated commitments on a single labor contract and cash ledger."""
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


def installment_config(*, cash=76, cancel=None, cancel_before=None, concurrency=1):
    path = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-payroll-installments.json"
    fields = json.loads(path.read_text())
    payroll = fields["labor_transfer"]["shared_payroll"]
    payroll["cancel_before_start_slot"] = cancel_before
    payroll["payroll_window"]["cancel_second_wage_slot"] = cancel
    return ClosedEconomyConfig.model_validate(dict(fields, initial_firm_cash_minor=cash, concurrency=concurrency))


class PayrollInstallmentBookTests(unittest.TestCase):
    def test_second_plan_must_match_the_first_and_fit_the_remaining_cash(self):
        book = EconomicRuntime({"firm": 20, "worker": 0, "other": 0})
        book.begin_tick(1, 0)
        book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3)
        before = content_hash(book.export_state())
        with self.assertRaisesRegex(ValueError, "first installment"):
            book.reserve_payroll("contract", "firm", "other", 12, due_tick=4, installment=2)
        with self.assertRaisesRegex(EconomicReject, "reservation_cash"):
            book.reserve_payroll("contract", "firm", "worker", 12, due_tick=4, installment=2)
        self.assertEqual(content_hash(book.export_state()), before)

    def test_two_wages_exclude_cash_and_settle_one_at_a_time_after_import(self):
        book = EconomicRuntime({"firm": 40, "worker": 0, "incumbent": 0})
        book.begin_tick(1, 0)
        with self.assertRaisesRegex(ValueError, "first installment"):
            book.reserve_payroll("contract", "firm", "worker", 12, due_tick=4, installment=2)
        book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3)
        second = book.reserve_payroll("contract", "firm", "worker", 12, due_tick=4, installment=2)
        self.assertEqual(book.reserve_payroll("contract", "firm", "worker", 12, due_tick=4, installment=2), second)
        with self.assertRaisesRegex(ValueError, "payload conflict"):
            book.reserve_payroll("contract", "firm", "worker", 13, due_tick=4, installment=2)
        self.assertEqual(book.account_view("firm").available_cash_minor, 16)

        restored = EconomicRuntime({"firm": 40, "worker": 0, "incumbent": 0})
        restored.import_state(book.export_state())
        before = content_hash(restored.export_state())
        tampered = restored.export_state()
        tampered["payroll_reservations"].pop()
        with self.assertRaisesRegex(ValueError, "differ from journal"):
            restored.import_state(tampered)
        self.assertEqual(content_hash(restored.export_state()), before)
        second_id = restored.payroll_reservations[1].reservation_id

        restored.begin_tick(2, 1)
        restored.accrue_wage("incumbent-work", "firm", "incumbent", 20)
        ordinary = restored.pay_wage("incumbent-pay", "firm", "incumbent", 20)
        self.assertEqual((ordinary.details["paid_minor"], ordinary.details["outstanding_minor"]), (16, 4))
        with self.assertRaisesRegex(EconomicReject, "release_mismatch"):
            restored.release_payroll("contract", installment=2)

        restored.begin_tick(3, 2)
        restored.accrue_wage("first-work", "firm", "worker", 12)
        with self.assertRaisesRegex(EconomicReject, "payment_mismatch"):
            restored.pay_wage("wrong-due", "firm", "worker", 12, reservation_id=second_id)
        first = restored.pay_wage("first-pay", "firm", "worker", 12, reservation_id="contract")
        self.assertEqual(first.details["paid_minor"], 12)
        self.assertEqual([r.reservation_id for r in restored.payroll_reservations], [second_id])
        restored.validate_state()

        restored.begin_tick(4, 3)
        with self.assertRaisesRegex(EconomicReject, "release_mismatch"):
            restored.release_payroll("contract", installment=2)
        restored.accrue_wage("second-work", "firm", "worker", 12)
        paid = restored.pay_wage("second-pay", "firm", "worker", 12, reservation_id=second_id)
        self.assertEqual(restored.pay_wage("second-pay", "firm", "worker", 12, reservation_id=second_id), paid)
        self.assertEqual(restored.account_view("worker").cash_minor, 24)
        self.assertEqual(restored.account_view("firm").wage_payable_minor, 4)
        self.assertFalse(restored.payroll_reservations)
        restored.validate_state()

    def test_releasing_future_wage_leaves_due_wage_and_old_claim_intact(self):
        book = EconomicRuntime({"firm": 40, "worker": 0})
        book.begin_tick(1, 0)
        book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3)
        book.reserve_payroll("contract", "firm", "worker", 12, due_tick=4, installment=2)
        book.begin_tick(2, 1)
        book.accrue_wage("old-claim", "firm", "worker", 4)
        book.begin_tick(3, 2)
        cancelled = book.release_payroll("contract", installment=2)
        self.assertEqual(book.release_payroll("contract", installment=2), cancelled)
        self.assertFalse(cancelled.postings or cancelled.goods_movements)
        self.assertEqual([r.reservation_id for r in book.payroll_reservations], ["contract"])
        book.accrue_wage("first-work", "firm", "worker", 12)
        book.pay_wage("first-pay", "firm", "worker", 12, reservation_id="contract")
        self.assertEqual(book.account_view("firm").wage_payable_minor, 4)
        book.validate_state()


class PayrollInstallmentScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_funding_and_cancellation_resolve_only_their_own_periods(self):
        for cash, cancel, cancel_before, statuses, wage_ticks in (
            (64, None, None, [], []),
            (76, None, None, ["paid", "paid"], [4, 5]),
            (76, 1, None, ["paid", "released"], [4]),
            (76, None, 1, ["released", "released"], []),
        ):
            with self.subTest(cash=cash, cancel=cancel, cancel_before=cancel_before):
                world = build_closed_economy(installment_config(cash=cash, cancel=cancel, cancel_before=cancel_before))
                book = world.scenario.economy
                await world.run(2)
                self.assertEqual([r.due_tick for r in book.payroll_reservations], [4, 5] if statuses else [])
                await world.step()
                if statuses and cancel_before is None:
                    view = book.account_view("firm:00")
                    self.assertEqual((view.reserved_payroll_minor, view.available_cash_minor), (24, 28))
                    purchase, = [r for r in world.snapshot.receipts if r.command_id.endswith(":window-purchase")]
                    self.assertEqual(list(purchase.reason_codes), ["insufficient_cash"])
                await world.step()
                self.assertEqual([r.due_tick for r in book.payroll_reservations],
                                 [5] if wage_ticks == [4, 5] else [])
                await world.run(2)
                own = world.snapshot.data["closed_economy"]
                holds = own["shared_payroll"]["holds"]
                self.assertEqual([h["status"] for h in sorted(holds.values(), key=lambda h: h["due_tick"])], statuses)
                work = [e for e in own["labor_events"] if "contract_id" in e]
                self.assertEqual([(e["tick"], e["delivered_minutes"], e["paid_minor"]) for e in work],
                                 [(t, 480, 12) for t in wage_ticks])
                self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_precommit_failure_rolls_back_both_periods_and_resumes(self):
        for cancel, failure_ticks in ((None, (2, 4, 5)), (1, (4,))):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch) / "run"
                world, store = economic_run.create_run(installment_config(cancel=cancel), root)
                for tick in failure_ticks:
                    await economic_run.advance(world, store, until=tick - 1)
                    before = content_hash(world.snapshot)
                    book_hash = content_hash(world.scenario.economy.export_state())

                    def abort(phase, snapshot):
                        if phase.value == "COMMIT":
                            raise RuntimeError("before installment marker")

                    world.phase_hook = abort
                    with self.assertRaisesRegex(RuntimeError, "before installment marker"):
                        await economic_run.advance(world, store, until=tick)
                    self.assertEqual(content_hash(world.snapshot), before)
                    self.assertEqual(content_hash(world.scenario.economy.export_state()), book_hash)
                    world, store, marker = economic_run.open_run(root)
                    self.assertEqual(marker.state_hash, before)
                    await economic_run.advance(world, store, until=tick)
                await economic_run.advance(world, store, until=6)
                direct = build_closed_economy(installment_config(cancel=cancel, concurrency=4))
                await direct.run(6)
                self.assertEqual(content_hash(direct.snapshot), content_hash(world.snapshot))
