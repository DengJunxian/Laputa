from typing import Literal

from pydantic import Field, model_validator

from .base import Id, NonNegative, Positive, Record, require_unique
from .events import Event
from .time import PhaseName


class MechanismSpec(Record):
    mechanism_id: Id
    version: Id
    phase: PhaseName
    reads: list[Id]
    writes: list[Id]

    @model_validator(mode="after")
    def declarations(self):
        require_unique(self.reads, "read field")
        require_unique(self.writes, "write field")
        if self.phase not in ("BOUNDARY", "EVENTS", "FEEDBACK"):
            raise ValueError("WP01 mechanisms run at BOUNDARY, EVENTS or FEEDBACK")
        return self


class RunConfig(Record):
    schema_version: Literal[1] = 1
    scenario: Literal["toy_transfer"] = "toy_transfer"
    root_seed: NonNegative = 42
    replica: NonNegative = 0
    ticks: Positive = 5
    concurrency: Positive = Field(default=1, le=64)
    initial_balances: dict[Id, NonNegative] = Field(default_factory=lambda: {"alice": 100, "bob": 100})
    transfer_amount: Positive = 3
    events: list[Event] = Field(default_factory=list)

    @model_validator(mode="after")
    def toy_boundary(self):
        if len(self.initial_balances) != 2 or not all(self.initial_balances.values()):
            raise ValueError("Toy requires exactly two prefunded accounts")
        require_unique([event.event_id for event in self.events], "event ID")
        known = {event.event_id: event for event in self.events}
        for event in self.events:
            if event.phase != "EVENTS" or not 1 <= event.tick <= self.ticks:
                raise ValueError("Scheduled events must occur in EVENTS within this run")
            if not set(event.actors) <= self.initial_balances.keys():
                raise ValueError("Unknown event actor")
            if event.parent_event_id is not None:
                parent = known.get(event.parent_event_id)
                if (parent is None or parent.tick >= event.tick or parent.root_event_id != event.root_event_id):
                    raise ValueError("Event lineage requires an earlier parent with the same root")
        return self
