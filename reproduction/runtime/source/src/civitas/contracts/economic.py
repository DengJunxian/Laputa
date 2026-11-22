"""M0 economic v4 contracts; the existing v1--v3 wire forms are unchanged."""
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .base import Digest, Id, NonNegative, Positive, Record, require_unique
from .decisions import Command, DecisionBundle, HoldIntent
from .portfolio import MAX_INT
from .receipts import ExecutionReceipt

Amount = Annotated[int, Field(ge=0, le=MAX_INT)]
PositiveAmount = Annotated[int, Field(gt=0, le=MAX_INT)]
SignedAmount = Annotated[int, Field(ge=-MAX_INT, le=MAX_INT)]


class PurchaseIntent(Record):
    kind: Literal["purchase"] = "purchase"
    intent_id: Id
    seller_id: Id
    quote_id: Id
    quantity_units: PositiveAmount
    max_unit_price_minor: PositiveAmount
    allow_partial: bool = False


class ConsumeIntent(Record):
    kind: Literal["consume"] = "consume"
    intent_id: Id
    quantity_units: PositiveAmount


class WithdrawDepositIntent(Record):
    kind: Literal["withdraw_deposit"] = "withdraw_deposit"
    intent_id: Id
    bank_id: Id
    notice_fact_id: Id
    amount_minor: PositiveAmount


class CreditApplicationIntent(Record):
    kind: Literal["apply_credit"] = "apply_credit"
    intent_id: Id
    bank_id: Id
    offer_fact_id: Id
    amount_minor: PositiveAmount


class QuoteIntent(Record):
    kind: Literal["quote"] = "quote"
    intent_id: Id
    quote_id: Id
    unit_price_minor: PositiveAmount
    ttl_ticks: Positive = 1


class LaborAcceptIntent(Record):
    kind: Literal["accept_labor_offer"] = "accept_labor_offer"
    intent_id: Id
    offer_id: Id
    max_switch_fee_minor: Amount | None = Field(default=None, exclude_if=lambda value: value is None)


class LaborApplyIntent(Record):
    kind: Literal["apply_labor_vacancy"] = "apply_labor_vacancy"
    intent_id: Id
    vacancy_id: Id


CooperationAction = Literal["cooperate", "defect", "decline"]


class CooperationIntent(Record):
    kind: Literal["cooperation_choice"] = "cooperation_choice"
    intent_id: Id
    opportunity_id: Id
    action: CooperationAction


class SecurityOrderIntent(Record):
    kind: Literal["security_order"] = "security_order"
    intent_id: Id
    asset_id: Id
    side: Literal["buy", "sell"]
    price_ticks: PositiveAmount
    quantity_units: PositiveAmount


EconomicIntent = (HoldIntent | PurchaseIntent | ConsumeIntent | QuoteIntent | LaborAcceptIntent
                  | LaborApplyIntent | CooperationIntent | SecurityOrderIntent | WithdrawDepositIntent | CreditApplicationIntent)


class NetworkProposal(Record):
    schema_version: Literal[1] = 1
    introduction_message_id: Id
    candidate_id: Id
    fee_limit_minor: PositiveAmount | None = Field(default=None, exclude_if=lambda value: value is None)


class NetworkWithdrawal(Record):
    schema_version: Literal[1] = 1
    kind: Literal["withdraw"] = "withdraw"
    edge_delta_id: Id


class CooperationPartnerProposal(Record):
    kind: Literal["cooperation_partner"] = "cooperation_partner"
    option_id: Id
    candidate_id: Id
    fee_limit_minor: Amount


class EconomicDecisionBundle(DecisionBundle):
    schema_version: Literal[4] = 4
    tick: Positive
    economic_intents: list[EconomicIntent] = Field(default_factory=list)
    network_proposals: list[NetworkProposal | NetworkWithdrawal | CooperationPartnerProposal] = Field(
        default_factory=list, exclude_if=lambda value: not value)


class EconomicCommand(Command):
    schema_version: Literal[4] = 4
    tick: Positive
    batch_id: Id
    observation_version: NonNegative
    world_version: NonNegative
    intent: EconomicIntent


