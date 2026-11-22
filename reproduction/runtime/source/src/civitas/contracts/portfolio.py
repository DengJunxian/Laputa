"""Fixed-point v3 records; legacy financial v2 remains binary64."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import Id, NonNegative, Positive, Record, require_unique

MAX_INT = 2**63 - 1
Units = Annotated[int, Field(ge=0, le=MAX_INT)]
SignedUnits = Annotated[int, Field(ge=-MAX_INT, le=MAX_INT)]
Ticks = Annotated[int, Field(gt=0, le=MAX_INT)]


class Rate(Record):
    numerator: Units = 0
    denominator: Ticks = 1

    @model_validator(mode='after')
    def range(self):
        if self.numerator > self.denominator:
            raise ValueError('Rate must lie in [0,1]')
        return self


class FeeSchedule(Record):
    schema_version: Literal[3] = 3
    fee_id: Id = 'fees-v1'
    commission: Rate = Field(default_factory=Rate)
    tax: Rate = Field(default_factory=Rate)
    rounding: Literal['floor_per_fill'] = 'floor_per_fill'

    @model_validator(mode='after')
    def proceeds(self):
        c, t = self.commission, self.tax
        if c.numerator*t.denominator+t.numerator*c.denominator > c.denominator*t.denominator:
            raise ValueError('Seller fees cannot exceed proceeds')
        return self


class AssetSpec(Record):
    schema_version: Literal[3] = 3
    asset_id: Id
    quote_currency: Id
    quantity_scale: Literal[1] = 1
    price_tick_minor: Ticks
    settlement_rule: Literal['T+0', 'T+1'] = 'T+0'


class AccountState(Record):
    schema_version: Literal[3] = 3
    account_id: Id
    balances: dict[Id, Units]
    locked: dict[Id, Units] = Field(default_factory=dict)
    cost_minor: dict[Id, Units] = Field(default_factory=dict)


class PortfolioAccountView(AccountState):
    numeric_mode: Literal['fixedpoint_multiasset'] = 'fixedpoint_multiasset'
    cash_scale: Ticks = 1_000_000
    version: NonNegative = 0
    reserved: dict[Id, Units]
    available: dict[Id, Units]


class ReserveCommand(Record):
    schema_version: Literal[3] = 3
    command_id: Id
    reservation_id: Id
    order_id: Id
    account_id: Id
    asset_id: Id
    side: Literal['buy','sell']
    limit_price_ticks: Ticks
    quantity_units: Units


class Reservation(Record):
    schema_version: Literal[3] = 3
    reservation_id: Id
    command: ReserveCommand
    resource_id: Id
    amount: Units
    remaining_units: Units


class Posting(Record):
    schema_version: Literal[3] = 3
    posting_id: Id
    tx_id: Id
    account_id: Id
    resource_id: Id
    amount: SignedUnits


class PortfolioTrade(Record):
    schema_version: Literal[3] = 3
    trade_id: Id
    asset_id: Id
    buyer_id: Id
    seller_id: Id
    buy_order_id: Id
    sell_order_id: Id
    buyer_reservation_id: Id
    seller_reservation_id: Id
    maker_order_id: Id
    taker_order_id: Id
    price_ticks: Ticks
    quantity_units: Units
    tick: Positive


class LedgerTransaction(Record):
    schema_version: Literal[3] = 3
    tx_id: Id
    trade: PortfolioTrade
    day: NonNegative
    fees: FeeSchedule
    notional_minor: Units
    buyer_fee_minor: Units
    seller_fee_minor: Units
    tax_minor: Units
    postings: list[Posting]

    @model_validator(mode='after')
    def identities(self):
        require_unique([p.posting_id for p in self.postings], 'posting ID')
        if any(p.tx_id != self.tx_id for p in self.postings):
            raise ValueError('Posting transaction identity mismatch')
        return self


class PortfolioLedgerState(Record):
    schema_version: Literal[3] = 3
    state_id: Literal['portfolio_ledger'] = 'portfolio_ledger'
    backend: Literal['fixedpoint_multiasset'] = 'fixedpoint_multiasset'
    cash_scale: Ticks
    assets: list[AssetSpec]
    initial_accounts: list[AccountState]
    accounts: list[AccountState]
    day: NonNegative
    fees: FeeSchedule
    reservations: list[Reservation]
    journal: list[LedgerTransaction]


from .decisions import HoldIntent
from .financial import CancelIntent, FinancialCommand, FinancialDecisionBundle, FinancialExecutionReceipt


class PortfolioOrderIntent(Record):
    schema_version: Literal[3] = 3
    kind: Literal['portfolio_order'] = 'portfolio_order'
    intent_id: Id
    order_id: Id
    asset_id: Id
    side: Literal['buy','sell']
    price_ticks: Ticks
    quantity_units: Units


PortfolioIntent = HoldIntent | PortfolioOrderIntent | CancelIntent


class PortfolioDecisionBundle(FinancialDecisionBundle):
    schema_version: Literal[3] = 3
    economic_intents: list[PortfolioIntent] = Field(default_factory=list)


class PortfolioCommand(FinancialCommand):
    schema_version: Literal[3] = 3
    intent: PortfolioIntent


class PortfolioExecutionReceipt(FinancialExecutionReceipt):
    schema_version: Literal[3] = 3
    numeric_mode: Literal['fixedpoint_multiasset'] = 'fixedpoint_multiasset'
    reservation_ids: list[Id] = Field(default_factory=list)


class PortfolioOrder(Record):
    schema_version: Literal[3] = 3
    command: ReserveCommand
    decision_id: Id
    accepted_tick: Positive
    arrival_sequence: Positive
    remaining_units: Units
    status: Literal['active','filled','cancelled']


class PortfolioOrderEvent(Record):
    schema_version: Literal[3] = 3
    event_id: Id
    order_id: Id
    tick: Positive
    event: Literal['accepted','filled','partial','cancelled','rejected']
    reason: str = ''
    remaining_units: Units


class MatchingVenueState(Record):
    schema_version: Literal[3] = 3
    asset_id: Id
    last_price_ticks: Ticks
    arrival_sequence: NonNegative
    trade_count: NonNegative
    orders: list[PortfolioOrder]


class MultiAssetState(Record):
    schema_version: Literal[3] = 3
    state_id: Literal['multiasset_market'] = 'multiasset_market'
    backend: Literal['fixedpoint_multiasset'] = 'fixedpoint_multiasset'
    ledger: PortfolioLedgerState
    venues: list[MatchingVenueState]
    tick: NonNegative
    world_version: NonNegative
    commands: list[PortfolioCommand]
    receipts: list[PortfolioExecutionReceipt]
    events: list[PortfolioOrderEvent]
