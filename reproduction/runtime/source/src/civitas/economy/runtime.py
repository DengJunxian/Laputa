"""Exact M0 wage/production/goods accounting behind the existing transaction port.

The journal is authoritative.  Account balances and physical quantities are
checked, rebuildable projections, never a second writable economic state.
System work is authorized by the scenario; LLM commands cannot accrue wages or
produce resources.  Institutional actions use the same book. Durable commit belongs to the world.
"""
from copy import copy
from dataclasses import dataclass
from itertools import islice

from pydantic import Field
from core.experiments.strict_types import SealedJSONRowsV1

from civitas.contracts.base import Id, Record, content_hash, require_unique
from civitas.contracts.decisions import HoldIntent
from civitas.contracts.economic import (
    Amount, ConsumeIntent, EconomicAccountView, EconomicCommand,
    EconomicExecutionReceipt, EconomicJournalEntry, EconomicPosting,
    EconomicRuntimeState, GoodsMovement, GoodsQuote, PayrollCashReservation, PayrollInstallment, PositiveAmount,
    PurchaseIntent, QuoteIntent, SECURITY_ASSET,
)
from civitas.contracts.portfolio import PortfolioTrade
from civitas.contracts.portfolio import MAX_INT
from civitas.mechanisms.games import bilateral_cooperation_deltas
from civitas.resources.fixedpoint import checked, decimal_minor, notional


from . import banking as banking_domain
from . import fiscal as fiscal_domain


class EconomicReject(ValueError):
    """A valid proposal lacks an available resource; not a delivery default."""


@dataclass(frozen=True, slots=True)
class _ExecutionPrefix:
    tick: int
    commands: tuple[tuple[str, EconomicCommand], ...]
    receipts: tuple[EconomicExecutionReceipt, ...]
    journal: tuple[EconomicJournalEntry, ...]
    quotes: dict[str, GoodsQuote]
    last_sequence: int
    has_current_execution: bool
    received_this_tick: dict[str, int]


class SecurityOpening(Record):
    units: dict[Id, PositiveAmount]
    price_minor: PositiveAmount
    mark_tick: PositiveAmount


class _SecurityMark(Record):
    price_minor: PositiveAmount


SECURITY_MARK_EVENT = "security:mark"


class _Opening(Record):
    cash: dict[Id, Amount]
    goods: dict[Id, Amount]
    inventory_cost: dict[Id, Amount]
    security: SecurityOpening | None = Field(default=None, exclude_if=lambda value: value is None)


class _Wage(Record):
    employer_id: Id
    worker_id: Id
    amount_minor: PositiveAmount
    reservation_id: Id | None = Field(default=None, exclude_if=lambda value: value is None)


class _WageAccrual(_Wage):
    production_cost_minor: Amount | None = Field(default=None, exclude_if=lambda value: value is None)


class _Production(Record):
    firm_id: Id
    quantity_units: PositiveAmount
    labor_cost_minor: PositiveAmount


class _Purchase(Record):
    buyer_id: Id
    seller_id: Id
    quote_id: Id
    quantity_units: PositiveAmount
    unit_price_minor: PositiveAmount


class _Consumption(Record):
    actor_id: Id
    quantity_units: PositiveAmount


class _LinkFee(Record):
    delta_id: Id
    payer_id: Id
    recipient_id: Id
    amount_minor: PositiveAmount


class _LaborSwitchFee(Record):
    contract_id: Id
    payer_id: Id
    recipient_id: Id
    amount_minor: PositiveAmount


class _CooperationSettlement(Record):
    round_id: Id
    offer_tick: PositiveAmount
    sponsor_id: Id
    contributions: dict[Id, Amount]


_PAYLOADS = {
    "opening": _Opening, "wage_accrual": _WageAccrual, "wage_payment": _Wage,
    "production": _Production, "purchase": _Purchase,
    "delivery_failure": _Purchase, "consumption": _Consumption,
    "payroll_reserve": PayrollCashReservation, "payroll_release": PayrollInstallment,
    "link_fee": _LinkFee,
    "labor_switch_fee": _LaborSwitchFee,
    "cooperation_settlement": _CooperationSettlement,
    "fiscal": Record,
    "bank": banking_domain.BankEvent,
    "bank_withdrawal": banking_domain.BankWithdrawalEvent,
    "credit_application": banking_domain.CreditApplicationEvent,
    "security_trade": PortfolioTrade,
    "security_mark": _SecurityMark,
}
_BANK_REQUESTS = {"withdraw_deposit": ("bank_withdrawal", "withdrawal_queued"),
                  "apply_credit": ("credit_application", "credit_application_recorded")}
_BANK_EVENTS = ("bank", "bank_withdrawal", "credit_application")
_COMMAND_EVENTS = ("purchase", "delivery_failure", "consumption", "bank_withdrawal", "credit_application")
_DECISION_EVENTS = (*_COMMAND_EVENTS, "payroll_reserve", "security_trade")
_DEBIT = frozenset({"cash", "inventory", "work_in_progress", "wage_receivable",
    "cost_of_goods_sold", "consumption_expense", "spoilage_expense", "purchase_advance", "link_fee_expense",
    "labor_switch_fee_expense", "cooperation_expense", "wage_variance_expense"})
_CREDIT = frozenset({"wage_payable", "opening_equity", "wage_income", "sales_income",
                     "customer_advance", "link_fee_income", "labor_switch_fee_income", "cooperation_income"})
_ACCOUNT_CODES = _DEBIT | _CREDIT
_BASE_ACCOUNT_CODES = _ACCOUNT_CODES - {"link_fee_expense", "link_fee_income", "labor_switch_fee_expense",
                                        "labor_switch_fee_income", "cooperation_expense", "cooperation_income",
                                        "wage_variance_expense"}
_ACCOUNTS = tuple(sorted(_BASE_ACCOUNT_CODES))
_EXPENSES = ("cost_of_goods_sold", "consumption_expense", "spoilage_expense", "link_fee_expense",
             "labor_switch_fee_expense", "cooperation_expense", "wage_variance_expense")
_BACKENDS = {1: "fixedpoint_m0_economy", 2: "fixedpoint_institutional_economy",
             3: "fixedpoint_joint_security_economy"}


