"""Complete logical tick state, separate from external invocation facts."""
from typing import Annotated, Any, Literal

from pydantic import Field, WrapSerializer, WrapValidator, field_validator, model_validator
from core.experiments.identity import SEALED_SNAPSHOT_LISTS, research_hash_record_rows
from core.experiments.strict_types import SealedJSONObjectV1, SealedJSONRowsV1, SealedRecordRows

from .base import Digest, Id, NonNegative, Record, require_unique, retained_record
from .decisions import DecisionBundle
from .economic import EconomicDecisionBundle, EconomicExecutionReceipt
from .events import Event
from .financial import FinancialDecisionBundle, FinancialExecutionReceipt
from .portfolio import PortfolioDecisionBundle, PortfolioExecutionReceipt
from .observations import Message, ObservationFact
from .receipts import ExecutionReceipt
from .time import SimTime


APPEND_ONLY_WORLD_RECORDS = ("decisions", "receipts", "memories")


def _sealed_history(record_type, key):
    def validate(value, handler):
        if type(value) is SealedRecordRows:
            if value.record_type is not record_type or value.key != key:
                raise ValueError("Sealed history differs from the declared field")
            return value
        return SealedRecordRows(record_type, key, handler(value))

    def serialize(value, handler):
        return handler(list(value))

    return Annotated[list[retained_record(record_type)], WrapValidator(validate), WrapSerializer(serialize)]


class TickContext(Record):
    tick: NonNegative
    world_version: NonNegative
    execution_seq: NonNegative
    actors: list[Id]
    data: dict[str, Any]

    def working_dump(self, *, mode="python", exclude=None):
        """Copy mutable working fields and share sealed data."""
        if exclude is not None and "data" in exclude:
            return self.model_dump(mode=mode, exclude=exclude)
        objects = {field: value for field, value in self.data.items() if SealedJSONObjectV1.is_sealed(value)}
        retained = {}
        for domain, field in SEALED_SNAPSHOT_LISTS:
            if domain in objects:
                continue
            group = self.data.get(domain)
            rows = group.get(field) if isinstance(group, dict) else None
            if type(rows) is SealedJSONRowsV1:
                retained.setdefault(domain, {})[field] = rows
            elif (domain, field) == ("closed_social", "trace") and isinstance(rows, list) and all(
                    isinstance(row, dict) for row in rows):
                retained.setdefault(domain, {})[field] = SealedJSONRowsV1(None, [dict(row) for row in rows])
        if not retained and not objects:
            return self.model_dump(mode=mode, exclude=exclude)
        exclusions = dict(exclude) if isinstance(exclude, dict) else dict.fromkeys(exclude or (), True)
        exclusions["data"] = {**{domain: set(fields) for domain, fields in retained.items()},
                              **dict.fromkeys(objects, True)}
        state = self.model_dump(mode=mode, exclude=exclusions)
        state["data"].update(objects)
        for domain, fields in retained.items():
            state["data"][domain].update(fields)
        return state


class RelationDelta(Record):
    delta_id: Id
    relation_id: Id
    source_id: Id
    target_id: Id
    layer: Id
    effective_at: SimTime
    value: dict[str, Any]


class Feedback(Record):
    data: dict[str, Any]
    memories: list[ObservationFact] = Field(default_factory=list)
    relations: list[RelationDelta] = Field(default_factory=list)


class ExecutionResult(Record):
    data: dict[str, Any]
    receipt: ExecutionReceipt | FinancialExecutionReceipt | PortfolioExecutionReceipt | EconomicExecutionReceipt


class BatchExecutionResult(Record):
    data: dict[str, Any]
    receipts: list[ExecutionReceipt | FinancialExecutionReceipt | PortfolioExecutionReceipt | EconomicExecutionReceipt]


class WorldSnapshot(TickContext):
    schema_version: Literal[1] = 1
    pending_events: list[Event] = Field(default_factory=list)
    events: list[retained_record(Event)] = Field(default_factory=list)
    decisions: list[retained_record(DecisionBundle)] = Field(default_factory=list)
    receipts: list[retained_record(ExecutionReceipt)] = Field(default_factory=list)
    messages: list[retained_record(Message)] = Field(default_factory=list)
    memories: list[retained_record(ObservationFact)] = Field(default_factory=list)
    beliefs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    relations: dict[str, RelationDelta] = Field(default_factory=dict)
    pending_relations: list[RelationDelta] = Field(default_factory=list)
    used_ids: list[Id] = Field(default_factory=list)

    @property
    def state_hash(self) -> str:
        digest = self.__dict__.get("_derived_state_hash")
        if digest is None:
            digest = research_hash_record_rows(self, ("events", "decisions", "receipts", "messages", "memories"))
            # Store plain instance metadata outside the public wire fields.
            object.__setattr__(self, "_derived_state_hash", digest)
        return digest

    @model_validator(mode="after")
    def identities(self):
        require_unique(self.actors, "actor ID")
        require_unique(self.used_ids, "world ID")
        for field, key in ((self.events + self.pending_events, "event_id"), (self.decisions, "decision_id"),
                           (self.receipts, "command_id"), (self.messages, "message_id"),
                           (self.memories, "fact_id"), (self.pending_relations, "delta_id")):
            if type(field) is not SealedRecordRows:
                require_unique([getattr(item, key) for item in field], key)
        if not set(self.beliefs) <= set(self.actors):
            raise ValueError("Unknown belief owner")
        return self


class PreparedTick(Record):
    base_version: NonNegative
    tick: NonNegative
    snapshot: WorldSnapshot
    state_hash: Digest
    invocation_receipt_refs: list[Id] = Field(default_factory=list)

    @field_validator("snapshot", mode="wrap")
    @classmethod
    def _validated_snapshot(cls, value, handler):
        # Reuse the exact recursively frozen record. Other inputs go through
        # the selected wire schema and its normal strict validation.
        if type(value) is cls.model_fields["snapshot"].annotation:
            return value
        return handler(value)

    @model_validator(mode="after")
    def identity(self):
        if (self.snapshot.world_version != self.base_version + 1 or self.snapshot.tick != self.tick
                or self.state_hash != self.snapshot.state_hash):
            raise ValueError("Prepared tick version/hash mismatch")
        return self


class FinancialWorldSnapshot(WorldSnapshot):
    schema_version: Literal[2] = 2
    decisions: list[retained_record(FinancialDecisionBundle)] = Field(default_factory=list)
    receipts: list[retained_record(FinancialExecutionReceipt)] = Field(default_factory=list)


class FinancialPreparedTick(PreparedTick):
    snapshot: FinancialWorldSnapshot


class PortfolioWorldSnapshot(WorldSnapshot):
    schema_version: Literal[3] = 3
    decisions: list[retained_record(PortfolioDecisionBundle)] = Field(default_factory=list)
    receipts: list[retained_record(PortfolioExecutionReceipt)] = Field(default_factory=list)


class PortfolioPreparedTick(PreparedTick):
    snapshot: PortfolioWorldSnapshot


class EconomicWorldSnapshot(WorldSnapshot):
    """v4 world records; economic participant state has its own version."""
    schema_version: Literal[4] = 4
    decisions: _sealed_history(EconomicDecisionBundle, "decision_id") = Field(default_factory=list)
    receipts: _sealed_history(EconomicExecutionReceipt, "command_id") = Field(default_factory=list)
    memories: _sealed_history(ObservationFact, "fact_id") = Field(default_factory=list)


class EconomicPreparedTick(PreparedTick):
    snapshot: EconomicWorldSnapshot