class EconomicExecutionReceipt(ExecutionReceipt):
    schema_version: Literal[4] = 4
    tick: Positive
    world_version: NonNegative
    status: Literal["accepted", "rejected", "partially_executed"]
    unit: Literal["goods", "labor_minutes", "cooperation_choice", "security_units", "cash_minor"] = "goods"
    executed_quantity: Amount
    outcome: Literal["hold", "quote_updated", "purchased", "consumed",
                     "delivery_failed_refunded", "labor_offer_accepted", "cooperation_choice_recorded",
                     "security_filled", "security_unfilled", "withdrawal_queued", "credit_application_recorded", "rejected"]
    counterparty_id: Id | None = None
    cash_delta_minor: SignedAmount = 0
    goods_delta_units: SignedAmount = 0
    accepted_quantity: Amount = 0
    quality_failure: bool = False
    refunded_minor: Amount = 0

    @model_validator(mode="after")
    def economic_semantics(self):
        require_unique(self.ledger_tx_ids, "economic transaction reference")
        if (self.status == "rejected") != (self.outcome == "rejected"):
            raise ValueError("Rejected economic receipt must have rejected outcome")
        if self.status == "rejected" and (self.quality_failure or self.cash_delta_minor
                or self.goods_delta_units or self.refunded_minor or self.accepted_quantity):
            raise ValueError("A rejected proposal is not a settled economic failure")
        if self.quality_failure != (self.outcome == "delivery_failed_refunded"):
            raise ValueError("Quality failure requires an accepted, refunded delivery event")
        if self.quality_failure and (self.status != "accepted" or not self.ledger_tx_ids
                or self.executed_quantity or self.goods_delta_units or self.cash_delta_minor
                or not self.accepted_quantity or not self.refunded_minor or self.counterparty_id is None):
            raise ValueError("Delivery failure must refund an accepted order without delivery")
        if self.status == "partially_executed" and self.outcome not in ("purchased", "security_filled"):
            raise ValueError("Only a settled delivery or security fill can be partially executed")
        recorded = self.outcome in ("withdrawal_queued", "credit_application_recorded")
        if self.unit == "cash_minor" or recorded:
            if (self.unit != "cash_minor" or not (recorded or self.outcome == "rejected")
                    or self.executed_quantity or self.cash_delta_minor or self.goods_delta_units or self.refunded_minor
                    or recorded != bool(self.ledger_tx_ids) or recorded != bool(self.accepted_quantity)
                    or recorded != (self.counterparty_id is not None)):
                raise ValueError("A bank request records a nominal amount with zero current settlement")
        if self.unit == "security_units" or self.outcome.startswith("security_"):
            if (self.unit != "security_units" or self.outcome not in ("security_filled", "security_unfilled", "rejected")
                    or self.goods_delta_units or self.counterparty_id is not None or self.refunded_minor
                    or (self.outcome == "security_filled") != bool(self.ledger_tx_ids)
                    or (self.outcome == "security_filled") != bool(self.executed_quantity)
                    or (self.outcome == "security_filled") != bool(self.cash_delta_minor)
                    or self.executed_quantity > self.accepted_quantity
                    or (self.status == "partially_executed") != (0 < self.executed_quantity < self.accepted_quantity)):
                raise ValueError("Security receipt differs from its session executions")
        if self.outcome in ("hold", "quote_updated") and (
                self.status != "accepted" or self.executed_quantity or self.accepted_quantity
                or self.cash_delta_minor or self.goods_delta_units or self.refunded_minor
                or self.ledger_tx_ids or self.counterparty_id is not None):
            raise ValueError("Hold/quote is not a financial or physical transfer")
        if self.outcome == "labor_offer_accepted" or self.unit == "labor_minutes":
            if (self.unit != "labor_minutes" or self.outcome not in ("labor_offer_accepted", "rejected")
                    or self.executed_quantity or self.accepted_quantity or self.cash_delta_minor
                    or self.goods_delta_units or self.refunded_minor or self.ledger_tx_ids
                    or (self.status == "accepted" and self.counterparty_id is None)):
                raise ValueError("Labor acceptance only schedules a contract")
        if self.outcome == "cooperation_choice_recorded" or self.unit == "cooperation_choice":
            if (self.unit != "cooperation_choice" or self.outcome not in ("cooperation_choice_recorded", "rejected")
                    or self.executed_quantity or self.accepted_quantity or self.cash_delta_minor
                    or self.goods_delta_units or self.refunded_minor or self.ledger_tx_ids):
                raise ValueError("Cooperation choice receipts do not settle the round")
        if self.outcome == "purchased" and (
                not self.executed_quantity or self.accepted_quantity != self.executed_quantity
                or self.goods_delta_units != self.executed_quantity or self.cash_delta_minor >= 0
                or not self.ledger_tx_ids or self.refunded_minor or self.counterparty_id is None):
            raise ValueError("Purchase receipt must describe the settled payment and delivery")
        if self.outcome == "consumed" and (
                self.status != "accepted" or not self.executed_quantity
                or self.accepted_quantity != self.executed_quantity
                or self.goods_delta_units != -self.executed_quantity or self.cash_delta_minor
                or not self.ledger_tx_ids or self.refunded_minor or self.counterparty_id is not None):
            raise ValueError("Consumption must remove owned goods without a second payment")
        return self


