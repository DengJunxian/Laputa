from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Digest, Id, NonNegative, Record


class ExecutionReceipt(Record):
    schema_version: Literal[1] = 1
    command_id: Id
    decision_id: Id
    actor_id: Id
    execution_seq: NonNegative
    status: Literal["accepted", "rejected"]
    reason_codes: list[Id] = Field(default_factory=list)
    executed_quantity: NonNegative
    unit: Literal["token"] = "token"
    ledger_tx_ids: list[Id] = Field(default_factory=list)
    order_ids: list[Id] = Field(default_factory=list, max_length=0)
    trade_ids: list[Id] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def rejection(self):
        if self.status == "rejected" and (not self.reason_codes or self.executed_quantity or self.ledger_tx_ids):
            raise ValueError("Rejected commands need reasons and no execution")
        return self


class ModelCallReceipt(Record):
    """External invocation fact; no provider is implemented by WP01."""
    schema_version: Literal[1] = 1
    request_id: Id
    attempt_id: Id
    input_hash: Digest
    prompt_version: Id
    parser_version: Id
    model_version: Id
    raw_response_ref: Id | None = None
    usage: dict[str, NonNegative] = Field(default_factory=dict)
    latency_ms: NonNegative
    status: Literal["success", "error", "billing_unknown"]
    metadata: dict[str, Any] = Field(default_factory=dict)
