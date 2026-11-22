from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Id, NonNegative, Record, require_unique
from .time import PhaseName, SimTime


class Event(Record):
    schema_version: Literal[1] = 1
    event_id: Id
    root_event_id: Id
    parent_event_id: Id | None = None
    tick: NonNegative
    phase: PhaseName
    kind: Id
    actors: list[Id]
    occurred_at: SimTime | None = None
    visible_at: SimTime
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def chronology(self):
        occurred = SimTime(tick=self.tick, phase=self.phase)
        if self.occurred_at is None:
            object.__setattr__(self, "occurred_at", occurred)
        if self.occurred_at != occurred or self.visible_at.key < occurred.key:
            raise ValueError("Event occurrence/visibility times disagree")
        if self.parent_event_id is None and self.root_event_id != self.event_id:
            raise ValueError("A root event must name itself")
        if self.parent_event_id == self.event_id:
            raise ValueError("An event cannot parent itself")
        require_unique(self.actors, "event actor")
        return self
