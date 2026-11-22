from enum import StrEnum
from typing import Literal

from .base import NonNegative, Record


class Phase(StrEnum):
    BOUNDARY = "BOUNDARY"
    EVENTS = "EVENTS"
    OBSERVE = "OBSERVE"
    DECIDE = "DECIDE"
    VALIDATE = "VALIDATE"
    EXECUTE = "EXECUTE"
    FEEDBACK = "FEEDBACK"
    COMMIT = "COMMIT"


# Wire values are strings, preserving the shared strict JSON scalar contract.
PhaseName = Literal["BOUNDARY", "EVENTS", "OBSERVE", "DECIDE", "VALIDATE",
                    "EXECUTE", "FEEDBACK", "COMMIT"]


class SimTime(Record):
    tick: NonNegative
    phase: PhaseName

    @property
    def key(self) -> tuple[int, int]:
        return self.tick, tuple(Phase).index(self.phase)
