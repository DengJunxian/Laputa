"""Fiscal terms and posting plan for settled wages and a cash-backed public budget."""
from typing import Annotated

from pydantic import Field, model_validator

from civitas.contracts.base import NonNegative, Positive, Record, require_unique
from civitas.contracts.economic import Amount
from civitas.contracts.observations import ObservationFact
from civitas.population.allocation import allocate

TREASURY = 'public:treasury'
OPERATIONS = 'public:disclosure'
DEBIT_CODES = {'tax_expense', 'tax_receivable', 'transfer_expense', 'disclosure_expense'}
CREDIT_CODES = {'tax_payable', 'tax_income', 'transfer_income', 'disclosure_income'}


class TaxBracket(Record):
    start_minor: Amount
    rate_bps: Annotated[int, Field(ge=0, le=10000)]


class FiscalPolicy(Record):
    initial_cash_minor: Amount = 0
    announced_tick: Positive
    effective_tick: Positive
    audience: list[NonNegative]
    disclosure_cost_minor: Amount = 0
    brackets: list[TaxBracket] = Field(min_length=1)
    transfer_recipients: list[NonNegative]
    transfer_minor: Amount = 0
    consumption_floor_units: Positive | None = Field(default=None, exclude_if=lambda value: value is None)
    precautionary_bps: Annotated[int, Field(ge=0, le=10000)] = 0

    @model_validator(mode='after')
    def terms(self):
        if self.announced_tick >= self.effective_tick:
            raise ValueError('Fiscal announcement must precede legal effect')
        starts = [b.start_minor for b in self.brackets]
        if starts[0] != 0 or starts != sorted(set(starts)):
            raise ValueError('Tax brackets require distinct ascending cutoffs starting at zero')
        require_unique(self.audience, 'fiscal audience')
        require_unique(self.transfer_recipients, 'transfer recipient')
        if self.transfer_minor and not self.transfer_recipients:
            raise ValueError('A positive transfer needs eligible recipients')
        if self.consumption_floor_units is not None and not self.transfer_minor:
            raise ValueError('A consumption floor requires an authorized positive transfer')
        return self


def progressive_tax(income, brackets):
    """Clip income by bracket and round the sum down once, in minor units.

    Adapted from AI Economist PeriodicBracketTax.taxes_due (BSD-3-Clause).
    """
    upper = [b.start_minor for b in brackets[1:]] + [max(income, brackets[-1].start_minor)]
    numerator = sum(b.rate_bps * min(cap - b.start_minor, max(0, income - b.start_minor))
                    for b, cap in zip(brackets, upper, strict=True))
    return numerator // 10000


def _cash_transfer(post, payer, payee, amount, expense, income):
    post(payer, expense, 'debit', amount)
    post(payer, 'cash', 'credit', amount)
    post(payee, 'cash', 'debit', amount)
    post(payee, income, 'credit', amount)


def pay_disclosure(intended, cash, cost, post):
    capacity = cash // cost if cost else len(intended)
    reached = intended[:capacity]
    paid = len(reached) * cost
    _cash_transfer(post, TREASURY, OPERATIONS, paid, 'disclosure_expense', 'disclosure_income')
    return reached, paid


def fiscal_plan(book, tick, post):
    policy = book.fiscal
    cash = book.account_view(TREASURY).available_cash_minor
    intended = [f'household:{i:04d}' for i in sorted(policy.audience)] if tick == policy.announced_tick else []
    reached, disclosure = pay_disclosure(intended, cash, policy.disclosure_cost_minor, post)
    taxes, transfers, collected, requested, response = {}, {}, 0, 0, None
    if tick >= policy.effective_tick:
        paid_wages = {}
        for entry in book.journal:
            if entry.tick == tick and entry.kind == 'wage_payment':
                worker = entry.payload['worker_id']
                amount, refs = paid_wages.get(worker, (0, []))
                paid_wages[worker] = (amount + entry.details['paid_minor'], [*refs, entry.tx_id])
        for worker, (base, refs) in sorted(paid_wages.items()):
            due = progressive_tax(base, policy.brackets)
            paid = min(due, book.account_view(worker).available_cash_minor)
            taxes[worker] = {'tax_base_minor': base, 'assessed_minor': due, 'paid_minor': paid, 'wage_tx_ids': refs}
            post(worker, 'tax_expense', 'debit', due)
            post(worker, 'tax_payable', 'credit', due)
            post(TREASURY, 'tax_receivable', 'debit', due)
            post(TREASURY, 'tax_income', 'credit', due)
            _cash_transfer(post, worker, TREASURY, paid, 'tax_payable', 'tax_receivable')
            collected += paid
        recipients = {f'household:{i:04d}': 1 for i in policy.transfer_recipients}
        requested = len(recipients) * policy.transfer_minor
        if policy.consumption_floor_units is not None:
            prior = [e for e in book.journal if e.kind == 'consumption' and e.tick == tick - 1]
            observed = sum(e.payload['quantity_units'] for e in prior)
            triggered = observed < policy.consumption_floor_units
            requested = requested if triggered else 0
            response = {'published_tick': tick - 1, 'consumed_units': observed,
                        'floor_units': policy.consumption_floor_units, 'triggered': triggered,
                        'source_tx_ids': [e.tx_id for e in prior]}
        outlay = min(cash + collected - disclosure, requested)
        if recipients:
            transfers = {row.account_id: row.allocated_units for row in allocate(outlay, recipients, 'fiscal')}
        for recipient, amount in transfers.items():
            _cash_transfer(post, TREASURY, recipient, amount, 'transfer_expense', 'transfer_income')
    return {'announced': intended, 'reached': reached, 'effective': tick >= policy.effective_tick,
            'assessments': taxes, 'tax_collected_minor': collected, 'transfers_minor': transfers,
            'transfers_paid_minor': sum(transfers.values()), 'requested_transfer_minor': requested,
            'disclosure_paid_minor': disclosure,
            'treasury_cash_minor': cash + collected - disclosure - sum(transfers.values()),
            **({'consumption_response': response} if response is not None else {})}