class LaborOffer(Record):
    offer_id: Id
    worker_id: Id
    employer_id: Id
    offered_tick: Positive
    start_tick: Positive
    end_tick_exclusive: Positive
    work_minutes: Literal[480] = 480
    wage_minor: PositiveAmount

    @model_validator(mode="after")
    def period(self):
        if self.start_tick - self.offered_tick not in (1, 2) or self.end_tick_exclusive <= self.start_tick:
            raise ValueError("Labor offer must start after one or two boundaries and have a positive term")
        return self


class LaborContract(Record):
    contract_id: Id
    offer: LaborOffer
    activation_failed: bool = False
    early_end_tick: Positive | None = Field(default=None, exclude_if=lambda value: value is None)


class PayrollInstallment(Record):
    contract_id: Id
    installment: Literal[2] | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def reservation_id(self) -> str:
        return self.contract_id if self.installment is None else f"{self.contract_id}:installment:2"


class FirstPayrollHold(PayrollInstallment):
    amount_minor: PositiveAmount
    due_tick: Positive
    status: Literal["held", "paid", "released"] = "held"
    payment_tx_id: Id | None = None

    @model_validator(mode="after")
    def settlement(self):
        if (self.payment_tx_id is not None) != (self.status == "paid"):
            raise ValueError("Only a paid payroll hold has a settlement transaction")
        return self


class LaborVacancyClosure(Record):
    vacancy_id: Id
    tick: Positive
    status: Literal["not_opened", "unfilled", "unfunded", "filled"]
    applicant_ids: list[Id]
    available_cash_minor: Amount | None
    first_payroll_minor: Positive | None

    @model_validator(mode="after")
    def closure(self):
        require_unique(self.applicant_ids, "vacancy applicant")
        if self.status == "not_opened":
            if self.applicant_ids or self.available_cash_minor is not None or self.first_payroll_minor is not None:
                raise ValueError("An unopened vacancy has no applicants or funding decision")
        elif self.available_cash_minor is None or self.first_payroll_minor is None:
            raise ValueError("A closed vacancy needs its funding decision")
        else:
            expected = "unfilled"
            if self.applicant_ids:
                expected = "filled" if self.available_cash_minor >= self.first_payroll_minor else "unfunded"
            if self.status != expected:
                raise ValueError("Vacancy closure disagrees with applications or funding")
        return self


class GoodsQuote(Record):
    quote_id: Id
    seller_id: Id
    good_id: Id
    unit_price_minor: PositiveAmount
    effective_tick: Positive
    expires_after_tick: Positive

    @model_validator(mode="after")
    def period(self):
        if self.expires_after_tick < self.effective_tick:
            raise ValueError("Quote expiry precedes activation")
        return self


class EconomicAccountView(Record):
    actor_id: Id
    currency_id: Id
    good_id: Id
    cash_scale: PositiveAmount
    cash_minor: Amount
    available_cash_minor: Amount
    goods_units: Amount
    inventory_cost_minor: Amount
    work_in_progress_minor: Amount
    wage_receivable_minor: Amount
    wage_payable_minor: Amount
    wage_income_minor: Amount
    sales_income_minor: Amount
    expenses_minor: Amount
    net_worth_minor: SignedAmount
    reserved_payroll_minor: Amount = Field(default=0, exclude_if=lambda value: value == 0)


