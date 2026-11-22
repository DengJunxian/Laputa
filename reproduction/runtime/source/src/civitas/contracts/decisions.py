from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Id, NonNegative, Positive, Record, require_unique


class HoldIntent(Record):
    kind: Literal["hold"] = "hold"
    intent_id: Id


class TransferIntent(Record):
    kind: Literal["transfer"] = "transfer"
    intent_id: Id
    recipient_id: Id
    amount: Positive
    unit: Literal["token"] = "token"


EconomicIntent = HoldIntent | TransferIntent


class MessageDraft(Record):
    message_id: Id
    recipient_id: Id
    body: dict[str, Any]


class DecisionBundle(Record):
    schema_version: Literal[1] = 1
    decision_id: Id
    actor_id: Id
    observation_id: Id
    batch_id: Id
    observation_version: NonNegative
    world_version: NonNegative
    policy_id: Id
    policy_version: Id
    economic_intents: list[EconomicIntent] = Field(default_factory=list)
    belief_patch: dict[str, Any] = Field(default_factory=dict)
    message_drafts: list[MessageDraft] = Field(default_factory=list)
    evidence_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        require_unique([self.decision_id, *(i.intent_id for i in self.economic_intents),
                        *(m.message_id for m in self.message_drafts)], "decision/intent/message ID")
        require_unique(self.evidence_ids, "evidence ID")
        return self


class Command(Record):
    schema_version: Literal[1] = 1
    command_id: Id
    decision_id: Id
    actor_id: Id
    local_order_index: NonNegative
    arrival_key: NonNegative
    intent: EconomicIntent
