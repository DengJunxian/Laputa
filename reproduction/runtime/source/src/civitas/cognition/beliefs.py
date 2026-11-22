"""Subjective probabilities over supplied joint scenarios; no WP06 optimizer."""
import math

from pydantic import Field, model_validator

from civitas.contracts.base import Id, NonNegative, Record, require_unique


class JointBelief(Record):
    probabilities: dict[Id, float]
    evidence_ids: list[Id]
    as_of_tick: NonNegative
    horizon_tick: NonNegative
    valid_until_tick: NonNegative
    abstain: bool = False

    @model_validator(mode='after')
    def probability_mass(self):
        require_unique(self.evidence_ids, 'belief evidence')
        if self.horizon_tick <= self.as_of_tick or self.valid_until_tick < self.as_of_tick:
            raise ValueError('Belief horizon and validity must follow its observation')
        if self.abstain:
            if self.probabilities:
                raise ValueError('Abstention cannot claim a probability distribution')
        elif (not self.probabilities or any(not 0 <= p <= 1 for p in self.probabilities.values())
              or not math.isclose(math.fsum(self.probabilities.values()), 1.0, rel_tol=0, abs_tol=1e-9)):
            raise ValueError('Joint probabilities must be in [0,1] and sum to one')
        return self

    def validate_for(self, observation, scenarios, max_horizon):
        if not self.abstain and set(self.probabilities) != set(scenarios):
            raise ValueError('Belief must cover exactly the given joint scenarios')
        if self.as_of_tick != observation.visible_until.tick:
            raise ValueError('Stale belief observation')
        if max(self.horizon_tick, self.valid_until_tick) > self.as_of_tick + max_horizon:
            raise ValueError('Belief exceeds permitted horizon')
        if not set(self.evidence_ids) <= observation.evidence_ids:
            raise ValueError('Belief cites invisible evidence')
        return self
