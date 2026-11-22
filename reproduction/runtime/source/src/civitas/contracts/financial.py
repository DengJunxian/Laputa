"""Financial v2 records; the WP01 toy v1 records keep their original wire form."""
from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Id, NonNegative, Positive, Record, require_unique
from .decisions import Command, DecisionBundle, HoldIntent
from .receipts import ExecutionReceipt


class OrderIntent(Record):
    kind: Literal["order"] = "order"
    intent_id: Id
    order_id: Id
    symbol: Id
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market", "ioc", "fok", "post-only"] = "limit"
    price: float = Field(gt=0)
    quantity: Positive


class CancelIntent(Record):
    kind: Literal["cancel"] = "cancel"
    intent_id: Id
    order_id: Id


FinancialIntent = HoldIntent | OrderIntent | CancelIntent


class FinancialDecisionBundle(DecisionBundle):
    schema_version: Literal[2] = 2
    tick: Positive
    economic_intents: list[FinancialIntent] = Field(default_factory=list)


class FinancialCommand(Command):
    schema_version: Literal[2] = 2
    intent: FinancialIntent
    tick: Positive
    batch_id: Id
    observation_version: NonNegative
    world_version: NonNegative


class FinancialExecutionReceipt(ExecutionReceipt):
    schema_version: Literal[2] = 2
    status: Literal["accepted", "rejected", "partially_executed", "cancelled"]
    unit: Literal["shares"] = "shares"
    order_ids: list[Id] = Field(default_factory=list)
    trade_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def financial_ids(self):
        for values in (self.order_ids, self.trade_ids, self.ledger_tx_ids):
            require_unique(values, "receipt reference")
        if self.status == "rejected" and self.trade_ids:
            raise ValueError("A rejection cannot contain trades")
        return self


class LegacyAccountView(Record):
    schema_version: Literal[2] = 2
    actor_id: Id
    account_id: Id
    version: NonNegative
    numeric_mode: Literal["legacy_float64"] = "legacy_float64"
    cash_unit: Literal["model_cash"] = "model_cash"
    share_unit: Literal["shares"] = "shares"
    cash: float = Field(ge=-1e-7)
    reserved_cash: float = Field(ge=-1e-7)
    available_cash: float = Field(ge=-1e-7)
    shares: NonNegative
    reserved_shares: NonNegative
    available_shares: NonNegative
    locked_shares: NonNegative
    acquired_today: NonNegative
    cost_basis: float = Field(ge=0)


class OrderEvent(Record):
    schema_version: Literal[2] = 2
    event_id: Id
    step: NonNegative
    order_id: Id
    decision_id: Id
    order_type: Literal["limit", "market", "ioc", "fok", "post-only"] = "limit"
    event: Literal["pending", "partial", "filled", "cancelled", "rejected", "cancel"]
    reason: str
    remaining_quantity: NonNegative


class LegacyOrderBatch(Record):
    schema_version: Literal[2] = 2
    commands: list[FinancialCommand] = Field(min_length=1)
    execution_seq_start: NonNegative = 0

    @model_validator(mode="after")
    def identity(self):
        require_unique([c.command_id for c in self.commands], "command ID")
        keys = {(c.tick, c.batch_id, c.observation_version, c.world_version) for c in self.commands}
        if len(keys) != 1:
            raise ValueError("Orders must share one observation batch")
        return self


class LegacySettlementState(Record):
    state_schema_version: Literal[1] = 1
    backend: Literal["legacy_float64"] = "legacy_float64"
    orders: list[dict[str, Any]]
    clearing: dict[str, Any]
    book: dict[str, Any]
    adapter: dict[str, Any]