def fiscal_facts(book, actor):
    policy = book.fiscal
    visible = {'tick': book.tick, 'phase': 'OBSERVE'}
    facts = []
    for tick in dict.fromkeys((policy.announced_tick, book.tick)):
        entry = book._journal.get(f'fiscal:{tick}')
        if entry is None:
            continue
        kind = None
        if actor in entry.details['reached']:
            kind = 'fiscal_announcement'
            value = {'effective_tick': policy.effective_tick,
                     'brackets': [b.model_dump() for b in policy.brackets],
                     'eligible_for_transfer': actor in {f'household:{i:04d}' for i in policy.transfer_recipients}}
            if policy.consumption_floor_units is not None:
                value['consumption_floor_units'] = policy.consumption_floor_units
            if policy.precautionary_bps and book.tick < policy.effective_tick:
                wage = sum(e.details['paid_minor'] for e in book.journal
                           if (e.kind == 'wage_payment' and e.tick == book.tick and e.payload['worker_id'] == actor))
                value['precautionary_reserve_minor'] = progressive_tax(wage, policy.brackets) * policy.precautionary_bps // 10000
        elif entry.tick == book.tick and (actor in entry.details['assessments'] or actor in entry.details['transfers_minor']):
            kind = 'fiscal_settlement'
            value = {'assessment': entry.details['assessments'].get(actor),
                     'transfer_minor': entry.details['transfers_minor'].get(actor, 0)}
        if kind:
            facts.append(ObservationFact(fact_id=f'{kind}:{entry.event_id}:{actor}', category='market',
                owner_id=actor, visible_at={**visible, 'tick': entry.tick},
                value={'kind': kind, 'ledger_tx_id': entry.tx_id, **value}))
    return facts


def fiscal_metrics(book):
    rows = [e.details for e in book.journal if e.kind == 'fiscal']
    fields = ('tax_collected_minor', 'transfers_paid_minor', 'disclosure_paid_minor', 'requested_transfer_minor')
    summary = {key: sum(row[key] for row in rows) for key in fields}
    bank_outlays = [e.details['disclosure_paid_minor'] for e in book.journal if e.kind == 'bank'
                    and 'disclosure_by_recipient_minor' in e.details]
    bank_disclosure = sum(bank_outlays)
    support = [e.details for e in book.journal if e.kind == 'bank'
               and e.payload['action'] in ('support_disburse', 'support_collect')]
    support_net = sum(e.get('disbursed_minor', 0) - e.get('bank_repaid_minor', 0)
                      - e.get('guarantee_paid_minor', 0) for e in support)
    return {**summary, 'treasury_cash_minor': book.account_view(TREASURY).cash_minor,
            **({'bank_disclosure_paid_minor': bank_disclosure} if bank_outlays else {}),
            **({'support_net_cash_outlay_minor': support_net} if support else {}),
            'audience': sorted({actor for row in rows for actor in row['announced']}),
            'reached': sorted({actor for row in rows for actor in row['reached']}),
            'fiscal_net_outlay_minor': summary['transfers_paid_minor'] + summary['disclosure_paid_minor'] + bank_disclosure
                                      + support_net
                                      - summary['tax_collected_minor']}
