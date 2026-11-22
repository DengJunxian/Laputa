from typing import Annotated, Literal

from pydantic import Field, model_validator

from civitas.contracts.base import Id, NonNegative, Positive, Record, require_unique

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]


class CommunicationEdge(Record):
    kind: Literal["communication"] = "communication"
    source_id: Id
    target_id: Id
    cost: Annotated[float, Field(ge=0.0)] = 0.0
    visibility: Literal["endpoints"] = "endpoints"


class FactTrust(Record):
    kind: Literal["fact_trust"] = "fact_trust"
    source_id: Id
    target_id: Id
    a0: PositiveFloat = 1.0
    b0: PositiveFloat = 1.0
    a: PositiveFloat = 1.0
    b: PositiveFloat = 1.0
    used_roots: list[Id] = Field(default_factory=list)
    visibility: Literal["source_private"] = "source_private"

    @model_validator(mode="after")
    def counts(self):
        require_unique(self.used_roots, "fact evidence root")
        if self.a < self.a0 or self.b < self.b0:
            raise ValueError("Trust counts cannot fall below priors")
        return self


class PredictionTrust(Record):
    kind: Literal["prediction_trust"] = "prediction_trust"
    source_id: Id
    target_id: Id
    brier_ewma: Probability = 0.25
    samples: NonNegative = 0
    used_roots: list[Id] = Field(default_factory=list)
    visibility: Literal["source_private"] = "source_private"

    @model_validator(mode="after")
    def roots(self):
        require_unique(self.used_roots, "prediction root")
        if self.samples != len(self.used_roots):
            raise ValueError("Prediction count disagrees with roots")
        return self


class TrustEvidence(Record):
    evidence_id: Id
    root_claim_id: Id
    author_id: Id
    observer_id: Id
    kind: Literal["fact", "prediction"]
    outcome: Annotated[int, Field(ge=0, le=1)]
    probability: Probability | None = None
    matured_tick: NonNegative
    visible_tick: NonNegative
    visible_to: list[Id]
    origin: Literal["synthetic_test", "public_resolution"]

    @model_validator(mode="after")
    def forecast(self):
        require_unique(self.visible_to, "evidence audience")
        if (self.kind == "prediction") != (self.probability is not None):
            raise ValueError("Only prediction evidence carries a forecast probability")
        return self


class Group(Record):
    group_id: Id
    public: bool = False


class Membership(Record):
    kind: Literal["membership"] = "membership"
    source_id: Id
    target_id: Id
    role: Literal["member", "moderator"] = "member"
    visibility: Literal["public", "members", "private"] = "private"


class RewiringConfig(Record):
    enabled: bool = False
    max_degree: Positive = 32
    max_changes: NonNegative = 1
    cooldown: NonNegative = 3
    period: Positive = 1
    exploration_candidates: NonNegative = 1
    intercept: float = 0.0
    trust_coefficient: float = 1.0
    similarity_coefficient: float = 1.0
    cost_coefficient: Annotated[float, Field(ge=0.0)] = 1.0
    exploration_bonus: float = 0.0
    new_edge_cost: Annotated[float, Field(ge=0.0)] = 0.0


class RelationsConfig(Record):
    schema_version: Literal[1] = 1
    mechanism_id: Literal["typed_relations_v1"] = "typed_relations_v1"
    attention_budget: NonNegative = 4
    trust_enabled: bool = True
    fact_decay: Probability = 1.0
    prediction_alpha: Probability = 0.2
    rewiring: RewiringConfig = Field(default_factory=RewiringConfig)


class PerceivedSimilarity(Record):
    observer_id: Id
    target_id: Id
    value: Probability
    available_tick: NonNegative
    origin: Literal["public_disclosure", "perceived"]
    evidence_refs: list[Id]


class TypedMessageContent(Record):
    schema_version: Literal[1] = 1
    root_claim_id: Id
    author_id: Id
    audience: list[Id] = Field(min_length=1)
    parent_message_id: Id | None = None
    content_ref: Id
    claim_kind: Literal["fact", "prediction"] = "fact"
    signal: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.0
    probability: Probability | None = None

    @model_validator(mode="after")
    def semantics(self):
        require_unique(self.audience, "message audience")
        if (self.claim_kind == "prediction") != (self.probability is not None):
            raise ValueError("A prediction message requires its original probability")
        return self


class ExposureEvent(Record):
    exposure_id: Id
    message_id: Id
    root_claim_id: Id
    actor_id: Id
    author_id: Id
    sender_id: Id
    tick: NonNegative
    rank: NonNegative
    weight: Probability
    claim_kind: Literal["fact", "prediction"]
    visibility: Literal["actor_private"] = "actor_private"


class ScoredClaim(Record):
    observer_id: Id
    author_id: Id
    root_claim_id: Id
    kind: Literal["fact", "prediction"]
    tick: NonNegative


class RewireTrace(Record):
    observer_id: Id
    candidate_id: Id
    tick: NonNegative
    origin: Literal["existing", "introduction", "public_group", "exploration"]
    probability: Probability
    draw: Probability
    selected: bool
    operation: Literal["upsert", "delete"]


