"""A two-hop opportunity carries one witnessed disclosure, without a graph query."""
from typing import Literal

from pydantic import Field

from civitas.contracts.base import Id, Positive, Record, content_hash
from civitas.contracts.decisions import MessageDraft


INTRODUCTION_KIND = "candidate_introduction"


class CandidateIntroduction(Record):
    schema_version: Literal[1] = 1
    kind: Literal["candidate_introduction"] = INTRODUCTION_KIND
    graph_version: Literal[1] = 1
    observer_id: Id
    intermediary_id: Id
    candidate_id: Id
    root_claim_id: Id
    source_message_id: Id
    issued_tick: Positive
    expires_tick: Positive
    allow_follow: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def message_id(self):
        return "introduction:" + content_hash([self.source_message_id, self.observer_id])

    def draft(self):
        return MessageDraft(message_id=self.message_id,
                            recipient_id=self.observer_id, body=self.model_dump())


def introduction_from(source, recipient_id, tick):
    """Only the intermediary's current direct, opt-in delivery can be forwarded."""
    if (source.body.get("allow_introduction") is not True or source.sender_id == recipient_id
            or (source.visible_at.tick, source.visible_at.phase) != (tick, "OBSERVE")):
        return None
    return CandidateIntroduction(observer_id=recipient_id, intermediary_id=source.recipient_id,
        candidate_id=source.sender_id, root_claim_id=source.body["root_claim_id"],
        source_message_id=source.message_id, issued_tick=tick, expires_tick=tick + 2,
        allow_follow=source.body.get("allow_follow"))
