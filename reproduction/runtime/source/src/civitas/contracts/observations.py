from typing import Any, Literal

from pydantic import Field, ValidationInfo, model_validator
from core.experiments.identity import research_hash_record_rows

from .base import Digest, Id, NonNegative, Record, require_unique, retained_record
from .time import SimTime


_DERIVE_OBSERVATION_ID = object()


class AccountView(Record):
    actor_id: Id
    account_id: Id
    version: NonNegative
    numeric_mode: Literal["integer"] = "integer"
    unit: Literal["token"] = "token"
    balance: NonNegative
    reserved: NonNegative
    available: NonNegative

    @model_validator(mode="after")
    def conservation(self):
        if self.balance != self.reserved + self.available:
            raise ValueError("Account available plus reserved must equal balance")
        return self


class ObservationFact(Record):
    fact_id: Id
    category: Literal["account", "market", "memory"]
    owner_id: Id | None = None
    public: bool = False
    visible_at: SimTime
    value: dict[str, Any]


class Message(Record):
    message_id: Id
    sender_id: Id
    recipient_id: Id
    created_at: SimTime
    visible_at: SimTime
    expires_at: SimTime | None = None
    body: dict[str, Any]

    @model_validator(mode="after")
    def chronology(self):
        if self.visible_at.key < self.created_at.key:
            raise ValueError("Message visible before creation")
        if self.expires_at is not None and self.expires_at.key <= self.visible_at.key:
            raise ValueError("Message expiry must follow visibility")
        return self


class Observation(Record):
    schema_version: Literal[1] = 1
    observation_id: Id
    actor_id: Id
    batch_id: Id
    observation_version: NonNegative
    world_version: NonNegative
    visible_until: SimTime
    account_view: list[retained_record(ObservationFact)] = Field(default_factory=list)
    market_view: list[retained_record(ObservationFact)] = Field(default_factory=list)
    delivered_messages: list[retained_record(Message)] = Field(default_factory=list)
    private_memory_refs: list[retained_record(ObservationFact)] = Field(default_factory=list)
    payload_hash: Digest

    def payload(self) -> dict:
        return self.model_dump(exclude={"observation_id", "payload_hash"}, mode="json")

    @classmethod
    def from_views(cls, **payload):
        return cls.model_validate(dict(**payload, observation_id="pending", payload_hash="0" * 64),
                                  context=_DERIVE_OBSERVATION_ID)

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset([f.fact_id for f in self.account_view + self.market_view + self.private_memory_refs]
                         + [m.message_id for m in self.delivered_messages])

    @model_validator(mode="after")
    def permissions_and_identity(self, info: ValidationInfo):
        facts = self.account_view + self.market_view + self.private_memory_refs
        require_unique([f.fact_id for f in facts] + [m.message_id for m in self.delivered_messages],
                       "observation evidence ID")
        for category, collection in (("account", self.account_view), ("market", self.market_view),
                                     ("memory", self.private_memory_refs)):
            for fact in collection:
                allowed = fact.owner_id == self.actor_id or (category == "market" and fact.public)
                if fact.category != category or not allowed or fact.visible_at.key > self.visible_until.key:
                    raise ValueError("Observation contains an unauthorized or future fact")
        for message in self.delivered_messages:
            if (message.recipient_id != self.actor_id or message.visible_at.key > self.visible_until.key
                    or (message.expires_at and message.expires_at.key <= self.visible_until.key)):
                raise ValueError("Observation contains an unauthorized, future or expired message")
        digest = research_hash_record_rows(self, ("account_view", "market_view", "delivered_messages", "private_memory_refs"),
                                           exclude=("observation_id", "payload_hash"))
        if info.context is _DERIVE_OBSERVATION_ID:
            object.__setattr__(self, "payload_hash", digest)
            object.__setattr__(self, "observation_id", f"observation:{digest}")
        elif digest != self.payload_hash:
            raise ValueError("Observation payload hash mismatch")
        return self