AccountCode = Literal["cash", "inventory", "work_in_progress", "wage_receivable",
                      "wage_payable", "opening_equity", "wage_income", "sales_income",
                      "cost_of_goods_sold", "consumption_expense", "spoilage_expense",
                      "purchase_advance", "customer_advance", "link_fee_expense", "link_fee_income",
                      "labor_switch_fee_expense", "labor_switch_fee_income", "cooperation_expense", "cooperation_income",
                      "wage_variance_expense",
                      "tax_expense", "tax_receivable", "transfer_expense", "disclosure_expense",
                      "tax_payable", "tax_income", "transfer_income", "disclosure_income",
                      "bank_reserve", "deposit_asset", "loan_asset", "interest_asset", "interest_expense",
                      "credit_loss", "payment_expense", "deposit_loss", "guarantee_expense",
                      "reserve_payable", "deposit_payable", "loan_payable", "interest_payable",
                      "interest_income", "loan_allowance", "credit_recovery", "payment_income", "debt_release_income"]


class EconomicPosting(Record):
    posting_id: Id
    tx_id: Id
    entity_id: Id
    currency_id: Id
    account_code: AccountCode
    side: Literal["debit", "credit"]
    amount_minor: PositiveAmount


class GoodsMovement(Record):
    movement_id: Id
    kind: Literal["opening", "production", "transfer", "consumption", "scrap"]
    resource_id: Id
    quantity_units: PositiveAmount
    from_owner: Id | None = None
    to_owner: Id | None = None

    @model_validator(mode="after")
    def endpoints(self):
        if self.kind == "transfer":
            if self.from_owner is None or self.to_owner is None or self.from_owner == self.to_owner:
                raise ValueError("Transfer requires distinct owners")
        elif self.kind in ("opening", "production"):
            if self.from_owner is not None or self.to_owner is None:
                raise ValueError("Creation requires one destination")
        elif self.from_owner is None or self.to_owner is not None:
            raise ValueError("Use or loss requires one source")
        return self


class EconomicJournalEntry(Record):
    tx_id: Id
    event_id: Id
    kind: Literal["opening", "wage_accrual", "wage_payment", "production",
                  "purchase", "delivery_failure", "consumption", "payroll_reserve", "payroll_release", "link_fee",
                  "labor_switch_fee", "cooperation_settlement", "fiscal", "bank", "bank_withdrawal",
                  "credit_application", "security_trade"]
    tick: NonNegative
    payload_hash: Digest
    payload: dict[str, Any]
    postings: list[EconomicPosting] = Field(default_factory=list)
    goods_movements: list[GoodsMovement] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identities(self):
        require_unique([p.posting_id for p in self.postings], "posting")
        require_unique([m.movement_id for m in self.goods_movements], "goods movement")
        if any(p.tx_id != self.tx_id for p in self.postings):
            raise ValueError("Posting transaction mismatch")
        return self


class WageObligation(Record):
    employer_id: Id
    worker_id: Id
    outstanding_minor: Amount


class PayrollCashReservation(PayrollInstallment):
    employer_id: Id
    worker_id: Id
    amount_minor: PositiveAmount
    due_tick: Positive


class EconomicRuntimeState(Record):
    """Strict participant envelope; balances and goods are verified journal projections."""
    schema_version: Literal[1, 2] = 1
    state_id: Literal["economic_runtime"] = "economic_runtime"
    backend: Literal["fixedpoint_m0_economy", "fixedpoint_institutional_economy"] = "fixedpoint_m0_economy"
    institution_config_hash: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    bank_positions: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)
    currency_id: Id
    good_id: Id
    cash_scale: PositiveAmount
    labor_cost_per_unit_minor: PositiveAmount
    initial_cash: dict[Id, Amount]
    initial_goods: dict[Id, Amount]
    initial_inventory_cost: dict[Id, Amount]
    tick: NonNegative
    world_version: NonNegative
    balances: dict[Id, dict[AccountCode, SignedAmount]]
    goods: dict[Id, Amount]
    wage_obligations: list[WageObligation]
    journal: list[EconomicJournalEntry]
    quotes: list[GoodsQuote]
    commands: list[EconomicCommand]
    receipts: list[EconomicExecutionReceipt]
    decision_cash_budget: dict[Id, Amount] | None = None
    payroll_reservations: list[PayrollCashReservation] = Field(default_factory=list, exclude_if=lambda value: not value)
