"""Single-currency banking posting plans within EconomicRuntime's journal."""
from types import SimpleNamespace
from typing import Annotated, Literal

from pydantic import Field, model_validator

from civitas.contracts.base import Id, Positive, Record, require_unique
from civitas.contracts.economic import Amount, CreditApplicationIntent, PositiveAmount, WithdrawDepositIntent
from civitas.contracts.observations import ObservationFact
from civitas.population.allocation import allocate
from .fiscal import TREASURY, pay_disclosure

ISSUER = 'public:settlement'
GUARANTOR = 'public:guarantee'

DEBIT_CODES = {'bank_reserve', 'deposit_asset', 'loan_asset', 'interest_asset',
               'interest_expense', 'credit_loss', 'payment_expense', 'deposit_loss', 'guarantee_expense'}
CREDIT_CODES = {'reserve_payable', 'deposit_payable', 'loan_payable', 'interest_payable',
                'interest_income', 'loan_allowance', 'credit_recovery', 'payment_income',
                'debt_release_income'}

ASSET_CODES = ('bank_reserve', 'deposit_asset', 'loan_asset', 'interest_asset')
SIGNED_CLAIM_CODES = (*ASSET_CODES, 'loan_allowance', 'deposit_payable', 'loan_payable',
                      'interest_payable', 'reserve_payable')
EXPENSES = ('interest_expense', 'credit_loss', 'payment_expense', 'deposit_loss', 'guarantee_expense')
Bps = Annotated[int, Field(ge=0, le=10000)]
BankMode = Literal['prefunded_intermediation', 'bank_credit_creation']


class BankOperation(Record):
    tick: Positive
    operation_id: Id
    kind: Literal['deposit', 'payment', 'approve', 'withdraw', 'recover', 'release', 'resolve']
    bank: Id | None = None
    actor: Id | None = None
    target_bank: Id | None = None
    target_actor: Id | None = None
    amount_minor: PositiveAmount | None = None
    installments: Annotated[int, Field(ge=1, le=10000)] | None = None
    period_rate_bps: Bps = 0
    payment_interval: Positive = 1
    default_after_misses: Positive = 2
    loss_bps: Bps = 10000
    expires_tick: Positive | None = None
    loan_id: Id | None = None

    @model_validator(mode='after')
    def fields_for_operation(self):
        required = {
            'deposit': {'bank', 'actor', 'amount_minor'},
            'payment': {'bank', 'actor', 'target_bank', 'target_actor', 'amount_minor'},
            'approve': {'bank', 'actor', 'amount_minor', 'installments'},
            'withdraw': {'bank', 'actor', 'amount_minor', 'expires_tick'},
            'recover': {'loan_id', 'amount_minor'},
            'release': {'loan_id'},
            'resolve': {'bank'},
        }[self.kind]
        nullable = {'bank', 'actor', 'target_bank', 'target_actor', 'amount_minor', 'installments',
                    'expires_tick', 'loan_id'}
        if {key for key in nullable if getattr(self, key) is not None} != required:
            raise ValueError('Bank operation requires the fields associated with its kind')
        if self.kind != 'approve' and (self.period_rate_bps, self.payment_interval, self.default_after_misses,
                                      self.loss_bps) != (0, 1, 2, 10000):
            raise ValueError('Loan terms apply to approval only')
        if self.kind == 'withdraw' and self.expires_tick < self.tick:
            raise ValueError('Withdrawal expires before its request')
        return self


class DisclosureFunding(Record):
    cost_per_recipient_minor: PositiveAmount
    budget_cap_minor: Amount
    authorized_through_tick: Positive


class RunNotice(Record):
    tick: Positive
    source_request: Id
    withdrawal_by_recipient: dict[Id, PositiveAmount] = Field(min_length=1)
    react_to_verified_delay: bool
    response_mode: Literal['scripted', 'actor'] = Field(default='scripted', exclude_if=lambda v: v == 'scripted')
    disclose: bool = Field(default=True, exclude_if=lambda v: v)
    disclosure_funding: DisclosureFunding | None = Field(default=None, exclude_if=lambda v: v is None)


class TreasurySupport(Record):
    bank_id: Id
    amount_minor: PositiveAmount
    disbursement_tick: Positive
    maturity_tick: Positive
    guarantee_limit_minor: Amount = 0

    @model_validator(mode='after')
    def terms(self):
        if self.maturity_tick <= self.disbursement_tick or self.guarantee_limit_minor > self.amount_minor:
            raise ValueError('Treasury support requires later maturity and a guarantee within principal')
        return self


