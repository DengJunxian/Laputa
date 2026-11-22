"""Accounting invariants and adversarial recovery tests for the real M0 participant."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "reproduction/runtime/source"),
               str(ROOT / "reproduction/runtime/source/src")]

from pydantic import ValidationError
from civitas.contracts.base import content_hash
from civitas.contracts.decisions import HoldIntent
from civitas.contracts.economic import (
    ConsumeIntent, EconomicCommand, EconomicExecutionReceipt, PurchaseIntent, QuoteIntent,
)
from civitas.contracts.portfolio import MAX_INT
from civitas.contracts.state import EconomicWorldSnapshot, TickContext
from civitas.economy import EconomicReject, EconomicRuntime
from civitas.economy import runtime as economic_runtime
from core.experiments import identity
from core.experiments.strict_types import SealedJSONRowsV1


def command(runtime, name, actor, intent, *, order=0):
    return EconomicCommand(command_id=f"cmd:{name}", decision_id=f"decision:{name}",
        actor_id=actor, local_order_index=order, arrival_key=0, intent=intent,
        tick=runtime.tick, batch_id=f"batch:{runtime.tick}",
        observation_version=runtime.world_version, world_version=runtime.world_version)


def purchase(runtime, name, *, actor="buyer", seller="firm", quote="quote:firm",
             quantity=1, price=5, partial=False):
    return command(runtime, name, actor, PurchaseIntent(intent_id=f"intent:{name}",
        seller_id=seller, quote_id=quote, quantity_units=quantity,
        max_unit_price_minor=price, allow_partial=partial))


def market(*, buyer_cash=100, firm_cash=100, units=4, cost=12, price=5):
    runtime = EconomicRuntime({"buyer": buyer_cash, "firm": firm_cash},
        initial_goods={"firm": units}, initial_inventory_cost={"firm": cost},
        labor_cost_per_unit_minor=3)
    runtime.set_quote("quote:firm", "firm", price, effective_tick=1, ttl_ticks=20)
    runtime.begin_tick(1, 0)
    return runtime


class EconomicBookTests(unittest.TestCase):
    def assert_balanced(self, runtime):
        for entry in runtime.journal:
            balances = defaultdict(int)
            for posting in entry.postings:
                balances[(posting.entity_id, posting.currency_id)] += (
                    posting.amount_minor if posting.side == "debit" else -posting.amount_minor)
            self.assertFalse(any(balances.values()), entry.event_id)
        runtime.validate_state()

    def test_opening_token_and_cost_basis_are_explicit(self):
        runtime = market()
        self.assertEqual(sum(runtime.account_view(a).cash_minor for a in runtime.actor_ids), 200)
        self.assertEqual(runtime.account_view("firm").inventory_cost_minor, 12)
        self.assertEqual(runtime.journal[0].kind, "opening")
        self.assert_balanced(runtime)

    def test_later_validation_skips_an_unchanged_quote_receipt(self):
        book = market()
        quote = command(book, "quote", "firm", QuoteIntent(intent_id="intent:quote",
                        quote_id="quote:later", unit_price_minor=5))
        book.execute_batch([quote])
        book.validate_state()
        book.begin_tick(2, 1)
        with patch.object(economic_runtime, "GoodsQuote", wraps=economic_runtime.GoodsQuote) as checked_quote:
            book.validate_state()
            checked_quote.assert_not_called()
            book.validate_state(full_history=True)
            checked_quote.assert_called_once()

    def test_historical_receipt_rewrite_and_late_event_are_rejected(self):
        for change in ("receipt", "late_event"):
            with self.subTest(change=change):
                book = market()
                hold = command(book, "hold", "buyer", HoldIntent(intent_id="intent:hold"))
                receipt, = book.execute_batch([hold])
                book.validate_state()
                book.begin_tick(2, 1)
                if change == "receipt":
                    wire = receipt.model_dump()
                    wire["decision_id"] = "decision:other"
                    book._receipts[hold.command_id] = EconomicExecutionReceipt.model_validate(wire)
                    message = "Command/receipt identity mismatch"
                else:
                    book.accrue_wage(f"command:{hold.command_id}", "firm", "buyer", 1)
                    message = "Economic action journal event lacks its command receipt|Receipt journal reference mismatch"
                with self.assertRaisesRegex(ValueError, message):
                    book.validate_state()

    def test_import_validates_old_receipt_references_and_duplicate_commands(self):
        book = market()
        book.execute_batch([purchase(book, "buy")])
        book.validate_state()
        book.begin_tick(2, 1)
        book.validate_state()
        committed = book.export_state()
        for change in ("receipt", "duplicate"):
            with self.subTest(change=change):
                bad = deepcopy(committed)
                if change == "receipt":
                    bad["receipts"][0]["ledger_tx_ids"] = ["econ:system:opening"]
                    message = "Receipt journal reference mismatch"
                else:
                    bad["commands"].append(deepcopy(bad["commands"][0]))
                    message = "Duplicate command"
                with self.assertRaisesRegex(ValueError, message):
                    book.import_state(bad)
                self.assertEqual(book.export_state(), committed)

    def test_historical_journal_key_is_checked_even_when_the_record_is_reused(self):
        book = market()
        buy = purchase(book, "buy")
        book.execute_batch([buy])
        book.validate_state()
        book.begin_tick(2, 1)
        book._journal["wrong-key"] = book._journal.pop(f"command:{buy.command_id}")
        with self.assertRaisesRegex(ValueError, "Journal event key mismatch|Receipt journal reference mismatch"):
            book.validate_state()

    def test_warm_history_does_not_hide_payroll_due_at_the_new_boundary(self):
        book = EconomicRuntime({"firm": 40, "worker": 0})
        book.begin_tick(1, 0)
        book.reserve_payroll("contract", "firm", "worker", 12, due_tick=3)
        book.validate_state()
        book.begin_tick(2, 1)
        book.validate_state()
        book.begin_tick(3, 2)
        with self.assertRaisesRegex(ValueError, "reservation was not settled on time"):
            book.validate_state()

    def test_sealed_journal_prefix_survives_snapshot_and_restore(self):
        book = market()
        base = book.snapshot_state()
        book.execute_batch([purchase(book, 'buy')])
        state = book.snapshot_state()
        context = TickContext(tick=1, world_version=0, execution_seq=1, actors=['buyer', 'firm'],
                              data={'economic_runtime': state})
        journal = context.data['economic_runtime']['journal']
        self.assertIs(journal[0], base['journal'][0])
        self.assertEqual(context.model_dump(mode='json')['data']['economic_runtime'], book.export_state())
        self.assertEqual(len(base['journal']), 1)
        with self.assertRaises(TypeError):
            journal[0]['payload']['cash']['buyer'] = 0
        book.import_state(base)
        self.assertEqual(book.snapshot_state(), base)
        book.execute_batch([purchase(book, 'retry')])
        self.assertEqual(book.snapshot_state(), book.export_state())
        self.assertNotEqual(book.snapshot_state()['journal'][-1], state['journal'][-1])

    def test_live_snapshot_hash_reuses_the_unchanged_opening_rows(self):
        book = market()

        def snapshot():
            return EconomicWorldSnapshot(tick=book.tick, world_version=book.world_version,
                execution_seq=0, actors=list(book.actor_ids), data={'economic_runtime': book.snapshot_state()})

        first = snapshot()
        book.execute_batch([purchase(book, 'first-buy')])
        second = snapshot()
        opening_encodes = []
        original_tagged = identity._tagged

        def count_opening(value, active):
            if isinstance(value, dict) and value.get('event_id') == 'system:opening':
                opening_encodes.append(value)
            return original_tagged(value, active)

        with patch.object(identity, '_tagged', count_opening):
            digests = [first.state_hash, second.state_hash]
        self.assertEqual(digests, [content_hash(first), content_hash(second)])
        self.assertEqual(len(opening_encodes), 1)
        restored = EconomicWorldSnapshot.model_validate(second.model_dump(mode='json'))
        self.assertEqual(restored.state_hash, second.state_hash)
        raw = restored.model_dump(mode='json')
        rows = raw['data']['economic_runtime']['journal']
        rows[0]['details']['wire_values'] = ('中', -0.0, None)
        raw['data']['economic_runtime']['journal'] = SealedJSONRowsV1(None, rows)
        with_tuple = EconomicWorldSnapshot.model_validate(raw)
        self.assertEqual(with_tuple.state_hash, content_hash(with_tuple))

    def test_execution_snapshot_serializes_only_appends_and_rechecks_replaced_rows(self):
        book = market()
        buy, reject = purchase(book, 'buy'), purchase(book, 'reject', price=4)
        accepted, rejected = book.execute_batch([buy, reject])
        self.assertEqual((accepted.status, rejected.status), ('accepted', 'rejected'))
        before = book.snapshot_state()
        another = purchase(book, 'another')
        book.execute_batch([another], execution_seq_start=2)
        serialized = []

        def count_dump(dump):
            def counted(record, **kwargs):
                serialized.append(record.command_id)
                return dump(record, **kwargs)
            return counted

        with (patch.object(EconomicCommand, 'model_dump', count_dump(EconomicCommand.model_dump)),
              patch.object(EconomicExecutionReceipt, 'model_dump', count_dump(EconomicExecutionReceipt.model_dump))):
            after = book.snapshot_state()
            self.assertEqual(book.execute_batch([buy, reject]), [accepted, rejected])
            retry = book.snapshot_state()
        self.assertEqual(serialized, [another.command_id, another.command_id])
        self.assertEqual(after, book.export_state())
        for field in ('commands', 'receipts'):
            self.assertIs(after[field][1], before[field][1])
            self.assertIs(after[field], retry[field])
        with self.assertRaises(TypeError):
            after['receipts'][1]['reason_codes'].append('other')
        changed = rejected.model_copy(update={'reason_codes': ['other']})
        book._receipts[reject.command_id] = changed
        refreshed = book.snapshot_state()
        self.assertEqual(refreshed, book.export_state())
        self.assertIs(refreshed['commands'], after['commands'])
        self.assertNotEqual(refreshed['receipts'][1], before['receipts'][1])
        self.assertFalse(refreshed['receipts'].retains(after['receipts']))

    def test_money_rejects_boolean_float_negative_and_overflow(self):
        for bad in (True, 1.0, -1, MAX_INT+1, "1"):
            with self.subTest(bad=bad), self.assertRaises((ValueError, ValidationError)):
                EconomicRuntime({"a": bad})
        with self.assertRaises(ValueError):
            EconomicRuntime({"a": MAX_INT, "b": 1})
        with self.assertRaises((ValueError, ValidationError)):
            EconomicRuntime({"a": 0}, initial_goods=False)
        with self.assertRaises(ValueError):
            EconomicRuntime({"a": 0}, currency_id="same", good_id="same")

    def test_strict_action_amounts(self):
        for bad in (True, 0, -1, 1.0, MAX_INT+1):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                PurchaseIntent(intent_id="i", seller_id="s", quote_id="q",
                               quantity_units=bad, max_unit_price_minor=5)

    def test_accrual_and_payment_do_not_double_count_income(self):
        runtime = EconomicRuntime({"firm": 100, "worker": 0}, labor_cost_per_unit_minor=3)
        runtime.begin_tick(1, 0)
        runtime.accrue_wage("labor:1", "firm", "worker", 12)
        self.assertEqual(runtime.account_view("worker").cash_minor, 0)
        self.assertEqual(runtime.account_view("worker").wage_receivable_minor, 12)
        paid = runtime.pay_wage("pay:1", "firm", "worker", 12)
        self.assertEqual(paid.details["paid_minor"], 12)
        self.assertEqual(runtime.account_view("worker").wage_income_minor, 12)
        self.assertEqual(runtime.account_view("worker").wage_receivable_minor, 0)
        self.assertEqual(runtime.account_view("firm").work_in_progress_minor, 12)
        runtime.produce("production:1", "firm", 4, labor_cost_minor=12)
        self.assertEqual(runtime.account_view("firm").work_in_progress_minor, 0)
        self.assertEqual(runtime.account_view("firm").goods_units, 4)
        self.assert_balanced(runtime)

    def test_cash_short_payroll_retains_real_obligation(self):
        runtime = EconomicRuntime({"firm": 5, "worker": 0}, labor_cost_per_unit_minor=3)
        runtime.begin_tick(1, 0)
        runtime.accrue_wage("labor", "firm", "worker", 12)
        paid = runtime.pay_wage("pay", "firm", "worker", 12)
        self.assertEqual(paid.details, {"requested_minor": 12, "paid_minor": 5, "outstanding_minor": 7})
        self.assertEqual(runtime.account_view("firm").wage_payable_minor, 7)
        self.assertEqual(runtime.account_view("worker").wage_receivable_minor, 7)
        self.assertEqual(runtime.account_view("firm").cash_minor, 0)
        runtime.produce("production", "firm", 4, labor_cost_minor=12)
        self.assert_balanced(runtime)

    def test_unaccrued_payroll_cannot_create_wages(self):
        runtime = EconomicRuntime({"firm": 100, "worker": 0})
        runtime.begin_tick(1, 0)
        paid = runtime.pay_wage("pay", "firm", "worker", 10)
        self.assertEqual(paid.details["paid_minor"], 0)
        self.assertFalse(paid.postings)
        self.assertEqual(runtime.account_view("worker").wage_income_minor, 0)
        self.assert_balanced(runtime)

    def test_production_requires_verified_labor_and_recipe(self):
        runtime = EconomicRuntime({"firm": 100, "worker": 0}, labor_cost_per_unit_minor=3)
        runtime.begin_tick(1, 0)
        before = runtime.export_state()
        with self.assertRaisesRegex(EconomicReject, "insufficient_verified_labor_cost"):
            runtime.produce("free", "firm", 4, labor_cost_minor=12)
        self.assertEqual(before, runtime.export_state())
        runtime.accrue_wage("labor", "firm", "worker", 12)
        before = runtime.export_state()
        with self.assertRaisesRegex(EconomicReject, "recipe_mismatch"):
            runtime.produce("wrong", "firm", 5, labor_cost_minor=12)
        self.assertEqual(before, runtime.export_state())

    def test_purchase_then_consumption_is_one_payment(self):
        runtime = market(buyer_cash=12)
        receipts = runtime.execute_batch([
            purchase(runtime, "buy", quantity=2),
            command(runtime, "consume", "buyer", ConsumeIntent(intent_id="intent:consume", quantity_units=2), order=1),
        ])
        buyer, firm = runtime.account_view("buyer"), runtime.account_view("firm")
        self.assertEqual([r.outcome for r in receipts], ["purchased", "consumed"])
        self.assertEqual((buyer.cash_minor, buyer.goods_units, buyer.inventory_cost_minor), (2, 0, 0))
        self.assertEqual(buyer.expenses_minor, 10)
        self.assertEqual((firm.cash_minor, firm.goods_units, firm.inventory_cost_minor), (110, 2, 6))
        self.assert_balanced(runtime)

    def test_integer_cost_rounding_removes_every_last_minor_unit(self):
        runtime = market(units=3, cost=10)
        expected_costs = [7, 4, 0]
        for index, remaining in enumerate(expected_costs):
            runtime.execute_batch([purchase(runtime, f"buy:{index}")], execution_seq_start=index)
            self.assertEqual(runtime.account_view("firm").inventory_cost_minor, remaining)
        self.assertEqual(runtime.account_view("firm").expenses_minor, 10)
        self.assertEqual(runtime.account_view("buyer").inventory_cost_minor, 15)
        self.assert_balanced(runtime)

    def test_partial_execution_requires_explicit_opt_in(self):
        runtime = market(buyer_cash=12, units=5, cost=15)
        receipt = runtime.execute_batch([purchase(runtime, "buy", quantity=5, partial=True)])[0]
        self.assertEqual((receipt.status, receipt.executed_quantity), ("partially_executed", 2))
        self.assertEqual(runtime.account_view("buyer").cash_minor, 2)
        self.assert_balanced(runtime)

    def test_resource_rejection_is_never_a_quality_failure(self):
        for runtime, quantity, reason in (
            (market(buyer_cash=4), 1, "insufficient_cash"),
            (market(units=1, cost=3), 2, "insufficient_goods"),
        ):
            with self.subTest(reason=reason):
                before = runtime.journal
                receipt = runtime.execute_batch([purchase(runtime, "buy", quantity=quantity)],
                    delivery_failures={"cmd:buy"})[0]
                self.assertEqual(receipt.status, "rejected")
                self.assertEqual(receipt.reason_codes, [reason])
                self.assertFalse(receipt.quality_failure)
                self.assertEqual(receipt.accepted_quantity, 0)
                self.assertEqual(runtime.journal, before)
                self.assert_balanced(runtime)

    def test_accepted_quality_failure_refunds_and_scraps_atomically(self):
        runtime = market()
        receipt = runtime.execute_batch([purchase(runtime, "buy", quantity=2)],
            delivery_failures={"cmd:buy"})[0]
        buyer, firm = runtime.account_view("buyer"), runtime.account_view("firm")
        self.assertEqual(receipt.status, "accepted")
        self.assertEqual(receipt.outcome, "delivery_failed_refunded")
        self.assertTrue(receipt.quality_failure)
        self.assertEqual((receipt.accepted_quantity, receipt.executed_quantity, receipt.refunded_minor), (2, 0, 10))
        self.assertEqual((buyer.cash_minor, buyer.goods_units), (100, 0))
        self.assertEqual((firm.cash_minor, firm.goods_units, firm.inventory_cost_minor), (100, 2, 6))
        self.assertEqual((firm.sales_income_minor, firm.expenses_minor), (0, 6))
        self.assertEqual(runtime.journal[-1].goods_movements[0].kind, "scrap")
        self.assertEqual(sum(p.amount_minor for p in runtime.journal[-1].postings
                             if p.entity_id == "buyer" and p.account_code == "cash"), 20)
        self.assert_balanced(runtime)

    def test_system_idempotency_and_payload_conflict(self):
        runtime = EconomicRuntime({"firm": 100, "worker": 0})
        runtime.begin_tick(1, 0)
        first = runtime.accrue_wage("labor", "firm", "worker", 10)
        before = runtime.export_state()
        self.assertEqual(runtime.accrue_wage("labor", "firm", "worker", 10), first)
        self.assertEqual(before, runtime.export_state())
        with self.assertRaisesRegex(ValueError, "payload conflict"):
            runtime.accrue_wage("labor", "firm", "worker", 11)
        with self.assertRaisesRegex(ValueError, "payload conflict"):
            runtime.pay_wage("labor", "firm", "worker", 10)
        self.assertEqual(before, runtime.export_state())

    def test_command_retry_does_not_settle_twice(self):
        runtime = market()
        cmd = purchase(runtime, "buy", quantity=2)
        receipts = runtime.execute_batch([cmd])
        before = runtime.export_state()
        self.assertEqual(runtime.execute_batch([cmd]), receipts)
        self.assertEqual(before, runtime.export_state())
        changed = cmd.model_copy(update={"intent": cmd.intent.model_copy(update={"quantity_units": 1})})
        with self.assertRaisesRegex(ValueError, "payload conflict"):
            runtime.execute_batch([changed])
        self.assertEqual(before, runtime.export_state())

    def test_quality_retry_cannot_change_the_original_outcome(self):
        runtime = market()
        cmd = purchase(runtime, "buy")
        first = runtime.execute_batch([cmd], delivery_failures={cmd.command_id})
        before = runtime.export_state()
        self.assertEqual(runtime.execute_batch([cmd], delivery_failures={cmd.command_id}), first)
        with self.assertRaisesRegex(ValueError, "quality outcome conflict"):
            runtime.execute_batch([cmd])
        self.assertEqual(before, runtime.export_state())

    def test_current_sales_are_not_reused_before_observation_barrier(self):
        runtime = EconomicRuntime({"a": 0, "b": 10, "c": 10},
            initial_goods={"a": 1, "c": 1}, initial_inventory_cost={"a": 1, "c": 1})
        runtime.set_quote("qa", "a", 5, ttl_ticks=3)
        runtime.set_quote("qc", "c", 5, ttl_ticks=3)
        runtime.begin_tick(1, 0)
        receipts = runtime.execute_batch([
            purchase(runtime, "b-buy", actor="b", seller="a", quote="qa"),
            purchase(runtime, "a-buy", actor="a", seller="c", quote="qc"),
        ])
        self.assertEqual([r.status for r in receipts], ["accepted", "rejected"])
        self.assertEqual((runtime.account_view("a").cash_minor, runtime.account_view("a").available_cash_minor), (5, 0))
        self.assert_balanced(runtime)
        runtime.begin_tick(2, 1)
        receipt = runtime.execute_batch([
            purchase(runtime, "a-next", actor="a", seller="c", quote="qc")], execution_seq_start=2)[0]
        self.assertEqual(receipt.status, "accepted")
        self.assert_balanced(runtime)

    def test_action_quote_takes_effect_only_next_tick(self):
        runtime = market()
        quote_cmd = command(runtime, "quote", "firm", QuoteIntent(intent_id="intent:quote",
            quote_id="q2", unit_price_minor=7, ttl_ticks=2))
        results = runtime.execute_batch([quote_cmd, purchase(runtime, "too-soon", quote="q2", price=7)])
        self.assertEqual(results[1].reason_codes, ["quote_not_effective"])
        runtime.begin_tick(2, 1)
        receipt = runtime.execute_batch([purchase(runtime, "now", quote="q2", price=7)], execution_seq_start=2)[0]
        self.assertEqual(receipt.cash_delta_minor, -7)
        self.assert_balanced(runtime)

    def test_quote_limit_expiry_and_self_purchase(self):
        runtime = market()
        results = runtime.execute_batch([
            purchase(runtime, "limit", price=4), purchase(runtime, "self", actor="firm"),
            purchase(runtime, "missing", quote="missing"),
        ])
        self.assertEqual([r.reason_codes[0] for r in results],
                         ["price_limit_exceeded", "self_purchase", "quote_identity_mismatch"])
        self.assert_balanced(runtime)

    def test_boundary_work_is_forbidden_after_decision_execution(self):
        runtime = market()
        hold = command(runtime, "hold", "buyer", HoldIntent(intent_id="intent:hold"))
        runtime.execute_batch([hold])
        before = runtime.export_state()
        with self.assertRaisesRegex(ValueError, "decision barrier"):
            runtime.accrue_wage("labor", "firm", "buyer", 1)
        self.assertEqual(runtime.export_state(), before)

    def test_unexpected_exception_rolls_back_previous_commands(self):
        runtime = market()
        hold = command(runtime, "existing", "buyer", HoldIntent(intent_id="intent:existing"))
        runtime.execute_batch([hold], execution_seq_start=9)
        batch = [purchase(runtime, "first"), purchase(runtime, "second")]
        before = runtime.export_state()
        original = runtime._execute_one
        calls = 0

        def failing(command_value, sequence, *, quality_failure):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected failure after an earlier successful settlement")
            return original(command_value, sequence, quality_failure=quality_failure)

        with patch.object(runtime, "_execute_one", side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                runtime.execute_batch(batch, execution_seq_start=10)
        self.assertEqual(runtime.export_state(), before)
        restored = market()
        restored.import_state(before)
        receipts = runtime.execute_batch(batch, execution_seq_start=10)
        self.assertEqual([r.execution_seq for r in receipts], [11, 12])
        self.assertEqual(restored.execute_batch(batch, execution_seq_start=10), receipts)
        self.assertEqual(restored.export_state(), runtime.export_state())
        restored.validate_state(full_history=True)

    def test_export_import_is_exact_and_export_has_no_live_aliases(self):
        runtime = market()
        runtime.execute_batch([purchase(runtime, "buy")])
        state = runtime.export_state()
        restored = EconomicRuntime({"buyer": 100, "firm": 100}, initial_goods={"firm": 4},
            initial_inventory_cost={"firm": 12}, labor_cost_per_unit_minor=3)
        restored.import_state(state)
        self.assertEqual(restored.export_state(), state)
        self.assertEqual(content_hash(restored.export_state()), content_hash(state))
        state["balances"]["buyer"]["cash"] = 999
        self.assertEqual(runtime.account_view("buyer").cash_minor, 95)
        self.assertEqual(restored.account_view("buyer").cash_minor, 95)
        with self.assertRaises((TypeError, ValidationError)):
            runtime.account_view("buyer").cash_minor = 999

    def test_restore_cannot_replace_configured_economic_identity(self):
        runtime = market()
        runtime.execute_batch([purchase(runtime, "buy")])
        before = runtime.export_state()
        mutations = {
            "currency_id": "USD", "good_id": "another_good", "cash_scale": 10,
            "labor_cost_per_unit_minor": 4,
            "initial_cash": {"buyer": 101, "firm": 99},
            "initial_goods": {"buyer": 0, "firm": 5},
            "initial_inventory_cost": {"buyer": 0, "firm": 13},
        }
        for key, value in mutations.items():
            state = deepcopy(before)
            state[key] = value
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "configured economic identity"):
                runtime.import_state(state)
            self.assertEqual(runtime.export_state(), before)

    def test_cache_tamper_is_rejected_without_mutating_live_book(self):
        runtime = market()
        runtime.execute_batch([purchase(runtime, "buy")])
        before = runtime.export_state()
        for section in ("balances", "goods"):
            state = deepcopy(before)
            if section == "balances":
                state["balances"]["buyer"]["cash"] += 1
                state["balances"]["buyer"]["opening_equity"] -= 1
            else:
                state["goods"]["buyer"] += 1
            with self.subTest(section=section), self.assertRaises(ValueError):
                runtime.import_state(state)
            self.assertEqual(runtime.export_state(), before)

    def test_balanced_but_forged_journal_is_rejected(self):
        runtime = market()
        runtime.execute_batch([purchase(runtime, "buy")])
        before = runtime.export_state()
        state = deepcopy(before)
        state["journal"][-1]["postings"][0]["amount_minor"] += 1
        state["journal"][-1]["postings"][1]["amount_minor"] += 1
        with self.assertRaisesRegex(ValueError, "canonical"):
            runtime.import_state(state)
        self.assertEqual(runtime.export_state(), before)

    def test_forged_journal_metadata_or_currency_is_rejected(self):
        runtime = market()
        runtime.execute_batch([purchase(runtime, "buy")])
        before = runtime.export_state()
        for change in ("details", "currency", "boolean_detail"):
            state = deepcopy(before)
            if change == "details":
                state["journal"][-1]["details"]["gross_minor"] += 1
            elif change == "currency":
                state["journal"][-1]["postings"][0]["currency_id"] = "USD"
            else:
                # Python equality equates 1 and True; canonical hashes must not.
                state["journal"][-1]["details"]["accepted_quantity"] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                runtime.import_state(state)
            self.assertEqual(runtime.export_state(), before)

    def test_strict_state_rejects_unknown_fields_version_and_duplicate_events(self):
        runtime = market()
        before = runtime.export_state()
        for change in ("extra", "version", "duplicate"):
            state = deepcopy(before)
            if change == "extra":
                state["another_cash_authority"] = {}
            elif change == "version":
                state["schema_version"] = 4
            else:
                state["journal"].append(deepcopy(state["journal"][0]))
            with self.subTest(change=change), self.assertRaises(ValueError):
                runtime.import_state(state)
            self.assertEqual(runtime.export_state(), before)

    def test_stale_command_and_duplicate_sequence_are_atomic(self):
        runtime = market()
        cmd = purchase(runtime, "first")
        before = runtime.export_state()
        with self.assertRaisesRegex(ValueError, "Stale"):
            runtime.execute_batch([cmd.model_copy(update={"world_version": 1})])
        self.assertEqual(runtime.export_state(), before)
        runtime.execute_batch([cmd])
        before = runtime.export_state()
        with self.assertRaisesRegex(ValueError, "increasing execution"):
            runtime.execute_batch([purchase(runtime, "second")])
        self.assertEqual(runtime.export_state(), before)

    def test_sparse_receipt_retries_share_one_history_scan_per_batch(self):
        book = market()

        def hold(label):
            return command(book, label, "buyer", HoldIntent(intent_id=f"intent:{label}"))

        early, recent = hold("early"), hold("recent")
        book.execute_batch([early], execution_seq_start=4)
        original, = book.execute_batch([recent], execution_seq_start=11)
        batch = [recent, hold("third"), hold("fourth")]
        with patch.object(economic_runtime, "max", wraps=max, create=True) as maximum:
            receipts = book.execute_batch(batch, execution_seq_start=11)
            self.assertEqual([r.execution_seq for r in receipts], [12, 13, 14])
            self.assertIs(receipts[0], original)
            maximum.assert_called_once()
            maximum.reset_mock()
            self.assertEqual(book.execute_batch(batch, execution_seq_start=11), receipts)
            maximum.assert_not_called()

        committed = book.export_state()
        with self.assertRaisesRegex(ValueError, "increasing execution sequences"):
            book.execute_batch([early, hold("in-the-gap")], execution_seq_start=4)
        self.assertEqual(book.export_state(), committed)
        book.validate_state(full_history=True)

    def test_false_quality_receipt_cannot_be_constructed(self):
        with self.assertRaises(ValidationError):
            EconomicExecutionReceipt(command_id="c", decision_id="d", actor_id="a", execution_seq=1,
                tick=1, world_version=0, status="rejected", reason_codes=["insufficient_cash"],
                executed_quantity=0, outcome="rejected", quality_failure=True)
        with self.assertRaises(ValidationError):
            EconomicExecutionReceipt(command_id="c", decision_id="d", actor_id="a", execution_seq=1,
                tick=1, world_version=0, status="accepted", executed_quantity=1, outcome="hold")


if __name__ == "__main__":
    unittest.main()