class EdgeDelta(Record):
    delta_id: Id
    proposed_tick: NonNegative
    effective_tick: Positive
    layer: Literal["communication", "fact_trust", "prediction_trust", "membership"]
    source_id: Id
    target_id: Id
    operation: Literal["upsert", "delete"]
    value: CommunicationEdge | FactTrust | PredictionTrust | Membership | None = None

    @model_validator(mode="after")
    def identity(self):
        if self.effective_tick <= self.proposed_tick:
            raise ValueError("Relation changes require t+1 or later")
        if self.layer != "membership" and self.source_id == self.target_id:
            raise ValueError("Self relation is not supported")
        if self.operation == "delete":
            if self.value is not None:
                raise ValueError("Deletion carries a key only")
        elif self.value is None or (self.value.kind, self.value.source_id, self.value.target_id) != (
                self.layer, self.source_id, self.target_id):
            raise ValueError("Delta value disagrees with its typed key")
        return self


class RelationState(Record):
    state_schema_version: Literal[1] = 1
    actors: list[Id]
    tick: NonNegative = 0
    config: RelationsConfig = Field(default_factory=RelationsConfig)
    groups: list[Group] = Field(default_factory=list)
    memberships: list[Membership] = Field(default_factory=list)
    exposures: list[ExposureEvent] = Field(default_factory=list)
    evidence: list[TrustEvidence] = Field(default_factory=list)
    similarities: list[PerceivedSimilarity] = Field(default_factory=list)
    feedback_tick: NonNegative = 0
    scored_claims: list[ScoredClaim] = Field(default_factory=list)
    rewiring_trace: list[RewireTrace] = Field(default_factory=list)
    communication: list[CommunicationEdge] = Field(default_factory=list)
    fact_trust: list[FactTrust] = Field(default_factory=list)
    prediction_trust: list[PredictionTrust] = Field(default_factory=list)
    pending: list[EdgeDelta] = Field(default_factory=list)
    applied: list[EdgeDelta] = Field(default_factory=list)

    @model_validator(mode="after")
    def integrity(self):
        require_unique(self.actors, "actor")
        require_unique([g.group_id for g in self.groups], "group")
        groups = {g.group_id for g in self.groups}
        require_unique([(m.source_id, m.target_id) for m in self.memberships], "membership")
        if any(m.source_id not in self.actors or m.target_id not in groups for m in self.memberships):
            raise ValueError("Unknown membership actor/group")
        for collection in (self.communication, self.fact_trust, self.prediction_trust):
            require_unique([(e.source_id, e.target_id) for e in collection], "typed edge")
            for edge in collection:
                if edge.source_id not in self.actors or edge.target_id not in self.actors:
                    raise ValueError("Unknown relation actor")
                if edge.source_id == edge.target_id:
                    raise ValueError("Self relation is not supported")
        require_unique([d.delta_id for d in self.pending + self.applied], "delta ID")
        require_unique([(d.effective_tick, d.layer, d.source_id, d.target_id) for d in self.pending],
                       "pending write at the same boundary")
        for delta in self.pending + self.applied:
            targets = groups if delta.layer == "membership" else self.actors
            if delta.source_id not in self.actors or delta.target_id not in targets:
                raise ValueError("Unknown delta actor")
        if any(d.effective_tick <= self.tick for d in self.pending) or any(d.effective_tick > self.tick for d in self.applied):
            raise ValueError("Relation delta chronology mismatch")
        if self.feedback_tick > self.tick:
            raise ValueError("Relation feedback is in the future")
        require_unique([e.exposure_id for e in self.exposures], "exposure ID")
        require_unique([(e.actor_id, e.message_id) for e in self.exposures], "message exposure")
        require_unique([e.evidence_id for e in self.evidence], "trust evidence ID")
        require_unique([(s.observer_id, s.target_id) for s in self.similarities], "perceived similarity")
        for exposure in self.exposures:
            if not {exposure.actor_id, exposure.author_id, exposure.sender_id} <= set(self.actors) or exposure.tick > self.tick:
                raise ValueError("Unknown exposure actor or future exposure")
        for e in self.evidence:
            if not {e.author_id, e.observer_id, *e.visible_to} <= set(self.actors):
                raise ValueError("Unknown trust-evidence actor")
        if any(s.observer_id not in self.actors or s.target_id not in self.actors for s in self.similarities):
            raise ValueError("Unknown similarity actor")
        require_unique([(s.observer_id, s.root_claim_id, s.kind) for s in self.scored_claims], "scored root claim")
        for claim in self.scored_claims:
            if claim.observer_id not in self.actors or claim.author_id not in self.actors or claim.tick > self.tick:
                raise ValueError("Unknown scored claim actor or future score")
        require_unique([(r.tick, r.observer_id, r.candidate_id, r.operation) for r in self.rewiring_trace], "rewiring trace")
        for trace in self.rewiring_trace:
            if trace.observer_id not in self.actors or trace.candidate_id not in self.actors or trace.tick > self.tick:
                raise ValueError("Unknown rewiring actor or future trace")
        if self.config.rewiring.enabled:
            counts = {}
            for edge in self.communication:
                counts[edge.target_id] = counts.get(edge.target_id, 0) + 1
            if any(n > self.config.rewiring.max_degree for n in counts.values()):
                raise ValueError("Communication degree exceeds configured cap")
        return self