class BankingConfig(Record):
    credit_creation_mode: BankMode
    initial_bank_cash: dict[Id, Amount] = Field(min_length=1, max_length=32)
    operations: list[BankOperation] = Field(default_factory=list)
    credit_offer: BankOperation | None = Field(default=None, exclude_if=lambda v: v is None)
    loan_to_capital_multiple: Positive = 20
    guarantee_budget_minor: Amount = 0
    guarantee_interbank: bool = False
    run_notice: RunNotice | None = None
    treasury_support: TreasurySupport | None = Field(default=None, exclude_if=lambda v: v is None)

    @property
    def requires_treasury(self):
        return self.treasury_support is not None or (
            self.run_notice is not None and self.run_notice.disclosure_funding is not None)

    @model_validator(mode='after')
    def inputs(self):
        if not all(bank.startswith('bank:') for bank in self.initial_bank_cash):
            raise ValueError('Bank IDs must start with bank:')
        instructions = list(self.operations)
        if self.credit_offer is not None:
            offer = self.credit_offer
            if offer.kind != 'approve' or not offer.actor.startswith('household:'):
                raise ValueError('A credit offer requires approval terms and one named household')
            instructions.append(offer)
        require_unique([op.operation_id for op in instructions], 'bank operation')
        for op in instructions:
            if op.operation_id.startswith('notice:'):
                raise ValueError('Bank instruction uses a reserved notification ID')
            if any(bank is not None and bank not in self.initial_bank_cash for bank in (op.bank, op.target_bank)):
                raise ValueError('Unregistered bank operation')
        if self.run_notice is not None:
            source = next((o for o in self.operations if o.operation_id == self.run_notice.source_request), None)
            if source is None or source.kind != 'withdraw' or source.tick >= self.run_notice.tick:
                raise ValueError('Run notice needs a prior authorized withdrawal')
            if source.actor in self.run_notice.withdrawal_by_recipient:
                raise ValueError('A withdrawal notice goes to other depositors')
            if self.run_notice.response_mode == 'actor' and not all(
                    a.startswith('household:') for a in self.run_notice.withdrawal_by_recipient):
                raise ValueError('Actor notices require household decision seats')
            if self.run_notice.response_mode == 'scripted' and not self.run_notice.disclose:
                raise ValueError('Withheld disclosure requires actor response mode')
            if self.run_notice.disclosure_funding is not None and self.run_notice.response_mode != 'actor':
                raise ValueError('Funded disclosure requires actor response mode')
        if self.guarantee_interbank and not self.guarantee_budget_minor:
            raise ValueError('Interbank guarantee requires funded cash')
        support = self.treasury_support
        if support is not None and (support.bank_id not in self.initial_bank_cash
                                    or support.guarantee_limit_minor > self.guarantee_budget_minor):
            raise ValueError('Treasury support requires a registered bank and a funded guarantee limit')
        return self


class Loan(Record):
    terms: BankOperation
    status: Literal['committed', 'refused', 'active', 'delinquent', 'defaulted', 'paid', 'released'] = 'committed'
    next_tick: Positive
    principal_minor: Amount = 0
    interest_minor: Amount = 0
    allowance_minor: Amount = 0
    misses: Amount = 0


class Withdrawal(Record):
    terms: BankOperation
    remaining_minor: Amount
    paid_minor: Amount = 0
    status: Literal['queued', 'paid', 'expired', 'resolution'] = 'queued'


class SupportAdvance(Record):
    status: Literal['active', 'defaulted', 'paid']
    outstanding_minor: Amount
    bank_repaid_minor: Amount = 0
    guarantee_paid_minor: Amount = 0

    @property
    def allowance_minor(self):
        return self.outstanding_minor if self.status == 'defaulted' else 0


class BankPositions(Record):
    deposits: dict[Id, dict[Id, PositiveAmount]] = Field(default_factory=dict)
    loans: dict[Id, Loan] = Field(default_factory=dict)
    withdrawals: dict[Id, Withdrawal] = Field(default_factory=dict)
    resolution: dict[Id, Literal['receivership', 'liquidated']] = Field(default_factory=dict)
    support_advances: dict[Id, SupportAdvance] = Field(default_factory=dict, exclude_if=lambda v: not v)

    def with_changes(self, changes):
        current = self.model_dump()
        return BankPositions.model_validate({key: {**current.get(key, {}), **changes.get(key, {})}
                                            for key in current.keys() | changes.keys()})


class BankEvent(Record):
    action: Literal['capital', 'scheduled', 'fund', 'due', 'queue', 'notice', 'reaction',
                    'support_disburse', 'support_collect']
    reference: Id


class BankWithdrawalEvent(Record):
    actor_id: Id
    intent: WithdrawDepositIntent


class CreditApplicationEvent(Record):
    actor_id: Id
    intent: CreditApplicationIntent


class Refusal(ValueError):
    pass