class EconomicRuntime:
    """WorldKernel transaction participant for one currency and one divisible good.

    Quantities are integer goods units; prices/costs are integer currency minor
    units.  ``labor_cost_per_unit_minor`` is an explicit fixed production recipe.
    Physical cash remains conserved. An explicit banking regime can create deposit claims.
    """

    participant_id = "economic_runtime"
    state_schema_version = 1

    def __init__(self, initial_cash, *, initial_goods=None, initial_inventory_cost=None,
                 currency_id="SIM", good_id="good", cash_scale=1,
                 labor_cost_per_unit_minor=1, fiscal=None, banking=None, security_opening=None):
        self.fiscal = fiscal_domain.FiscalPolicy.model_validate(fiscal) if fiscal is not None else None
        self.banking = banking_domain.BankingConfig.model_validate(banking) if banking is not None else None
        self.state_schema_version = 2 if self.fiscal is not None or self.banking is not None else 1
        self._institution_hash = content_hash({'fiscal': self.fiscal, 'banking': self.banking}) if self.state_schema_version == 2 else None
        self._security_opening = None if security_opening is None else SecurityOpening.model_validate(security_opening)
        self._bank_positions = banking_domain.BankPositions()
        self._debit_codes, self._credit_codes = set(_DEBIT), set(_CREDIT)
        self._expense_codes = _EXPENSES
        if self.fiscal is not None:
            self._debit_codes.update(fiscal_domain.DEBIT_CODES)
            self._credit_codes.update(fiscal_domain.CREDIT_CODES)
            self._expense_codes += ("tax_expense", "transfer_expense", "disclosure_expense")
        if self.banking is not None:
            self._debit_codes.update(banking_domain.DEBIT_CODES)
            self._credit_codes.update(banking_domain.CREDIT_CODES)
            self._expense_codes += banking_domain.EXPENSES
        if self._security_opening is not None:
            if (self.banking is None or self.fiscal is None or good_id == SECURITY_ASSET
                    or not self._security_opening.units
                    or not self._security_opening.units.keys() <= initial_cash.keys()):
                raise ValueError("Joint security opening requires distinct resources and registered economic owners")
            self.state_schema_version = 3
            self._debit_codes.update(("security_inventory", "security_disposal_cost"))
            self._credit_codes.add("security_sale_proceeds")
        self._allowed_codes = self._debit_codes | self._credit_codes
        opening = _Opening(cash=initial_cash, goods={} if initial_goods is None else initial_goods,
                           inventory_cost={} if initial_inventory_cost is None else initial_inventory_cost)
        if not opening.cash or not set(opening.goods) <= set(opening.cash):
            raise ValueError("Nonempty cash owners and matching goods owners required")
        if not set(opening.inventory_cost) <= set(opening.cash):
            raise ValueError("Unknown inventory cost owner")
        if type(currency_id) is not str or not currency_id or type(good_id) is not str or not good_id:
            raise ValueError("Nonempty currency and good identifiers required")
        if currency_id == good_id:
            raise ValueError("Currency and physical good must be distinct")
        decimal_minor("0", cash_scale)
        checked(cash_scale)
        if checked(labor_cost_per_unit_minor) == 0:
            raise ValueError("A positive labor cost recipe is required")
        self.currency_id, self.good_id = currency_id, good_id
        self.cash_scale = cash_scale
        self.labor_cost_per_unit_minor = labor_cost_per_unit_minor
        actors = sorted(opening.cash)
        self._initial_cash = {a: opening.cash[a] for a in actors}
        if (self.banking is not None and self.banking.requires_treasury
                and (self.fiscal is None or self._initial_cash.get(fiscal_domain.TREASURY) != self.fiscal.initial_cash_minor
                     or self._initial_cash.get(fiscal_domain.OPERATIONS) != 0)):
            raise ValueError('Funded bank policy requires the configured treasury and operations accounts')
        if self.banking is not None:
            if any(self._initial_cash.get(a) != amount for a, amount in {
                    **self.banking.initial_bank_cash, banking_domain.ISSUER: 0,
                    banking_domain.GUARANTOR: self.banking.guarantee_budget_minor}.items()):
                raise ValueError("Banking opening capital and issuer must match the configured endowments")
            banking_domain.actors_for(self.banking, actors)
        self._initial_goods = {a: opening.goods.get(a, 0) for a in actors}
        self._initial_cost = {a: opening.inventory_cost.get(a, 0) for a in actors}
        self._cash_total = checked(sum(self._initial_cash.values()))
        for actor in actors:
            if not self._initial_goods[actor] and self._initial_cost[actor]:
                raise ValueError("Empty opening inventory must have zero carrying cost")
        self.tick = self.world_version = 0
        self._balances = {a: {k: 0 for k in _ACCOUNTS} for a in actors}
        self._goods = {a: 0 for a in actors}
        self._security_units = {a: 0 for a in actors} if self._security_opening is not None else {}
        self._wages = {}
        self._payroll_reservations = {}
        self._journal = {}
        self._quotes = {}
        self._commands = {}
        self._receipts = {}
        self._history_capture = {}
        self._decision_cash_budget = None
        self._checked_journal = None
        self._checked_execution = None
        self._post_event("opening", "system:opening", {
            "cash": self._initial_cash, "goods": self._initial_goods,
            "inventory_cost": self._initial_cost,
            **({"security": self._security_opening.model_dump()} if self._security_opening is not None else {}),
        })

    @property
    def actor_ids(self):
        return tuple(self._initial_cash)

    @property
    def journal(self):
        return tuple(self._journal.values())

    @property
    def payroll_reservations(self):
        return tuple(self._payroll_reservations[key] for key in sorted(self._payroll_reservations))

    def _reserved_payroll(self, actor_id):
        return sum(r.amount_minor for r in self._payroll_reservations.values() if r.employer_id == actor_id)

    def _available_cash(self, actor_id):
        cash = self._balances[actor_id]["cash"]
        if self._decision_cash_budget is not None:
            cash = min(cash, self._decision_cash_budget[actor_id])
        return cash - self._reserved_payroll(actor_id)

    def _net_worth(self, balances):
        value = (balances["cash"] + balances["inventory"] + balances["work_in_progress"]
                 + balances["wage_receivable"] + balances["wage_payable"] + balances.get("security_inventory", 0))
        if self.fiscal is not None:
            value += balances.get("tax_receivable", 0) + balances.get("tax_payable", 0)
        if self.banking is not None:
            value += sum(balances.get(k, 0) for k in banking_domain.SIGNED_CLAIM_CODES)
        return value

    def account_view(self, actor_id):
        self._require_actor(actor_id)
        b = self._balances[actor_id]
        return EconomicAccountView(actor_id=actor_id, currency_id=self.currency_id,
            good_id=self.good_id, cash_scale=self.cash_scale, cash_minor=b["cash"],
            available_cash_minor=self._available_cash(actor_id), goods_units=self._goods[actor_id],
            reserved_payroll_minor=self._reserved_payroll(actor_id),
            inventory_cost_minor=b["inventory"], work_in_progress_minor=b["work_in_progress"],
            wage_receivable_minor=b["wage_receivable"], wage_payable_minor=-b["wage_payable"],
            wage_income_minor=-b["wage_income"], sales_income_minor=-b["sales_income"],
            expenses_minor=sum(b.get(k, 0) for k in self._expense_codes),
            net_worth_minor=self._net_worth(b))

    def quotes(self):
        """All immutable quotes; observers filter effective/expiry at their barrier."""
        return list(self._quotes.values())

    def security_units(self, actor):
        return self._security_units[actor] if self._security_opening is not None else self._goods[actor]

    @property
    def security_mark(self):
        return self._journal.get(SECURITY_MARK_EVENT)

    def mark_security(self, price_minor):
        self._boundary_only()
        return self._post_event("security_mark", SECURITY_MARK_EVENT, {"price_minor": price_minor})

    def security_wealth(self, actor):
        mark = self.security_mark
        opening = self._security_opening
        units = self._security_units[actor]
        cost = self._balances[actor].get("security_inventory", 0)
        marked = notional(mark.payload["price_minor"], 1, units)
        return {"asset_id": SECURITY_ASSET, "units": units, "cost_minor": cost, "market_value_minor": marked,
                "market_value_change_minor": marked - notional(opening.price_minor, 1, units),
                "marked_net_worth_minor": self.account_view(actor).net_worth_minor - cost + marked,
                "mark_tx_id": mark.tx_id}

    def begin_tick(self, tick, world_version):
        checked(tick)
        checked(world_version)
        if tick != self.tick + 1 or world_version != tick - 1:
            raise ValueError("Economic tick must advance once with its base world version")
        self.tick, self.world_version = tick, world_version
        self._decision_cash_budget = None

    def _boundary_only(self):
        if self.tick < 1 or self._decision_cash_budget is not None:
            raise ValueError("Authorized system work belongs before the decision barrier")

    def banking_report(self):
        if self.banking is None:
            raise ValueError("This run has no banking regime")
        return banking_domain.bank_report(self)

    def banking_period(self):
        self._boundary_only()
        if self.banking is None:
            return

        def apply(action, key):
            event_id = f"bank:{self.tick}:{action}:{key}"
            self._post_event("bank", event_id, {"action": action, "reference": key})

        if self.tick == 1:
            for bank in sorted(self.banking.initial_bank_cash):
                apply("capital", bank)
        for key, queue_item in sorted(self._bank_positions.withdrawals.items()):
            if queue_item.status == "queued" and queue_item.terms.expires_tick < self.tick:
                apply("queue", key)
        for key, loan in sorted(self._bank_positions.loans.items()):
            if loan.next_tick == self.tick and loan.status in ('committed', 'active', 'delinquent'):
                apply("fund" if loan.status == 'committed' else "due", key)
        for op in self.banking.operations:
            if op.tick == self.tick:
                apply("scheduled", op.operation_id)
        support = self.banking.treasury_support
        if support is not None:
            bank = support.bank_id
            if self.tick == support.disbursement_tick:
                apply("support_disburse", bank)
            elif banking_domain.support_collection_due(support, self._bank_positions.support_advances.get(bank),
                                                       self.tick, self._balances[bank].get('bank_reserve', 0)):
                apply("support_collect", bank)
        notice = self.banking.run_notice
        if notice is not None and notice.disclose and self.tick == notice.tick:
            apply("notice", notice.source_request)
        if notice is not None and notice.response_mode == "scripted" and self.tick == notice.tick + 1:
            for actor in sorted(notice.withdrawal_by_recipient):
                apply("reaction", actor)
        queue = sorted((w.terms.tick, w.terms.operation_id) for w in self._bank_positions.withdrawals.values()
                       if w.status == "queued")
        for _, key in queue:
            apply("queue", key)

    def fiscal_period(self):
        self._boundary_only()
        if self.fiscal is not None and (self.tick == self.fiscal.announced_tick or self.tick >= self.fiscal.effective_tick):
            return self._post_event('fiscal', f'fiscal:{self.tick}', {})
        return None

    def settle_link_fee(self, delta_id, payer_id, recipient_id, amount_minor):
        """Settle an authorized edge's fee before current-tick wages or purchases."""
        self._boundary_only()
        return self._post_event("link_fee", f"link-fee:{delta_id}",
            _LinkFee(delta_id=delta_id, payer_id=payer_id, recipient_id=recipient_id,
                     amount_minor=amount_minor).model_dump())

    def settle_labor_switch_fee(self, contract_id, payer_id, recipient_id, amount_minor):
        self._boundary_only()
        return self._post_event("labor_switch_fee", f"labor-switch-fee:{contract_id}",
            _LaborSwitchFee(contract_id=contract_id, payer_id=payer_id, recipient_id=recipient_id,
                            amount_minor=amount_minor).model_dump())

    def settle_cooperation(self, round_id, offer_tick, sponsor_id, contributions):
        self._boundary_only()
        return self._post_event("cooperation_settlement", f"cooperation-settlement:{round_id}",
            _CooperationSettlement(round_id=round_id, offer_tick=offer_tick, sponsor_id=sponsor_id,
                                   contributions=contributions).model_dump())

    def settle_security(self, trade):
        trade = PortfolioTrade.model_validate(trade)
        if trade.tick != self.tick or self.tick < 1:
            raise ValueError("Security settlement requires the current trading session")
        event_id = f"security-fill:{trade.trade_id}"
        previous = event_id in self._journal
        opening_budget = self._decision_cash_budget is None
        if opening_budget:
            self._decision_cash_budget = {a: b["cash"] for a, b in self._balances.items()}
        try:
            entry = self._post_event("security_trade", event_id, trade.model_dump())
        except Exception:
            if opening_budget:
                self._decision_cash_budget = None
            raise
        if not previous:
            self._decision_cash_budget[trade.buyer_id] -= entry.details["gross_minor"]
        return entry

    def set_quote(self, quote_id, seller_id, unit_price_minor, *, effective_tick=1, ttl_ticks=1):
        """Scenario initial/boundary quote; an action quote uses next-tick activation."""
        if self._decision_cash_budget is not None:
            raise ValueError("System quote changes belong before the decision barrier")
        return self._set_quote(quote_id, seller_id, unit_price_minor,
                               effective_tick=effective_tick, ttl_ticks=ttl_ticks)

    def _set_quote(self, quote_id, seller_id, unit_price_minor, *, effective_tick, ttl_ticks):
        self._require_actor(seller_id)
        if checked(ttl_ticks) == 0 or checked(effective_tick) == 0:
            raise ValueError("Positive quote validity required")
        quote = GoodsQuote(quote_id=quote_id, seller_id=seller_id, good_id=self.good_id,
            unit_price_minor=unit_price_minor, effective_tick=effective_tick,
            expires_after_tick=checked(effective_tick + ttl_ticks - 1))
        previous = self._quotes.get(quote_id)
        if previous is not None:
            if previous != quote:
                raise ValueError("quote_id payload conflict")
            return previous
        if effective_tick < max(1, self.tick):
            raise ValueError("Cannot backdate a quote")
        self._quotes[quote_id] = quote
        return quote

    def accrue_wage(self, event_id, employer_id, worker_id, amount_minor, *, production_cost_minor=None):
        """Scenario attests completed work; records a claim, not a cash creation."""
        self._boundary_only()
        return self._post_event("wage_accrual", event_id,
            _WageAccrual(employer_id=employer_id, worker_id=worker_id, amount_minor=amount_minor,
                         production_cost_minor=production_cost_minor).model_dump())

    def pay_wage(self, event_id, employer_id, worker_id, amount_minor, *, reservation_id=None):
        """Pay available cash against a real wage claim; retain any wage arrears."""
        self._boundary_only()
        return self._post_event("wage_payment", event_id,
            _Wage(employer_id=employer_id, worker_id=worker_id, amount_minor=amount_minor,
                  reservation_id=reservation_id).model_dump())

    def reserve_payroll(self, contract_id, employer_id, worker_id, amount_minor, *, due_tick, installment=None):
        reservation = PayrollCashReservation(contract_id=contract_id, employer_id=employer_id,
            worker_id=worker_id, amount_minor=amount_minor, due_tick=due_tick, installment=installment)
        return self._post_event("payroll_reserve", f"payroll-reserve:{reservation.reservation_id}",
                                reservation.model_dump())

    def release_payroll(self, contract_id, *, installment=None):
        self._boundary_only()
        release = PayrollInstallment(contract_id=contract_id, installment=installment)
        return self._post_event("payroll_release", f"payroll-release:{release.reservation_id}", release.model_dump())

    def produce(self, event_id, firm_id, quantity_units, *, labor_cost_minor):
        self._boundary_only()
        return self._post_event("production", event_id,
            _Production(firm_id=firm_id, quantity_units=quantity_units,
                        labor_cost_minor=labor_cost_minor).model_dump())

    def _require_actor(self, actor):
        if actor not in self._balances:
            raise EconomicReject("unknown_actor")

    def _plan(self, kind, event_id, payload, tick):
        """Derive canonical postings from accepted economic facts and prior state."""
        payload = _PAYLOADS[kind].model_validate(payload).model_dump()
        tx_id = f"econ:{event_id}"
        postings, movements, details = [], [], {}

        def post(actor, account, side, amount):
            checked(amount)
            self._require_actor(actor)
            if amount:
                postings.append(EconomicPosting(posting_id=f"{tx_id}:p:{len(postings)}",
                    tx_id=tx_id, entity_id=actor, currency_id=self.currency_id,
                    account_code=account, side=side, amount_minor=amount))

        def move(movement_kind, quantity, source=None, destination=None, *, resource_id=None):
            if quantity:
                movements.append(GoodsMovement(movement_id=f"{tx_id}:g:{len(movements)}",
                    kind=movement_kind, resource_id=self.good_id if resource_id is None else resource_id, quantity_units=quantity,
                    from_owner=source, to_owner=destination))

        if kind == "opening":
            expected = {"cash": self._initial_cash, "goods": self._initial_goods,
                        "inventory_cost": self._initial_cost,
                        **({"security": self._security_opening.model_dump()} if self._security_opening is not None else {})}
            if self._journal or event_id != "system:opening" or tick != 0 or payload != expected:
                raise ValueError("Only the configured opening endowment may initialize the book")
            for actor in self.actor_ids:
                cash, cost, quantity = payload["cash"][actor], payload["inventory_cost"][actor], payload["goods"][actor]
                post(actor, "cash", "debit", cash)
                post(actor, "inventory", "debit", cost)
                security_cost = 0
                if self._security_opening is not None:
                    units = self._security_opening.units.get(actor, 0)
                    security_cost = notional(self._security_opening.price_minor, 1, units)
                    post(actor, "security_inventory", "debit", security_cost)
                    move("opening", units, destination=actor, resource_id=SECURITY_ASSET)
                post(actor, "opening_equity", "credit", checked(cash + cost + security_cost))
                move("opening", quantity, destination=actor)
            details = {"cash_created_minor": self._cash_total, "goods_created_units": sum(payload["goods"].values())}
        elif kind == "security_mark":
            if (self._security_opening is None or tick != self._security_opening.mark_tick
                    or event_id != SECURITY_MARK_EVENT or payload["price_minor"] > self._security_opening.price_minor):
                raise ValueError("Security mark differs from its registered window")
            details = {"asset_id": SECURITY_ASSET, "source": "synthetic_exogenous_mark"}
        elif kind == "bank":
            event = banking_domain.BankEvent.model_validate(payload)
            if self.banking is None or event_id != f"bank:{tick}:{event.action}:{event.reference}":
                raise ValueError("Bank event outside the configured regime")
            details = banking_domain.bank_plan(self, event, tick, post)
        elif kind == "bank_withdrawal":
            details = banking_domain.withdrawal_plan(self, banking_domain.BankWithdrawalEvent.model_validate(payload), tick)
        elif kind == "credit_application":
            details = banking_domain.credit_application_plan(self, banking_domain.CreditApplicationEvent.model_validate(payload), tick)
        elif kind == "fiscal":
            if (self.fiscal is None or event_id != f'fiscal:{tick}'
                    or not (tick == self.fiscal.announced_tick or tick >= self.fiscal.effective_tick)):
                raise ValueError("Fiscal event outside the configured policy")
            details = fiscal_domain.fiscal_plan(self, tick, post)
        elif kind in ("link_fee", "labor_switch_fee"):
            payer, recipient, amount = payload["payer_id"], payload["recipient_id"], payload["amount_minor"]
            self._require_actor(payer)
            self._require_actor(recipient)
            reference = payload["delta_id"] if kind == "link_fee" else payload["contract_id"]
            if tick < 1 or payer == recipient or event_id != f"{kind.replace('_', '-')}:{reference}":
                raise ValueError(f"Invalid {kind.replace('_', ' ')} identity or parties")
            if amount > self._available_cash(payer):
                raise EconomicReject(f"insufficient_{kind}_cash")
            post(payer, f"{kind}_expense", "debit", amount)
            post(payer, "cash", "credit", amount)
            post(recipient, "cash", "debit", amount)
            post(recipient, f"{kind}_income", "credit", amount)
        elif kind == "cooperation_settlement":
            sponsor, contributions = payload["sponsor_id"], payload["contributions"]
            if (sponsor in contributions or tick != payload["offer_tick"] + 1
                    or event_id != f"cooperation-settlement:{payload['round_id']}"):
                raise ValueError("Invalid cooperation settlement identity, parties or tick")
            self._require_actor(sponsor)
            deltas = bilateral_cooperation_deltas(contributions)
            for actor, amount in contributions.items():
                self._require_actor(actor)
                if amount > self._available_cash(actor):
                    raise EconomicReject("insufficient_cooperation_contribution_cash")
            subsidy = sum(deltas.values())
            if subsidy > self._available_cash(sponsor):
                raise EconomicReject("insufficient_cooperation_sponsor_cash")
            deltas[sponsor] = -subsidy
            for actor, delta in sorted(deltas.items()):
                if delta > 0:
                    post(actor, "cash", "debit", delta)
                    post(actor, "cooperation_income", "credit", delta)
                elif delta < 0:
                    post(actor, "cooperation_expense", "debit", -delta)
                    post(actor, "cash", "credit", -delta)
            details = {"cash_delta_minor": deltas, "sponsor_subsidy_minor": subsidy}
        elif kind in ("wage_accrual", "wage_payment"):
            employer, worker, amount = payload["employer_id"], payload["worker_id"], payload["amount_minor"]
            self._require_actor(employer)
            self._require_actor(worker)
            if employer == worker:
                raise EconomicReject("self_employment_unsupported")
            outstanding = self._wages.get((employer, worker), 0)
            if kind == "wage_accrual":
                if "reservation_id" in payload:
                    raise ValueError("Wage accrual does not settle a payroll reservation")
                production_cost = payload.get("production_cost_minor", amount)
                if production_cost > amount:
                    raise ValueError("Standard production cost exceeds the wage accrual")
                post(employer, "work_in_progress", "debit", production_cost)
                post(employer, "wage_variance_expense", "debit", amount - production_cost)
                post(employer, "wage_payable", "credit", amount)
                post(worker, "wage_receivable", "debit", amount)
                post(worker, "wage_income", "credit", amount)
                details = {"accrued_minor": amount, "outstanding_minor": checked(outstanding+amount)}
            else:
                free = self._available_cash(employer)
                if "reservation_id" in payload:
                    held = self._payroll_reservations.get(payload["reservation_id"])
                    if (held is None or (held.employer_id, held.worker_id, held.amount_minor, held.due_tick)
                            != (employer, worker, amount, tick) or outstanding < amount):
                        raise EconomicReject("payroll_reservation_payment_mismatch")
                    paid = amount
                else:
                    paid = min(amount, outstanding, free)
                post(employer, "wage_payable", "debit", paid)
                post(employer, "cash", "credit", paid)
                post(worker, "cash", "debit", paid)
                post(worker, "wage_receivable", "credit", paid)
                details = {"requested_minor": amount, "paid_minor": paid, "outstanding_minor": outstanding-paid}
        elif kind == "payroll_reserve":
            reserve = PayrollCashReservation.model_validate(payload)
            employer, worker = reserve.employer_id, reserve.worker_id
            self._require_actor(employer)
            self._require_actor(worker)
            if (employer == worker or tick < 1 or reserve.due_tick != tick + 2 + int(reserve.installment == 2)
                    or event_id != f"payroll-reserve:{reserve.reservation_id}"):
                raise ValueError("Invalid payroll reservation identity or timing")
            if reserve.installment == 2:
                first = self._payroll_reservations.get(reserve.contract_id)
                if first != reserve.model_copy(update={"installment": None, "due_tick": reserve.due_tick - 1}):
                    raise ValueError("Second payroll reservation requires its matching first installment")
            if reserve.amount_minor > self._available_cash(employer):
                raise EconomicReject("insufficient_payroll_reservation_cash")
        elif kind == "payroll_release":
            release = PayrollInstallment.model_validate(payload)
            held = self._payroll_reservations.get(release.reservation_id)
            first_release = self._journal.get(f"payroll-release:{release.contract_id}")
            before_start = (release.installment == 2 and first_release is not None
                            and first_release.kind == "payroll_release" and first_release.tick == tick
                            and first_release.payload == {"contract_id": release.contract_id})
            if (held is None or (held.contract_id, held.installment) != (release.contract_id, release.installment)
                    or tick != held.due_tick - (2 if before_start else 1)
                    or event_id != f"payroll-release:{release.reservation_id}"):
                raise EconomicReject("payroll_reservation_release_mismatch")
        elif kind == "production":
            actor, quantity, cost = payload["firm_id"], payload["quantity_units"], payload["labor_cost_minor"]
            self._require_actor(actor)
            if cost != notional(self.labor_cost_per_unit_minor, 1, quantity):
                raise EconomicReject("production_recipe_mismatch")
            if cost > self._balances[actor]["work_in_progress"]:
                raise EconomicReject("insufficient_verified_labor_cost")
            post(actor, "inventory", "debit", cost)
            post(actor, "work_in_progress", "credit", cost)
            move("production", quantity, destination=actor)
            details = {"produced_units": quantity, "capitalized_labor_cost_minor": cost}
        elif kind in ("purchase", "delivery_failure", "security_trade"):
            buyer, seller, quantity = payload["buyer_id"], payload["seller_id"], payload["quantity_units"]
            self._require_actor(buyer)
            self._require_actor(seller)
            if buyer == seller:
                raise EconomicReject("self_purchase")
            joint_trade = kind == "security_trade" and self._security_opening is not None
            if kind == "security_trade":
                funded = (tick == self._security_opening.mark_tick if joint_trade else
                          self.good_id.startswith("security:") and self.banking is None and self.fiscal is None)
                asset = SECURITY_ASSET if joint_trade else self.good_id
                if (not funded or payload["asset_id"] != asset or payload["tick"] != tick or not quantity
                        or not payload["price_ticks"] or event_id != f"security-fill:{payload['trade_id']}"):
                    raise ValueError("Security fill is outside its funded asset, identity or session")
                price = payload["price_ticks"]
            else:
                price = payload["unit_price_minor"]
                quote = self._quotes.get(payload["quote_id"])
                if quote is None or (quote.seller_id, quote.unit_price_minor) != (seller, price):
                    raise EconomicReject("quote_identity_mismatch")
                if not quote.effective_tick <= tick <= quote.expires_after_tick:
                    raise EconomicReject("quote_not_effective")
            gross = notional(price, 1, quantity)
            if gross > self._available_cash(buyer):
                raise EconomicReject("insufficient_cash")
            held = self._security_units[seller] if joint_trade else self._goods[seller]
            inventory = "security_inventory" if joint_trade else "inventory"
            if quantity > held:
                raise EconomicReject("insufficient_goods")
            cost = self._balances[seller].get(inventory, 0) * quantity // held
            details = {"accepted_quantity": quantity, "gross_minor": gross, "carrying_cost_minor": cost}
            if kind in ("purchase", "security_trade"):
                post(buyer, inventory, "debit", gross)
                post(buyer, "cash", "credit", gross)
                post(seller, "cash", "debit", gross)
                post(seller, "security_sale_proceeds" if joint_trade else "sales_income", "credit", gross)
                post(seller, "security_disposal_cost" if joint_trade else "cost_of_goods_sold", "debit", cost)
                post(seller, inventory, "credit", cost)
                move("transfer", quantity, seller, buyer, resource_id=SECURITY_ASSET if joint_trade else None)
            else:
                # Accepted payment and its refund have explicit, balanced legs.
                # No sale revenue is earned and no goods are assigned to the buyer.
                post(buyer, "purchase_advance", "debit", gross)
                post(buyer, "cash", "credit", gross)
                post(seller, "cash", "debit", gross)
                post(seller, "customer_advance", "credit", gross)
                post(seller, "customer_advance", "debit", gross)
                post(seller, "cash", "credit", gross)
                post(buyer, "cash", "debit", gross)
                post(buyer, "purchase_advance", "credit", gross)
                post(seller, "spoilage_expense", "debit", cost)
                post(seller, "inventory", "credit", cost)
                move("scrap", quantity, source=seller)
                details.update({"refunded_minor": gross, "quality_failure": True, "delivered_units": 0})
        elif kind == "consumption":
            actor, quantity = payload["actor_id"], payload["quantity_units"]
            self._require_actor(actor)
            if quantity > self._goods[actor]:
                raise EconomicReject("insufficient_goods")
            cost = self._balances[actor]["inventory"] * quantity // self._goods[actor]
            post(actor, "consumption_expense", "debit", cost)
            post(actor, "inventory", "credit", cost)
            move("consumption", quantity, source=actor)
            details = {"consumed_units": quantity, "carrying_cost_minor": cost}
        return EconomicJournalEntry(tx_id=tx_id, event_id=event_id, kind=kind, tick=tick,
            payload_hash=content_hash({"kind": kind, "payload": payload}), payload=payload,
            postings=postings, goods_movements=movements, details=details)

    def _apply(self, entry):
        balances, goods, wage_change, balance_checks = {}, {}, {}, {}
        for p in entry.postings:
            if p.currency_id != self.currency_id:
                raise ValueError("Cannot pool currency units")
            key = (p.entity_id, p.currency_id)
            signed = p.amount_minor if p.side == "debit" else -p.amount_minor
            balance_checks[key] = balance_checks.get(key, 0) + signed
            if p.entity_id not in balances:
                balances[p.entity_id] = dict(self._balances[p.entity_id])
            row = balances[p.entity_id]
            value = row.get(p.account_code, 0) + signed
            if not -MAX_INT <= value <= MAX_INT:
                raise ValueError("Account balance outside signed-64-bit range")
            row[p.account_code] = value
        if any(balance_checks.values()):
            raise ValueError("Each entity/currency transaction must balance")
        securities = {}
        for movement in entry.goods_movements:
            if movement.resource_id == self.good_id:
                changes, holdings = goods, self._goods
            elif self._security_opening is not None and movement.resource_id == SECURITY_ASSET:
                changes, holdings = securities, self._security_units
            else:
                raise ValueError("Unknown physical resource")
            if movement.from_owner is not None:
                previous = changes.get(movement.from_owner, holdings[movement.from_owner])
                changes[movement.from_owner] = checked(previous - movement.quantity_units)
            if movement.to_owner is not None:
                previous = changes.get(movement.to_owner, holdings[movement.to_owner])
                changes[movement.to_owner] = checked(previous + movement.quantity_units)
        if self._security_opening is not None:
            issued = sum(self._security_opening.units.values()) if entry.kind == "opening" else 0
            if sum(units - self._security_units[actor] for actor, units in securities.items()) != issued:
                raise ValueError("Security quantity conservation failure")
            for actor in securities.keys() | balances.keys():
                units = securities.get(actor, self._security_units[actor])
                if not units and balances.get(actor, self._balances[actor]).get("security_inventory", 0):
                    raise ValueError("Empty security position must have zero carrying cost")
        employer = worker = None
        wage_delta = 0
        if entry.kind in ("wage_accrual", "wage_payment"):
            employer, worker = entry.payload["employer_id"], entry.payload["worker_id"]
            if employer == worker or employer not in self._balances or worker not in self._balances:
                raise ValueError("Invalid wage claim parties")
            amount = checked(entry.details["outstanding_minor"])
            wage_change[employer, worker] = amount
            wage_delta = amount - self._wages.get((employer, worker), 0)
        bank_positions = (self._bank_positions.with_changes(entry.details["changes"])
                          if entry.kind in _BANK_EVENTS else self._bank_positions)
        changes_bank = self.banking is not None and (
            entry.kind in _BANK_EVENTS or any(
                p.account_code in banking_domain.SIGNED_CLAIM_CODES
                or (p.entity_id == banking_domain.ISSUER and p.account_code == "cash")
                for p in entry.postings))
        if entry.kind == "opening" or changes_bank:
            self._assert_projection(self._balances | balances, self._goods | goods,
                                    self._wages | wage_change, bank_positions)
        else:
            # Unchanged owners retain their validated balances. Check global
            # conservation by delta and check each changed owner before publishing.
            if sum(b["cash"] - self._balances[a]["cash"] for a, b in balances.items()):
                raise ValueError("Settlement token conservation failure")
            if self.fiscal is not None and sum(b.get(code, 0) - self._balances[a].get(code, 0)
                                               for a, b in balances.items()
                                               for code in ("tax_receivable", "tax_payable")):
                raise ValueError("Tax assessment claim mirror mismatch")
            affected = balances.keys() | goods.keys()
            if worker is not None:
                affected.update((employer, worker))
            for actor in affected:
                before = self._balances[actor]
                self._assert_owner(balances.get(actor, before), goods.get(actor, self._goods[actor]),
                                   before["wage_receivable"] + (wage_delta if actor == worker else 0),
                                   -before["wage_payable"] + (wage_delta if actor == employer else 0))
        if entry.kind == "payroll_reserve":
            reservation = PayrollCashReservation.model_validate(entry.payload)
            self._payroll_reservations[reservation.reservation_id] = reservation
        elif entry.kind == "payroll_release":
            del self._payroll_reservations[PayrollInstallment.model_validate(entry.payload).reservation_id]
        elif entry.kind == "wage_payment" and "reservation_id" in entry.payload:
            del self._payroll_reservations[entry.payload["reservation_id"]]
        self._balances.update(balances)
        self._goods.update(goods)
        self._security_units.update(securities)
        self._wages.update(wage_change)
        self._bank_positions = bank_positions

    def _assert_owner(self, balances, goods, receivable, payable):
        if not _BASE_ACCOUNT_CODES <= balances.keys() <= self._allowed_codes or sum(balances.values()) != 0:
            raise ValueError("Owner chart/trial balance mismatch")
        for code, value in balances.items():
            checked(value if code in self._debit_codes else -value)
        checked(goods)
        if not goods and balances["inventory"]:
            raise ValueError("Empty inventory must have zero carrying cost")
        if balances["purchase_advance"] or balances["customer_advance"]:
            raise ValueError("M0 accepted delivery resolves atomically")
        if balances["wage_receivable"] != receivable or -balances["wage_payable"] != payable:
            raise ValueError("Wage claim and general ledger mismatch")
        if not -MAX_INT <= self._net_worth(balances) <= MAX_INT:
            raise ValueError("Net worth outside signed-64-bit range")
        checked(sum(balances.get(k, 0) for k in self._expense_codes))

    def _assert_projection(self, balances, goods, wages, bank_positions=None):
        if self.banking is not None:
            banking_domain.verify_claims(self.banking, balances,
                self._bank_positions if bank_positions is None else bank_positions)
        if set(balances) != set(self.actor_ids) or set(goods) != set(self.actor_ids):
            raise ValueError("Owner set mismatch")
        if sum(b["cash"] for b in balances.values()) != self._cash_total:
            raise ValueError("Settlement token conservation failure")
        if self.fiscal is not None and sum(b.get("tax_receivable", 0) + b.get("tax_payable", 0)
                                           for b in balances.values()):
            raise ValueError("Tax assessment claim mirror mismatch")
        receivable, payable = {}, {}
        for (employer, worker), amount in wages.items():
            if employer not in balances or worker not in balances or employer == worker:
                raise ValueError("Invalid wage claim parties")
            checked(amount)
            receivable[worker] = receivable.get(worker, 0) + amount
            payable[employer] = payable.get(employer, 0) + amount
        for actor, row in balances.items():
            self._assert_owner(row, goods[actor], receivable.get(actor, 0), payable.get(actor, 0))

    def _post_event(self, kind, event_id, payload):
        payload = _PAYLOADS[kind].model_validate(payload).model_dump()
        digest = content_hash({"kind": kind, "payload": payload})
        previous = self._journal.get(event_id)
        if previous is not None:
            if previous.payload_hash != digest:
                raise ValueError("event_id payload conflict")
            return previous
        entry = self._plan(kind, event_id, payload, self.tick)
        self._apply(entry)
        self._journal[event_id] = entry
        return entry

    def execute_batch(self, commands, *, execution_seq_start=0, delivery_failures=None):
        """One receipt per command; unexpected exceptions roll back the whole batch.

        A feasible command can be selected for exogenous delivery-quality failure.
        Insolvency/stock rejection never becomes a trust/default observation.
        """
        commands = [EconomicCommand.model_validate(c) for c in commands]
        checked(execution_seq_start)
        require_unique([c.command_id for c in commands], "economic command")
        failures = set() if delivery_failures is None else set(delivery_failures)
        if not failures <= {c.command_id for c in commands if isinstance(c.intent, PurchaseIntent)}:
            raise ValueError("Delivery failures must identify purchase commands in this batch")
        if not commands:
            return []
        if self.tick < 1:
            raise ValueError("Begin an economic tick before execution")
        if len({c.batch_id for c in commands}) != 1:
            raise ValueError("Commands must share one frozen batch")
        for c in commands:
            if (c.tick, c.world_version, c.observation_version) != (self.tick, self.world_version, self.world_version):
                raise ValueError("Stale economic command")
            self._require_actor(c.actor_id)
        before = self._copy_live()
        try:
            if self._decision_cash_budget is None:
                self._decision_cash_budget = {a: b["cash"] for a, b in self._balances.items()}
            result = []
            last_sequence = None
            for offset, command in enumerate(commands, 1):
                sequence = execution_seq_start + offset
                previous = self._commands.get(command.command_id)
                if previous is not None:
                    if previous != command:
                        raise ValueError("command_id payload conflict")
                    receipt = self._receipts[command.command_id]
                    if receipt.execution_seq != sequence:
                        raise ValueError("Idempotent replay requires the original receipt sequence")
                    if (command.command_id in failures) != receipt.quality_failure and receipt.status != "rejected":
                        raise ValueError("command_id quality outcome conflict")
                else:
                    if last_sequence is None:
                        last_sequence = max((r.execution_seq for r in self._receipts.values()), default=0)
                    if sequence <= last_sequence:
                        raise ValueError("New economic receipts require increasing execution sequences")
                    receipt = self._execute_one(command, sequence,
                                                quality_failure=command.command_id in failures)
                    self._commands[command.command_id] = command
                    self._receipts[command.command_id] = receipt
                    last_sequence = receipt.execution_seq
                result.append(receipt)
            return result
        except Exception:
            self._restore_live(before)
            raise

    def _receipt(self, command, sequence, **fields):
        return EconomicExecutionReceipt(command_id=command.command_id, decision_id=command.decision_id,
            actor_id=command.actor_id, execution_seq=sequence, tick=command.tick,
            world_version=command.world_version, **fields)

    def _execute_one(self, command, sequence, *, quality_failure):
        intent, actor = command.intent, command.actor_id
        try:
            if isinstance(intent, HoldIntent):
                return self._receipt(command, sequence, status="accepted", outcome="hold", executed_quantity=0)
            if isinstance(intent, QuoteIntent):
                self._set_quote(intent.quote_id, actor, intent.unit_price_minor,
                                effective_tick=self.tick+1, ttl_ticks=intent.ttl_ticks)
                return self._receipt(command, sequence, status="accepted", outcome="quote_updated", executed_quantity=0)
            if isinstance(intent, PurchaseIntent):
                self._require_actor(intent.seller_id)
                if actor == intent.seller_id:
                    raise EconomicReject("self_purchase")
                quote = self._quotes.get(intent.quote_id)
                if quote is None or quote.seller_id != intent.seller_id:
                    raise EconomicReject("quote_identity_mismatch")
                if not quote.effective_tick <= self.tick <= quote.expires_after_tick:
                    raise EconomicReject("quote_not_effective")
                if quote.unit_price_minor > intent.max_unit_price_minor:
                    raise EconomicReject("price_limit_exceeded")
                cash = self._available_cash(actor)
                quantity = min(intent.quantity_units, self._goods[intent.seller_id], cash//quote.unit_price_minor)
                if quantity == 0 or quantity < intent.quantity_units and not intent.allow_partial:
                    reason = "insufficient_cash" if cash//quote.unit_price_minor < intent.quantity_units else "insufficient_goods"
                    raise EconomicReject(reason)
                kind = "delivery_failure" if quality_failure else "purchase"
                entry = self._post_event(kind, f"command:{command.command_id}", {
                    "buyer_id": actor, "seller_id": intent.seller_id, "quote_id": intent.quote_id,
                    "quantity_units": quantity, "unit_price_minor": quote.unit_price_minor,
                })
                amount = entry.details["gross_minor"]
                if quality_failure:
                    return self._receipt(command, sequence, status="accepted",
                        outcome="delivery_failed_refunded", executed_quantity=0,
                        counterparty_id=intent.seller_id, accepted_quantity=quantity, quality_failure=True,
                        refunded_minor=amount, ledger_tx_ids=[entry.tx_id],
                        reason_codes=["delivery_quality_failure", "payment_refunded", "goods_scrapped"])
                self._decision_cash_budget[actor] -= amount
                partial = quantity < intent.quantity_units
                return self._receipt(command, sequence, status="partially_executed" if partial else "accepted",
                    outcome="purchased", executed_quantity=quantity, accepted_quantity=quantity,
                    counterparty_id=intent.seller_id, cash_delta_minor=-amount, goods_delta_units=quantity,
                    ledger_tx_ids=[entry.tx_id], reason_codes=["partial_capacity"] if partial else [])
            if intent.kind in _BANK_REQUESTS:
                kind, outcome = _BANK_REQUESTS[intent.kind]
                entry = self._post_event(kind, f"command:{command.command_id}",
                    {"actor_id": actor, "intent": intent.model_dump()})
                return self._receipt(command, sequence, status="accepted", outcome=outcome,
                    unit="cash_minor", executed_quantity=0, accepted_quantity=intent.amount_minor,
                    counterparty_id=intent.bank_id, ledger_tx_ids=[entry.tx_id])
            if isinstance(intent, ConsumeIntent):
                entry = self._post_event("consumption", f"command:{command.command_id}", {
                    "actor_id": actor, "quantity_units": intent.quantity_units})
                return self._receipt(command, sequence, status="accepted", outcome="consumed",
                    executed_quantity=intent.quantity_units, accepted_quantity=intent.quantity_units,
                    goods_delta_units=-intent.quantity_units, ledger_tx_ids=[entry.tx_id])
            raise ValueError("Unsupported M0 action")
        except (EconomicReject, banking_domain.Refusal) as exc:
            return self._receipt(command, sequence, status="rejected", outcome="rejected", executed_quantity=0,
                counterparty_id=intent.seller_id if isinstance(intent, PurchaseIntent) else None,
                unit="cash_minor" if intent.kind in _BANK_REQUESTS else "goods",
                reason_codes=[str(exc)])

    def _copy_live(self):
        return ({a: dict(b) for a, b in self._balances.items()}, dict(self._goods), dict(self._wages),
                dict(self._journal), dict(self._quotes), dict(self._commands), dict(self._receipts),
                dict(self._payroll_reservations), self._bank_positions,
                None if self._decision_cash_budget is None else dict(self._decision_cash_budget),
                dict(self._security_units))

    def _restore_live(self, state):
        (self._balances, self._goods, self._wages, self._journal, self._quotes,
         self._commands, self._receipts, self._payroll_reservations, self._bank_positions, self._decision_cash_budget,
         self._security_units) = state
        self._checked_execution = None
        self._history_capture = {}

    def _rebuild(self, *, full=False):
        journal = self.journal
        cached = None if full else self._checked_journal
        prefix = cached.journal if cached is not None else ()
        if (prefix and len(prefix) <= len(journal) and prefix[-1].tick <= self.tick
                and cached._configuration_identity() == self._configuration_identity()
                and cached.fiscal == self.fiscal and cached.banking == self.banking
                and all(verified is current for verified, current in zip(prefix, journal))
                and all(self._quotes.get(k) is v for k, v in cached._quotes.items())):
            # References point to frozen research records. A restored book starts
            # with no cache, and any replaced prefix is checked from opening.
            candidate = copy(cached)
            candidate._restore_live(cached._copy_live())
            previous_tick = prefix[-1].tick
            start = len(prefix)
        else:
            candidate = EconomicRuntime(self._initial_cash, initial_goods=self._initial_goods,
                initial_inventory_cost=self._initial_cost, currency_id=self.currency_id, good_id=self.good_id,
                cash_scale=self.cash_scale, labor_cost_per_unit_minor=self.labor_cost_per_unit_minor,
                fiscal=self.fiscal, banking=self.banking, security_opening=self._security_opening)
            if not journal or content_hash(journal[0]) != content_hash(candidate.journal[0]):
                raise ValueError("Opening journal differs from configuration")
            candidate._journal[journal[0].event_id] = journal[0]
            previous_tick, start = 0, 1
        candidate._quotes = dict(self._quotes)
        for entry in journal[start:]:
            if not previous_tick <= entry.tick <= self.tick or entry.tick == 0:
                raise ValueError("Invalid economic journal order/time")
            if entry.tick != previous_tick:
                candidate._decision_cash_budget = None
            previous_tick = entry.tick
            if entry.event_id in candidate._journal:
                raise ValueError("Duplicate journal event")
            if entry.kind in _DECISION_EVENTS:
                if candidate._decision_cash_budget is None:
                    candidate._decision_cash_budget = {a: b["cash"] for a, b in candidate._balances.items()}
            elif candidate._decision_cash_budget is not None:
                raise ValueError("Boundary economic event appears after same-tick execution")
            if entry.kind in ("purchase", "delivery_failure"):
                amount = notional(entry.payload["unit_price_minor"], 1, entry.payload["quantity_units"])
                if amount > candidate._available_cash(entry.payload["buyer_id"]):
                    raise ValueError("Journal exceeds the frozen cash budget after payroll reservations")
            expected = candidate._plan(entry.kind, entry.event_id, entry.payload, entry.tick)
            if content_hash(entry) != content_hash(expected):
                raise ValueError("Journal diverges from canonical economic postings")
            candidate._apply(entry)
            candidate._journal[entry.event_id] = entry
            if entry.kind in ("purchase", "security_trade"):
                candidate._decision_cash_budget[entry.payload["buyer_id"]] -= entry.details["gross_minor"]
        self._checked_journal = candidate
        return candidate

    def validate_state(self, *, full_history=False):
        self._assert_projection(self._balances, self._goods, self._wages)
        if self.world_version != max(0, self.tick-1):
            raise ValueError("Participant clock/base version mismatch")
        rebuilt = self._rebuild(full=full_history)
        if (rebuilt._balances, rebuilt._goods, rebuilt._wages) != (self._balances, self._goods, self._wages):
            raise ValueError("Cached accounts/quantities differ from journal reconstruction")
        if rebuilt._security_units != self._security_units:
            raise ValueError("Security quantities differ from journal reconstruction")
        if rebuilt._bank_positions != self._bank_positions:
            raise ValueError("Bank claims or contract state differ from journal reconstruction")
        if rebuilt._payroll_reservations != self._payroll_reservations:
            raise ValueError("Active payroll reservations differ from journal reconstruction")
        if any(r.due_tick <= self.tick for r in self.payroll_reservations):
            raise ValueError("A payroll reservation was not settled on time")
        if any(self._available_cash(a) < 0 for a in self.actor_ids):
            raise ValueError("Payroll reservations exceed cash available at the observation barrier")
        for quote in self._quotes.values():
            if quote.seller_id not in self._balances or quote.good_id != self.good_id:
                raise ValueError("Invalid quote owner/resource")
        if len(self._commands) != len(self._receipts):
            raise ValueError("One command requires exactly one main receipt")
        commands = tuple(self._commands.items())
        journal = self.journal
        prefix = None if full_history else self._matching_execution_prefix(commands, journal)
        command_start = len(prefix.commands) if prefix else 0
        journal_start = len(prefix.journal) if prefix else 0
        checked_receipts, sequence, active, received = self._validate_execution_history(
            commands[command_start:], journal[journal_start:], prefix)
        receipts = (*prefix.receipts, *checked_receipts) if prefix else tuple(checked_receipts)
        self._checked_execution = _ExecutionPrefix(self.tick, commands, receipts, journal, dict(self._quotes),
                                                   sequence, active, received)

    def _matching_execution_prefix(self, commands, journal):
        prefix = self._checked_execution
        if (prefix is None or prefix.tick > self.tick or len(commands) < len(prefix.commands)
                or len(journal) < len(prefix.journal)):
            return None
        # Identity covers immutable records. A rewritten key or row takes the full path.
        if (any(key != old_key or command is not old_command or self._receipts.get(key) is not receipt
                for (key, command), (old_key, old_command), receipt in zip(commands, prefix.commands, prefix.receipts))
                or any(row is not old or self._journal.get(old.event_id) is not old
                       for row, old in zip(journal, prefix.journal))
                or any(self._quotes.get(key) is not old for key, old in prefix.quotes.items())):
            return None
        return prefix

    def _validate_execution_history(self, commands, journal, prefix):
        sequence = prefix.last_sequence if prefix else 0
        same_tick = prefix is not None and prefix.tick == self.tick
        current_commands = prefix.has_current_execution if same_tick else False
        received_this_tick = dict(prefix.received_this_tick) if same_tick else {a: 0 for a in self.actor_ids}
        command_events = set()
        receipts = []
        for command_id, command in commands:
            receipt = self._receipts.get(command_id)
            if receipt is None:
                raise ValueError("One command requires exactly one main receipt")
            if (receipt.command_id, receipt.decision_id, receipt.actor_id, receipt.tick, receipt.world_version) != (
                    command_id, command.decision_id, command.actor_id, command.tick, command.world_version):
                raise ValueError("Command/receipt identity mismatch")
            if (not 1 <= command.tick <= self.tick or command.world_version != command.tick-1
                    or command.observation_version != command.world_version or command.actor_id not in self._balances):
                raise ValueError("Invalid command observation barrier")
            if receipt.execution_seq <= sequence:
                raise ValueError("Economic execution sequences must increase in recorded order")
            sequence = receipt.execution_seq
            receipts.append(receipt)
            current_commands = current_commands or command.tick == self.tick
            event_id = f"command:{command_id}"
            event = self._journal.get(event_id)
            if event is not None:
                command_events.add(event_id)
            if receipt.ledger_tx_ids != ([] if event is None else [event.tx_id]):
                raise ValueError("Receipt journal reference mismatch")
            intent = command.intent
            if (receipt.unit == "cash_minor") != (intent.kind in _BANK_REQUESTS):
                raise ValueError("Bank request receipt requires a bank request command")
            if receipt.status == "rejected":
                if event is not None:
                    raise ValueError("Rejected action has a journal effect")
                continue
            if isinstance(intent, HoldIntent):
                if receipt.outcome != "hold" or event is not None:
                    raise ValueError("Hold receipt mismatch")
            elif isinstance(intent, QuoteIntent):
                expected = GoodsQuote(quote_id=intent.quote_id, seller_id=command.actor_id, good_id=self.good_id,
                    unit_price_minor=intent.unit_price_minor, effective_tick=command.tick+1,
                    expires_after_tick=command.tick+intent.ttl_ticks)
                if receipt.outcome != "quote_updated" or self._quotes.get(intent.quote_id) != expected or event is not None:
                    raise ValueError("Quote receipt mismatch")
            elif isinstance(intent, PurchaseIntent):
                if event is None or event.kind not in ("purchase", "delivery_failure") or event.tick != command.tick:
                    raise ValueError("Missing settled purchase event")
                payload = event.payload
                quantity, gross = payload["quantity_units"], event.details["gross_minor"]
                if ((payload["buyer_id"], payload["seller_id"], payload["quote_id"]) != (
                        command.actor_id, intent.seller_id, intent.quote_id)
                        or quantity > intent.quantity_units or quantity < intent.quantity_units and not intent.allow_partial
                        or payload["unit_price_minor"] > intent.max_unit_price_minor):
                    raise ValueError("Purchase intent/event mismatch")
                failure = event.kind == "delivery_failure"
                expected_status = "accepted" if failure or quantity == intent.quantity_units else "partially_executed"
                if (receipt.status != expected_status or receipt.counterparty_id != intent.seller_id
                        or receipt.accepted_quantity != quantity or receipt.quality_failure != failure
                        or receipt.executed_quantity != (0 if failure else quantity)
                        or receipt.cash_delta_minor != (0 if failure else -gross)
                        or receipt.goods_delta_units != (0 if failure else quantity)
                        or receipt.refunded_minor != (gross if failure else 0)
                        or receipt.outcome != ("delivery_failed_refunded" if failure else "purchased")):
                    raise ValueError("Purchase receipt amounts mismatch")
                if command.tick == self.tick and not failure:
                    received_this_tick[intent.seller_id] += gross
            elif intent.kind in _BANK_REQUESTS:
                kind, outcome = _BANK_REQUESTS[intent.kind]
                if (event is None or event.kind != kind or event.tick != command.tick
                        or event.payload != {"actor_id": command.actor_id, "intent": intent.model_dump()}
                        or receipt.outcome != outcome or receipt.counterparty_id != intent.bank_id
                        or receipt.accepted_quantity != intent.amount_minor):
                    raise ValueError("Bank receipt differs from its authorized request")
            elif isinstance(intent, ConsumeIntent):
                if (event is None or event.kind != "consumption" or event.tick != command.tick
                        or event.payload != {"actor_id": command.actor_id, "quantity_units": intent.quantity_units}
                        or receipt.outcome != "consumed" or receipt.executed_quantity != intent.quantity_units
                        or receipt.goods_delta_units != -intent.quantity_units or receipt.cash_delta_minor):
                    raise ValueError("Consumption receipt mismatch")
        expected_events = set()
        for entry in journal:
            if self._journal.get(entry.event_id) is not entry:
                raise ValueError("Journal event key mismatch")
            if entry.kind in _COMMAND_EVENTS:
                expected_events.add(entry.event_id)
            elif prefix is not None and entry.event_id.startswith("command:") and entry.event_id[8:] in self._commands:
                raise ValueError("Economic action journal event lacks its command receipt")
            if entry.kind == "security_trade" and entry.tick == self.tick:
                current_commands = True
                received_this_tick[entry.payload["seller_id"]] += entry.details["gross_minor"]
        if command_events != expected_events:
            raise ValueError("Economic action journal event lacks its command receipt")
        if (self._decision_cash_budget is not None) != current_commands:
            raise ValueError("Decision budget/barrier mismatch")
        if self._decision_cash_budget is not None:
            expected = {a: checked(b["cash"]-received_this_tick[a]) for a, b in self._balances.items()}
            if self._decision_cash_budget != expected:
                raise ValueError("Current-batch sales cannot finance frozen decisions")
        return receipts, sequence, current_commands, received_this_tick

    def export_state(self):
        return self._export_state([e.model_dump(mode="json") for e in self.journal],
            [c.model_dump(mode="json") for c in self._commands.values()],
            [r.model_dump(mode="json") for r in self._receipts.values()])

    def snapshot_state(self):
        history = {field: self._capture_history_rows(field, records) for field, records in self._history_tables()}
        return self._export_state(**history)

    def matches_committed_state(self, committed):
        """Prove that the frozen committed state can serve as a rollback input."""
        history = {}
        for field, records in self._history_tables():
            previous, prefix = self._retained_history(field, records)
            if prefix is None or len(previous) != len(records) or prefix is not committed.get(field):
                return False
            history[field] = prefix
        return self._export_state(**history) == committed

    def _history_tables(self):
        return (("journal", self._journal), ("commands", self._commands), ("receipts", self._receipts))

    def _retained_history(self, field, records):
        previous, prefix = self._history_capture.get(field, ((), None))
        if len(records) < len(previous) or any(
                key != old_key or record is not old_record
                for (old_key, old_record), (key, record) in zip(previous, records.items())):
            return (), None
        return previous, prefix

    def _capture_history_rows(self, field, records):
        previous, prefix = self._retained_history(field, records)
        if prefix is None or len(records) > len(previous):
            tail = tuple(islice(records.items(), len(previous), None))
            prefix = SealedJSONRowsV1(prefix, [record.model_dump(mode="json") for _, record in tail])
            self._history_capture[field] = (previous + tail, prefix)
        return prefix

    def _export_state(self, journal, commands, receipts):
        return {
            "schema_version": self.state_schema_version, "state_id": self.participant_id,
            "backend": _BACKENDS[self.state_schema_version],
            **({"institution_config_hash": self._institution_hash} if self._institution_hash else {}),
            **({"bank_positions": self._bank_positions.model_dump()} if self.banking is not None else {}),
            "currency_id": self.currency_id, "good_id": self.good_id, "cash_scale": self.cash_scale,
            "labor_cost_per_unit_minor": self.labor_cost_per_unit_minor,
            "initial_cash": dict(self._initial_cash), "initial_goods": dict(self._initial_goods),
            "initial_inventory_cost": dict(self._initial_cost), "tick": self.tick, "world_version": self.world_version,
            "balances": {a: dict(b) for a, b in self._balances.items()}, "goods": dict(self._goods),
            **({"security_units": dict(self._security_units)} if self._security_opening is not None else {}),
            "wage_obligations": [{"employer_id": employer, "worker_id": worker, "outstanding_minor": amount}
                                 for (employer, worker), amount in sorted(self._wages.items())],
            "journal": journal,
            "quotes": [q.model_dump(mode="json") for q in self._quotes.values()],
            "commands": commands,
            "receipts": receipts,
            "decision_cash_budget": None if self._decision_cash_budget is None else dict(self._decision_cash_budget),
            **({"payroll_reservations": [r.model_dump(mode="json") for r in self.payroll_reservations]}
               if self._payroll_reservations else {}),
        }

    def _configuration_identity(self):
        return content_hash({"currency_id": self.currency_id, "good_id": self.good_id,
            "cash_scale": self.cash_scale, "labor_cost_per_unit_minor": self.labor_cost_per_unit_minor,
            "initial_cash": self._initial_cash, "initial_goods": self._initial_goods,
            "initial_inventory_cost": self._initial_cost,
            **({"security": self._security_opening} if self._security_opening is not None else {})})

    def import_state(self, state):
        envelope = EconomicRuntimeState.model_validate(state)
        if (envelope.schema_version != self.state_schema_version or envelope.institution_config_hash != self._institution_hash
                or envelope.backend != _BACKENDS[self.state_schema_version]
                or (envelope.security_units is None) != (self._security_opening is None)):
            raise ValueError("Restore cannot change the economic protocol or institutional configuration")
        candidate = EconomicRuntime(envelope.initial_cash, initial_goods=envelope.initial_goods,
            initial_inventory_cost=envelope.initial_inventory_cost, currency_id=envelope.currency_id,
            good_id=envelope.good_id, cash_scale=envelope.cash_scale,
            labor_cost_per_unit_minor=envelope.labor_cost_per_unit_minor, fiscal=self.fiscal, banking=self.banking,
            security_opening=self._security_opening)
        if candidate._configuration_identity() != self._configuration_identity():
            raise ValueError("Restore cannot replace the configured economic identity/endowments")
        require_unique([e.event_id for e in envelope.journal], "journal event")
        require_unique([e.tx_id for e in envelope.journal], "journal transaction")
        require_unique([q.quote_id for q in envelope.quotes], "quote")
        require_unique([c.command_id for c in envelope.commands], "command")
        require_unique([r.command_id for r in envelope.receipts], "receipt")
        require_unique([(w.employer_id, w.worker_id) for w in envelope.wage_obligations], "wage obligation")
        require_unique([r.reservation_id for r in envelope.payroll_reservations], "payroll reservation")
        candidate.tick, candidate.world_version = envelope.tick, envelope.world_version
        candidate._balances = {a: dict(b) for a, b in envelope.balances.items()}
        candidate._goods = dict(envelope.goods)
        candidate._security_units = dict(envelope.security_units or {})
        candidate._wages = {(w.employer_id, w.worker_id): w.outstanding_minor for w in envelope.wage_obligations}
        candidate._journal = {e.event_id: e for e in envelope.journal}
        if (envelope.bank_positions is None) != (self.banking is None):
            raise ValueError("Bank claims are required exactly in banking runs")
        if envelope.bank_positions is not None:
            candidate._bank_positions = banking_domain.BankPositions.model_validate(envelope.bank_positions)
        candidate._quotes = {q.quote_id: q for q in envelope.quotes}
        candidate._commands = {c.command_id: c for c in envelope.commands}
        candidate._receipts = {r.command_id: r for r in envelope.receipts}
        candidate._payroll_reservations = {r.reservation_id: r for r in envelope.payroll_reservations}
        candidate._decision_cash_budget = (None if envelope.decision_cash_budget is None
                                           else dict(envelope.decision_cash_budget))
        candidate.validate_state(full_history=True)
        self.__dict__.update(candidate.__dict__)
