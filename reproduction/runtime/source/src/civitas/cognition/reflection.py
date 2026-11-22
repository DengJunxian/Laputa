"""Evidence-linked candidate notes, never internal reasoning or a ledger replacement."""
from pydantic import Field, model_validator

from civitas.contracts.base import Id, Record, require_unique


class ReflectionCandidate(Record):
    actor_id: Id
    observation_id: Id
    evidence_ids: list[Id]
    semantic: list[Id] = Field(default_factory=list, max_length=16)
    procedural: list[Id] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def evidence_required(self):
        require_unique(self.evidence_ids, 'reflection evidence')
        if (self.semantic or self.procedural) and not self.evidence_ids:
            raise ValueError('Reflection notes require witnessed evidence')
        return self

    def validate_for(self, observation):
        if (self.actor_id, self.observation_id) != (observation.actor_id, observation.observation_id):
            raise ValueError('Reflection identity mismatch')
        if not set(self.evidence_ids) <= observation.evidence_ids:
            raise ValueError('Reflection cites invisible evidence')
        return self