def exclusive_limit(config):
    """Maximum covered interbank scheduled debt service after one missed interval.

    A guarantee applies during each due attempt. Once cash is unavailable, uncovered
    losses remain with the lender and the run records the failed manipulation.
    """
    return sum(op.amount_minor + op.installments * (op.amount_minor * op.period_rate_bps // 10000)
               for op in config.operations if op.kind == 'approve' and op.actor in config.initial_bank_cash)


def actors_for(config, existing):
    expected = set(existing) | set(config.initial_bank_cash)
    if config.credit_offer is not None and config.credit_offer.actor not in expected:
        raise ValueError('Unknown credit offer recipient')
    for op in config.operations:
        if any(actor is not None and actor not in expected for actor in (op.actor, op.target_actor)):
            raise ValueError('Unknown banking customer')
        if op.kind in ('deposit', 'withdraw') and op.actor in config.initial_bank_cash:
            raise ValueError('Reserve conversion and customer withdrawal use distinct account types')
    if config.run_notice is not None and not set(config.run_notice.withdrawal_by_recipient) <= (
            expected - set(config.initial_bank_cash)):
        raise ValueError('Run notice requires registered non-bank recipients')


def gross_and_equity(balances):
    assets = balances['cash'] + sum(balances.get(code, 0) for code in ASSET_CODES) + balances.get('loan_allowance', 0)
    liabilities = -sum(balances.get(code, 0) for code in ('deposit_payable', 'loan_payable', 'interest_payable'))
    return assets, liabilities, assets - liabilities


def support_collection_due(terms, advance, tick, reserves):
    return advance is not None and (
        (advance.status == 'active' and tick == terms.maturity_tick)
        or (advance.status == 'defaulted' and tick > terms.maturity_tick and reserves > 0))


class BankPlan:
    def __init__(self, book, tick):
        self.book = book
        self.tick = tick
        self.config = book.banking
        current = book._bank_positions
        self.positions = SimpleNamespace(deposits={b: dict(rows) for b, rows in current.deposits.items()},
            loans=dict(current.loans), withdrawals=dict(current.withdrawals), resolution=dict(current.resolution),
            support_advances=dict(current.support_advances))
        self.postings = []
        self.details = {'status': 'settled'}

    def post(self, actor, code, debit, amount):
        if amount:
            self.postings.append((actor, code, 'debit' if debit else 'credit', amount))

    def balance(self, actor, code):
        return self.book._balances[actor].get(code, 0)

    def deposit_delta(self, bank, actor, delta):
        if not delta:
            return
        claims = self.positions.deposits.setdefault(bank, {})
        value = claims.get(actor, 0) + delta
        if value < 0:
            raise Refusal('insufficient_deposit')
        if value:
            claims[actor] = value
        else:
            claims.pop(actor, None)
        self.post(actor, 'deposit_asset', delta > 0, abs(delta))
        self.post(bank, 'deposit_payable', delta < 0, abs(delta))

    def spendable_deposit(self, bank, actor):
        held = sum(w.remaining_minor for w in self.positions.withdrawals.values() if w.status == 'queued'
                   and (w.terms.bank, w.terms.actor) == (bank, actor))
        return self.positions.deposits.get(bank, {}).get(actor, 0) - held

    def convert_cash(self, bank, actor, amount, *, into_reserve):
        # Cash at the issuer backs its outstanding reserve liability exactly.
        self.post(actor, 'cash', not into_reserve, amount)
        self.post(ISSUER, 'cash', into_reserve, amount)
        self.post(ISSUER, 'reserve_payable', not into_reserve, amount)
        self.post(bank, 'bank_reserve', into_reserve, amount)

    def move_reserve(self, payer, recipient, amount):
        if payer != recipient:
            self.post(payer, 'bank_reserve', False, amount)
            self.post(recipient, 'bank_reserve', True, amount)
            self.post(ISSUER, 'reserve_payable', True, amount)
            self.post(ISSUER, 'reserve_payable', False, amount)

    def open_bank(self, bank):
        if bank in self.positions.resolution:
            raise Refusal('bank_under_resolution')

    def capital(self, bank):
        if self.tick != 1:
            raise ValueError('Reserve opening must occur in period one')
        amount = self.balance(bank, 'cash')
        self.convert_cash(bank, bank, amount, into_reserve=True)
        self.details.update(bank=bank, amount_minor=amount)

    def scheduled(self, op):
        bank, actor, amount = op.bank, op.actor, op.amount_minor
        if op.kind in ('deposit', 'payment', 'approve', 'withdraw'):
            self.open_bank(bank)
        self.details.update(kind=op.kind, operation_id=op.operation_id, bank=bank)
        if op.kind == 'deposit':
            if self.book._available_cash(actor) < amount:
                raise Refusal('insufficient_cash')
            self.convert_cash(bank, actor, amount, into_reserve=True)
            self.deposit_delta(bank, actor, amount)
        elif op.kind == 'payment':
            self.open_bank(op.target_bank)
            if (actor, bank) == (op.target_actor, op.target_bank):
                raise Refusal('same_deposit_claim')
            if self.spendable_deposit(bank, actor) < amount:
                raise Refusal('insufficient_unreserved_deposit')
            if bank != op.target_bank and self.balance(bank, 'bank_reserve') < amount:
                raise Refusal('insufficient_reserves')
            self.deposit_delta(bank, actor, -amount)
            self.deposit_delta(op.target_bank, op.target_actor, amount)
            self.post(actor, 'payment_expense', True, amount)
            self.post(op.target_actor, 'payment_income', False, amount)
            self.move_reserve(bank, op.target_bank, amount)
            self.details.update(payer_bank=bank, payee_bank=op.target_bank, cleared_minor=amount)
        elif op.kind == 'approve':
            if actor == bank:
                raise Refusal('self_lending')
            if actor in self.config.initial_bank_cash:
                self.open_bank(actor)
            self.positions.loans[op.operation_id] = Loan(terms=op, next_tick=self.tick + 1)
            self.details['status'] = 'committed'
        elif op.kind == 'withdraw':
            self.request_withdrawal(op)
        elif op.kind == 'recover':
            loan = self.positions.loans.get(op.loan_id)
            if loan is None or loan.status != 'defaulted':
                raise Refusal('no_defaulted_loan')
            paid = min(amount, loan.interest_minor + loan.principal_minor)
            if self.payment_capacity(loan) < paid:
                raise Refusal('unfunded_recovery')
            self.repay(loan, paid, sponsor=0)
            self.details['recovered_minor'] = paid
        elif op.kind == 'release':
            loan = self.positions.loans.get(op.loan_id)
            if loan is None or loan.status != 'defaulted':
                raise Refusal('no_defaulted_loan')
            op_loan = loan.terms
            total = loan.principal_minor + loan.interest_minor
            self.post(op_loan.bank, 'loan_allowance', True, loan.allowance_minor)
            self.post(op_loan.bank, 'credit_loss', True, total - loan.allowance_minor)
            self.reduce_claims(loan, loan.principal_minor, loan.interest_minor)
            self.post(op_loan.actor, 'debt_release_income', False, total)
            self.save_loan(loan, status='released', principal_minor=0, interest_minor=0, allowance_minor=0)
            self.details['released_minor'] = total
        else:
            self.resolve(bank)

    def request_withdrawal(self, op):
        if self.spendable_deposit(op.bank, op.actor) < op.amount_minor:
            raise Refusal('insufficient_unreserved_deposit')
        self.positions.withdrawals[op.operation_id] = Withdrawal(terms=op, remaining_minor=op.amount_minor)
        self.details['status'] = 'queued'

    def notice(self):
        spec = self.config.run_notice
        intended = sorted(spec.withdrawal_by_recipient)
        funding = spec.disclosure_funding
        if funding is not None:
            self.details.update(intended=intended, reached=[], disclosure_by_recipient_minor={},
                                disclosure_paid_minor=0, budget_cap_minor=funding.budget_cap_minor)
            if self.tick > funding.authorized_through_tick:
                self.details['status'] = 'authorization_expired'
                return
        request = self.positions.withdrawals.get(spec.source_request)
        if request is not None and spec.response_mode == 'actor':
            # Vary delivery time while holding the verified historical fact fixed.
            root = self.book._journal.get(f'bank:{request.terms.tick}:queue:{spec.source_request}')
        else:
            root = next((row for row in reversed(self.book.journal) if row.kind == 'bank'
                         and row.tick < self.tick
                         and row.payload == {'action': 'queue', 'reference': spec.source_request}), None)
        if root is None or request is None:
            self.details.update(status='not_emitted_no_verified_root', reached=[])
            return
        reached = intended
        if funding is not None:
            cash = self.book.account_view(TREASURY).available_cash_minor
            reached, paid = pay_disclosure(intended, min(cash, funding.budget_cap_minor),
                                           funding.cost_per_recipient_minor,
                                           lambda *posting: self.postings.append(posting))
            self.details.update(disclosure_paid_minor=paid, treasury_cash_before_minor=cash,
                                disclosure_by_recipient_minor=dict.fromkeys(reached, funding.cost_per_recipient_minor))
        delayed = root.details['status'] == 'queued' and root.details['outstanding_minor'] > 0
        self.details.update(status='disclosed' if reached else 'unfunded', bank=request.terms.bank, source_tx_id=root.tx_id,
            reached=reached,
            content=('verified_withdrawal_delay' if delayed and spec.react_to_verified_delay else
                     'withdrawal_window_notice'))

    def reaction(self, actor):
        spec = self.config.run_notice
        notice = next((row for row in self.book.journal if row.event_id == f'bank:{spec.tick}:notice:{spec.source_request}'),
                      None)
        if not notice or actor not in notice.details.get('reached', ()):
            self.details['status'] = 'unexposed'
            return
        self.details['notice_tx_id'] = notice.tx_id
        if notice.details['content'] != 'verified_withdrawal_delay':
            self.details['status'] = 'no_signal_response'
            return
        bank = notice.details['bank']
        self.open_bank(bank)
        amount = min(spec.withdrawal_by_recipient[actor], self.spendable_deposit(bank, actor))
        if amount:
            op = BankOperation(tick=self.tick, operation_id=f'notice:{actor}', kind='withdraw',
                               bank=bank, actor=actor, amount_minor=amount, expires_tick=self.tick + 1)
            self.request_withdrawal(op)
        else:
            self.details['status'] = 'no_spendable_deposit'
        self.details.update(requested_minor=amount, bank=bank)

    def save_loan(self, loan, **changes):
        self.positions.loans[loan.terms.operation_id] = loan.model_copy(update=changes)

    def fund(self, loan):
        op = loan.terms
        bank, actor, amount = op.bank, op.actor, op.amount_minor
        positions = self.book._balances[bank]
        _, _, equity = gross_and_equity(positions)
        risk = positions.get('loan_asset', 0) + amount
        if (bank in self.positions.resolution or actor in self.positions.resolution or equity <= 0
                or risk > equity * self.config.loan_to_capital_multiple
                or (self.config.credit_creation_mode == 'prefunded_intermediation'
                    and positions.get('bank_reserve', 0) + positions.get('deposit_payable', 0) < amount)):
            self.save_loan(loan, status='refused')
            self.details.update(status='rejected', reason='funding_or_capital_recheck')
            return
        self.post(bank, 'loan_asset', True, amount)
        self.post(actor, 'loan_payable', False, amount)
        if self.config.credit_creation_mode == 'bank_credit_creation':
            self.deposit_delta(bank, actor, amount)
        elif actor in self.config.initial_bank_cash:
            self.move_reserve(bank, actor, amount)
        else:
            self.convert_cash(bank, actor, amount, into_reserve=False)
        self.save_loan(loan, status='active', principal_minor=amount, next_tick=self.tick + op.payment_interval)
        self.details['disbursed_minor'] = amount

    def payment_capacity(self, loan):
        op = loan.terms
        liquid = (self.balance(op.actor, 'bank_reserve') if op.actor in self.config.initial_bank_cash
                  else self.book._available_cash(op.actor))
        return self.spendable_deposit(op.bank, op.actor) + liquid

    def reduce_claims(self, loan, principal, interest):
        bank, actor = loan.terms.bank, loan.terms.actor
        for amount, asset, liability in ((principal, 'loan_asset', 'loan_payable'),
                                         (interest, 'interest_asset', 'interest_payable')):
            self.post(bank, asset, False, amount)
            self.post(actor, liability, True, amount)

    def repay(self, loan, amount, *, sponsor):
        op = loan.terms
        bank, actor = op.bank, op.actor
        own = amount - sponsor
        deposit = min(own, self.spendable_deposit(bank, actor))
        self.deposit_delta(bank, actor, -deposit)
        if actor in self.config.initial_bank_cash:
            self.move_reserve(actor, bank, own - deposit)
        else:
            self.convert_cash(bank, actor, own - deposit, into_reserve=True)
        if sponsor:
            self.convert_cash(bank, GUARANTOR, sponsor, into_reserve=True)
            self.post(GUARANTOR, 'guarantee_expense', True, sponsor)
            self.post(actor, 'debt_release_income', False, sponsor)
        interest = min(loan.interest_minor, amount)
        principal = amount - interest
        self.reduce_claims(loan, principal, interest)
        allowance_reversed = min(loan.allowance_minor, amount)
        self.post(bank, 'loan_allowance', True, allowance_reversed)
        self.post(bank, 'credit_recovery', False, allowance_reversed)
        balance = loan.principal_minor + loan.interest_minor - amount
        status = ('paid' if not balance else 'defaulted' if loan.status == 'defaulted' else 'active')
        self.save_loan(loan, status=status, next_tick=self.tick + op.payment_interval, misses=0,
            principal_minor=loan.principal_minor - principal, interest_minor=loan.interest_minor - interest,
            allowance_minor=loan.allowance_minor - allowance_reversed)
        self.details.update(principal_paid_minor=principal, interest_paid_minor=interest, guarantee_paid_minor=sponsor)

    def due(self, loan):
        op = loan.terms
        interest = loan.principal_minor * op.period_rate_bps // 10000
        self.post(op.bank, 'interest_asset', True, interest)
        self.post(op.bank, 'interest_income', False, interest)
        self.post(op.actor, 'interest_expense', True, interest)
        self.post(op.actor, 'interest_payable', False, interest)
        loan = loan.model_copy(update={'interest_minor': loan.interest_minor + interest})
        # Equal principal instalments, capped at the actual remaining principal.
        installment = (op.amount_minor + op.installments - 1) // op.installments
        due = min(loan.principal_minor, installment) + loan.interest_minor
        available = self.payment_capacity(loan)
        gap = max(0, due - available)
        interbank = op.actor in self.config.initial_bank_cash
        sponsor = (min(gap, self.book._available_cash(GUARANTOR))
                   if self.config.guarantee_interbank and interbank else 0)
        self.details.update(bank=op.bank, borrower=op.actor, interest_accrued_minor=interest,
                            due_minor=due, interbank=interbank, uncovered_due_minor=gap - sponsor)
        if available + sponsor >= due:
            self.repay(loan, due, sponsor=sponsor)
            return
        misses = loan.misses + 1
        default = misses >= op.default_after_misses
        allowance = ((loan.principal_minor + loan.interest_minor) * op.loss_bps // 10000) if default else 0
        self.post(op.bank, 'credit_loss', True, allowance)
        self.post(op.bank, 'loan_allowance', False, allowance)
        self.save_loan(loan, status='defaulted' if default else 'delinquent', misses=misses,
                       next_tick=self.tick + op.payment_interval, allowance_minor=allowance)
        self.details.update(status='defaulted' if default else 'delinquent', impairment_minor=allowance,
                            guarantee_paid_minor=0, uncovered_due_minor=gap)

    def disburse_support(self, terms):
        bank, amount = terms.bank_id, terms.amount_minor
        if self.tick != terms.disbursement_tick or bank in self.positions.support_advances:
            raise ValueError('Treasury support disbursement is outside the frozen authorization')
        self.open_bank(bank)
        if gross_and_equity(self.book._balances[bank])[2] <= 0:
            raise Refusal('bank_ineligible_equity')
        if self.book._available_cash(TREASURY) < amount:
            raise Refusal('insufficient_treasury_cash')
        self.convert_cash(bank, TREASURY, amount, into_reserve=True)
        self.post(TREASURY, 'loan_asset', True, amount)
        self.post(bank, 'loan_payable', False, amount)
        self.positions.support_advances[bank] = SupportAdvance(status='active', outstanding_minor=amount)
        self.details.update(disbursed_minor=amount, maturity_tick=terms.maturity_tick)

    def collect_support(self, terms):
        bank = terms.bank_id
        advance = self.positions.support_advances.get(bank)
        reserves = self.balance(bank, 'bank_reserve')
        if not support_collection_due(terms, advance, self.tick, reserves):
            raise ValueError('No Treasury support collection is due')
        repaid = min(advance.outstanding_minor, reserves)
        guaranteed = (min(advance.outstanding_minor - repaid, terms.guarantee_limit_minor,
                          self.book._available_cash(GUARANTOR)) if self.tick == terms.maturity_tick else 0)
        received = repaid + guaranteed
        remaining = advance.outstanding_minor - received
        self.convert_cash(bank, TREASURY, repaid, into_reserve=False)
        self.post(GUARANTOR, 'cash', False, guaranteed)
        self.post(GUARANTOR, 'guarantee_expense', True, guaranteed)
        self.post(TREASURY, 'cash', True, guaranteed)
        self.post(bank, 'debt_release_income', False, guaranteed)
        self.post(TREASURY, 'loan_asset', False, received)
        self.post(bank, 'loan_payable', True, received)
        impairment = max(0, remaining - advance.allowance_minor)
        reversal = max(0, advance.allowance_minor - remaining)
        self.post(TREASURY, 'credit_loss', True, impairment)
        self.post(TREASURY, 'loan_allowance', False, impairment)
        self.post(TREASURY, 'loan_allowance', True, reversal)
        self.post(TREASURY, 'credit_recovery', False, reversal)
        status = 'defaulted' if remaining else 'paid'
        self.positions.support_advances[bank] = SupportAdvance(status=status, outstanding_minor=remaining,
            bank_repaid_minor=advance.bank_repaid_minor + repaid,
            guarantee_paid_minor=advance.guarantee_paid_minor + guaranteed)
        self.details.update(status=status, bank_repaid_minor=repaid, guarantee_paid_minor=guaranteed,
                            outstanding_minor=remaining, impairment_minor=impairment, allowance_reversed_minor=reversal)

    def serve(self, request):
        op = request.terms
        if self.tick > op.expires_tick or op.bank in self.positions.resolution:
            status = 'expired' if self.tick > op.expires_tick else 'resolution'
            self.positions.withdrawals[op.operation_id] = request.model_copy(update={'status': status})
            self.details.update(status=status, outstanding_minor=request.remaining_minor)
            return
        paid = min(request.remaining_minor, self.balance(op.bank, 'bank_reserve'))
        self.deposit_delta(op.bank, op.actor, -paid)
        self.convert_cash(op.bank, op.actor, paid, into_reserve=False)
        remaining = request.remaining_minor - paid
        self.positions.withdrawals[op.operation_id] = request.model_copy(update={
            'status': 'queued' if remaining else 'paid', 'remaining_minor': remaining,
            'paid_minor': request.paid_minor + paid})
        self.details.update(status='queued' if remaining else 'paid', paid_minor=paid, outstanding_minor=remaining)

    def resolve(self, bank):
        if self.positions.resolution.get(bank) == 'liquidated':
            raise Refusal('bank_already_liquidated')
        b = self.book._balances[bank]
        assets, _, equity = gross_and_equity(b)
        if equity >= 0 and bank not in self.positions.resolution:
            raise Refusal('bank_has_nonnegative_equity')
        reserves = b.get('bank_reserve', 0)
        claims = dict(self.positions.deposits.get(bank, {}))
        # Final pari passu settlement requires all non-reserve assets and senior
        # interbank debts to have cleared. In receivership nominal claims survive.
        final = (assets == reserves and not any(b.get(code, 0) for code in (
            'loan_asset', 'interest_asset', 'deposit_asset', 'loan_payable', 'interest_payable')))
        self.positions.resolution[bank] = 'liquidated' if final else 'receivership'
        payouts, losses = {}, {}
        if final and claims:
            outlay = min(sum(claims.values()), reserves)
            payouts = {a.account_id: a.allocated_units for a in allocate(outlay, claims, 'resolution')}
            for actor, claim in sorted(claims.items()):
                paid = payouts[actor]
                losses[actor] = claim - paid
                self.deposit_delta(bank, actor, -claim)
                if actor in self.config.initial_bank_cash:
                    self.move_reserve(bank, actor, paid)
                else:
                    self.convert_cash(bank, actor, paid, into_reserve=False)
                self.post(actor, 'deposit_loss', True, losses[actor])
                self.post(bank, 'debt_release_income', False, losses[actor])
        self.details.update(status=self.positions.resolution[bank], payouts_minor=payouts, losses_minor=losses)


def bank_plan(book, event, tick, post):
    plan = BankPlan(book, tick)
    key = event.reference
    action = event.action
    config = book.banking
    plan.details.update(action=action, reference=key)
    try:
        if action == 'capital':
            if key not in config.initial_bank_cash:
                raise ValueError('Unknown reserve member')
            plan.capital(key)
        elif action == 'scheduled':
            op = next((op for op in config.operations if op.tick == tick and op.operation_id == key), None)
            if op is None:
                raise ValueError('Unregistered banking instruction')
            plan.scheduled(op)
        elif action in ('notice', 'reaction'):
            spec = config.run_notice
            if spec is None or not spec.disclose or (
                    action == 'notice' and (key != spec.source_request or tick != spec.tick)
                    or action == 'reaction' and (spec.response_mode != 'scripted'
                        or key not in spec.withdrawal_by_recipient or tick != spec.tick + 1)):
                raise ValueError('Unregistered banking notice or response')
            if action == 'notice':
                plan.notice()
            else:
                plan.reaction(key)
        elif action == 'queue':
            request = plan.positions.withdrawals.get(key)
            if request is None or request.status != 'queued':
                raise ValueError('No eligible withdrawal to service')
            plan.serve(request)
        elif action in ('support_disburse', 'support_collect'):
            terms = config.treasury_support
            if terms is None or key != terms.bank_id:
                raise ValueError('Unregistered Treasury support instruction')
            (plan.disburse_support if action == 'support_disburse' else plan.collect_support)(terms)
        else:
            loan = plan.positions.loans.get(key)
            states = ('committed',) if action == 'fund' else ('active', 'delinquent')
            if loan is None or loan.status not in states or loan.next_tick != tick:
                raise ValueError('No due banking contract')
            (plan.fund if action == 'fund' else plan.due)(loan)
    except Refusal as exc:
        return {'status': 'rejected', 'reason': str(exc), 'action': action, 'reference': key, 'changes': {}}
    previous = book._bank_positions.model_dump()
    changes = {table: {key: row for key, row in values.items() if previous.get(table, {}).get(key) != row}
               for table, values in BankPositions.model_validate(vars(plan.positions)).model_dump().items()}
    for row in plan.postings:
        post(*row)
    return {**plan.details, 'changes': {k: v for k, v in changes.items() if v}}


def withdrawal_plan(book, event, tick):
    spec = book.banking.run_notice if book.banking is not None else None
    actor, intent = event.actor_id, event.intent
    if spec is None or spec.response_mode != 'actor' or tick != spec.tick:
        raise Refusal('withdrawal_notice_unavailable')
    notice = book._journal.get(f'bank:{tick}:notice:{spec.source_request}')
    if (notice is None or actor not in notice.details.get('reached', ())
            or notice.details['content'] != 'verified_withdrawal_delay'
            or intent.notice_fact_id != f'bank-notice:{notice.tx_id}:{actor}'
            or intent.bank_id != notice.details['bank']
            or intent.amount_minor > spec.withdrawal_by_recipient[actor]):
        raise Refusal('withdrawal_notice_unauthorized')
    request_id = f'notice:{actor}'
    if request_id in book._bank_positions.withdrawals:
        raise Refusal('withdrawal_notice_already_used')
    plan = BankPlan(book, tick)
    plan.open_bank(intent.bank_id)
    plan.request_withdrawal(BankOperation(tick=tick, operation_id=request_id, kind='withdraw',
                           bank=intent.bank_id, actor=actor, amount_minor=intent.amount_minor, expires_tick=tick + 1))
    return {'status': 'queued', 'notice_tx_id': notice.tx_id, 'request_id': request_id,
            'changes': {'withdrawals': {request_id: plan.positions.withdrawals[request_id].model_dump()}}}


def eligible_credit_offer(book, actor, tick):
    offer = book.banking.credit_offer if book.banking is not None else None
    if offer and (offer.actor, offer.tick) == (actor, tick) and offer.bank not in book._bank_positions.resolution:
        return offer
    return None


def credit_offer_id(offer):
    return f'credit-offer:{offer.tick}:{offer.operation_id}:{offer.actor}'


def credit_application_plan(book, event, tick):
    intent = event.intent
    offer = eligible_credit_offer(book, event.actor_id, tick)
    if (offer is None or intent.offer_fact_id != credit_offer_id(offer)
            or (intent.bank_id, intent.amount_minor) != (offer.bank, offer.amount_minor)):
        raise Refusal('credit_offer_unauthorized')
    if offer.operation_id in book._bank_positions.loans:
        raise Refusal('credit_offer_already_used')
    plan = BankPlan(book, tick)
    plan.scheduled(offer)
    return {**plan.details, 'offer_fact_id': intent.offer_fact_id,
            'changes': {'loans': {offer.operation_id: plan.positions.loans[offer.operation_id].model_dump()}}}


def bank_report(book):
    positions = book._bank_positions
    banks = {}
    for bank in book.banking.initial_bank_cash:
        b = book._balances[bank]
        assets, liabilities, equity = gross_and_equity(b)
        queued = sum(row.remaining_minor for row in positions.withdrawals.values()
                     if row.terms.bank == bank and row.status == 'queued')
        reserves = b.get('bank_reserve', 0)
        banks[bank] = {'assets_minor': assets, 'liabilities_minor': liabilities, 'equity_minor': equity,
                       'reserves_minor': reserves, 'deposit_liabilities_minor': -b.get('deposit_payable', 0),
                       'gross_principal_minor': b.get('loan_asset', 0), 'interest_claims_minor': b.get('interest_asset', 0),
                       'allowance_minor': -b.get('loan_allowance', 0), 'queued_minor': queued,
                       'illiquid': queued > reserves, 'insolvent': equity < 0,
                       'resolution': positions.resolution.get(bank, 'open')}
    return {'mode': book.banking.credit_creation_mode, 'banks': banks, 'deposit_claims': positions.deposits,
            'loans': {key: val.model_dump() for key, val in positions.loans.items()},
            'withdrawals': {key: val.model_dump() for key, val in positions.withdrawals.items()},
            'guarantee_cash_minor': book._available_cash(GUARANTOR),
            'guarantee_bound_minor': exclusive_limit(book.banking),
            'guarantee_spent_minor': book.banking.guarantee_budget_minor - book._available_cash(GUARANTOR),
            'reserve_issuer_liability_minor': -book._balances[ISSUER].get('reserve_payable', 0),
            **({'support_advances': {bank: row.model_dump() for bank, row in positions.support_advances.items()}}
               if positions.support_advances else {})}


def verify_claims(config, balances, positions):
    def check(actor, code, amount):
        if balances[actor].get(code, 0) != amount:
            raise ValueError(f'Bank claim mirror mismatch for {actor}/{code}')

    def add(table, key, amount):
        table[key] = table.get(key, 0) + amount

    if not set(positions.deposits) <= set(config.initial_bank_cash):
        raise ValueError('Deposit claim issuer is unregistered')
    deposits, lender_principal, borrower_principal = {}, {}, {}
    lender_interest, borrower_interest, allowances, held = {}, {}, {}, {}
    for bank, accounts in positions.deposits.items():
        for actor, amount in accounts.items():
            add(deposits, actor, amount)
    for loan in positions.loans.values():
        bank, borrower = loan.terms.bank, loan.terms.actor
        if loan.allowance_minor > loan.principal_minor + loan.interest_minor:
            raise ValueError('Loan allowance exceeds the nominal claim')
        add(lender_principal, bank, loan.principal_minor)
        add(borrower_principal, borrower, loan.principal_minor)
        add(lender_interest, bank, loan.interest_minor)
        add(borrower_interest, borrower, loan.interest_minor)
        add(allowances, bank, loan.allowance_minor)
    support = config.treasury_support
    for bank, advance in positions.support_advances.items():
        if (support is None or bank != support.bank_id or
                advance.outstanding_minor + advance.bank_repaid_minor + advance.guarantee_paid_minor
                != support.amount_minor or advance.guarantee_paid_minor > support.guarantee_limit_minor
                or (advance.status == 'paid') != (advance.outstanding_minor == 0)):
            raise ValueError('Treasury support principal and authorization do not reconcile')
        add(lender_principal, TREASURY, advance.outstanding_minor)
        add(borrower_principal, bank, advance.outstanding_minor)
        add(allowances, TREASURY, advance.allowance_minor)
    for request in positions.withdrawals.values():
        op = request.terms
        if request.remaining_minor + request.paid_minor != op.amount_minor:
            raise ValueError('Withdrawal conservation failure')
        if request.status == 'queued' and op.bank not in positions.resolution:
            add(held, (op.bank, op.actor), request.remaining_minor)
    for (bank, actor), amount in held.items():
        if positions.deposits.get(bank, {}).get(actor, 0) < amount:
            raise ValueError('Withdrawal holds exceed the depositor claim')
    reserves = sum(row.get('bank_reserve', 0) for row in balances.values())
    check(ISSUER, 'reserve_payable', -reserves)
    check(ISSUER, 'cash', reserves)
    if not set(deposits) <= set(balances):
        raise ValueError('Unknown owner of deposit claim')
    for actor in balances:
        check(actor, 'deposit_asset', deposits.get(actor, 0))
        check(actor, 'deposit_payable', -sum(positions.deposits.get(actor, {}).values()))
        check(actor, 'loan_asset', lender_principal.get(actor, 0))
        check(actor, 'loan_payable', -borrower_principal.get(actor, 0))
        check(actor, 'interest_asset', lender_interest.get(actor, 0))
        check(actor, 'interest_payable', -borrower_interest.get(actor, 0))
        check(actor, 'loan_allowance', -allowances.get(actor, 0))
        if actor not in config.initial_bank_cash:
            check(actor, 'bank_reserve', 0)
        if actor != ISSUER:
            check(actor, 'reserve_payable', 0)


def bank_facts(book, actor):
    own_deposits = {bank: claims[actor] for bank, claims in book._bank_positions.deposits.items() if actor in claims}
    own_loans = {key: loan.model_dump() for key, loan in book._bank_positions.loans.items()
                 if loan.terms.actor == actor}
    now = {'tick': book.tick, 'phase': 'OBSERVE'}
    facts = [ObservationFact(fact_id=f'bank-statement:{book.tick}:{actor}', category='market', owner_id=actor,
        visible_at=now, value={'kind': 'bank_statement', 'deposits': own_deposits, 'loans': own_loans})]
    offer = eligible_credit_offer(book, actor, book.tick)
    if offer is not None:
        facts.append(ObservationFact(fact_id=credit_offer_id(offer), category='market', owner_id=actor,
            visible_at=now, value={'kind': 'credit_offer', 'bank': offer.bank, 'amount_minor': offer.amount_minor,
                'installments': offer.installments, 'period_rate_bps': offer.period_rate_bps,
                'payment_interval': offer.payment_interval, 'default_after_misses': offer.default_after_misses,
                'loss_bps': offer.loss_bps, 'expires_tick': offer.tick, 'funding_tick': offer.tick + 1,
                'credit_creation_mode': book.banking.credit_creation_mode}))
    spec = book.banking.run_notice
    if spec is not None:
        entry = book._journal.get(f'bank:{spec.tick}:notice:{spec.source_request}')
        if entry is not None and actor in entry.details['reached']:
            facts.append(ObservationFact(fact_id=f'bank-notice:{entry.tx_id}:{actor}', category='market',
                owner_id=actor, visible_at={'tick': entry.tick, 'phase': 'OBSERVE'}, value={
                    'kind': entry.details['content'], 'bank': entry.details['bank'],
                    'source_tx_id': entry.details['source_tx_id'],
                    **({'withdrawal_limit_minor': spec.withdrawal_by_recipient[actor]}
                       if spec.response_mode == 'actor' else {})}))
    return facts
