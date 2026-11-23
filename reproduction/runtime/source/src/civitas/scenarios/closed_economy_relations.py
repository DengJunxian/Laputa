"""Receipt-backed fulfillment learning on the existing observation boundary.

This is a deliberately narrow economic-social bridge, not the proposed general
CN03 implementation. Exposure, actual use, and sharing have separate records.
All writes are a declared FEEDBACK observation effect, effective at t+1.
"""
from collections import defaultdict
from itertools import chain
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from core.experiments.strict_types import SealedJSONObjectV1, SealedJSONRowsV1

from civitas.contracts.base import Id, Positive, Record, require_unique, retained_record
from civitas.contracts.config import MechanismSpec
from civitas.contracts.economic import LaborAcceptIntent
from civitas.contracts.observations import ObservationFact
from civitas.contracts.time import SimTime
from civitas.core.observation import ObservationBuilder
from civitas.relations.store import RelationStore
from civitas.relations.types import CommunicationEdge, RelationState
from .closed_economy_config import Bps
from .closed_economy_introductions import CandidateIntroduction, INTRODUCTION_KIND, introduction_from
from .closed_economy_labor_outcomes import (LABOR_ASSESSMENT, LABOR_OUTCOME_KIND, LABOR_OUTCOME_PREFIX, REHIRE_OFFER, SWITCH_OFFER,
                                           WagePerformanceTrust, labor_assessments)
from .closed_economy_links import active_link_changes, schedule_links
from .closed_economy_demand import KIND as DEMAND_KIND, consumption_message_id, consumption_source, demand_comparison
from .closed_economy_partner import (
    PARTNER_TRUST, PartnerPerformanceTrust, partner_offer, schedule_partner_exchange, update_partner_performance,
)

SOCIAL_FIELD = "closed_social"
EXPERIENCE_KIND = "verified_delivery_experience"
DELIVERY_OUTCOMES = frozenset({"purchased", "delivery_failed_refunded"})


class SocialActorState(Record):
    scores_bps: dict[Id, Bps]
    consumed_roots: list[Id] = Field(default_factory=list)
    exposed_messages: list[Id] = Field(default_factory=list)
    shared_roots: list[Id] = Field(default_factory=list)
    introduced_roots: list[Id] = Field(default_factory=list, exclude_if=lambda value: not value)
    wage_performance_trust: dict[Id, WagePerformanceTrust] = Field(
        default_factory=dict, exclude_if=lambda value: not value)
    partner_performance_trust: dict[Id, PartnerPerformanceTrust] = Field(
        default_factory=dict, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def unique_records(self):
        for field in (self.consumed_roots, self.exposed_messages, self.shared_roots, self.introduced_roots):
            require_unique(field, "social evidence record")
        return self


class SocialPatch(Record):
    actor_id: Id
    effective_tick: Positive
    state: retained_record(SocialActorState)


class ClosedSocialState(Record):
    schema_version: Literal[1] = 1
    households: list[Id]
    sellers: list[Id]
    neighbors: dict[Id, list[Id]] | None = Field(default=None, exclude_if=lambda value: value is None)
    network: retained_record(RelationState) | None = Field(default=None, exclude_if=lambda value: value is None)
    current: dict[Id, retained_record(SocialActorState)]
    pending: list[retained_record(SocialPatch)] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("trace", mode="wrap")
    @classmethod
    def seal_trace(cls, value, handler):
        return value if type(value) is SealedJSONRowsV1 else SealedJSONRowsV1(None, handler(value))

    def working_dump(self):
        return {**self.model_dump(exclude={"trace"}), "trace": self.trace}

    def updated(self, **changes):
        fields = {name: getattr(self, name) for name in type(self).model_fields}
        return type(self).model_validate({**fields, **changes})

    @model_validator(mode="after")
    def membership(self):
        require_unique(self.households, "household")
        require_unique(self.sellers, "seller")
        households = set(self.households)
        sellers = set(self.sellers)
        if (self.neighbors is None) == (self.network is None):
            raise ValueError("Communication requires one graph authority")
        topology_actors = self.neighbors if self.neighbors is not None else self.network.actors
        if set(self.current) != households or set(topology_actors) != households:
            raise ValueError("Social actor membership changed")
        for actor, neighbors in (self.neighbors or {}).items():
            require_unique(neighbors, "neighbor")
            if actor in neighbors or not set(neighbors) <= households:
                raise ValueError("Invalid social neighbor")
        for state in chain(self.current.values(), (p.state for p in self.pending)):
            if state.scores_bps.keys() != sellers:
                raise ValueError("Unknown supplier score")
            if not state.wage_performance_trust.keys() <= sellers:
                raise ValueError("Unknown employer in wage performance trust")
            if not state.partner_performance_trust.keys() <= households:
                raise ValueError("Unknown candidate in partner performance trust")
        if any(p.actor_id not in self.current for p in self.pending):
            raise ValueError("Unknown pending social actor")
        require_unique([f"{p.effective_tick}:{p.actor_id}" for p in self.pending],
                       "pending social actor/tick")
        return self

    def outgoing_contacts(self):
        if self.network is None:
            return self.neighbors
        store = RelationStore(self.network)
        return {actor: store.neighbors(actor) for actor in self.households}


def initial_social(config, households, sellers):
    neighbors = {actor: config.household_contacts(i) for i, actor in enumerate(households)}
    priors = {actor: dict.fromkeys(sellers, 10000) for actor in households}
    if config.population_input is not None:
        for actor, row in zip(households, config.population_input.household_profiles, strict=True):
            if row.behavior is not None:
                priors[actor] = dict(row.behavior.supplier_priors_bps)
    network = None
    if config.link_budget_per_tick is not None:
        network = RelationState(actors=households, communication=[
            CommunicationEdge(source_id=source, target_id=target)
            for source, targets in neighbors.items() for target in targets])
        neighbors = None
    return ClosedSocialState(households=households, sellers=sellers,
        neighbors=neighbors, network=network,
        current={actor: SocialActorState(scores_bps=priors[actor]) for actor in households})


def materialize_social(raw, tick):
    if type(raw) is ClosedSocialWire and type(tick) is int and raw.tick == tick:
        return raw.effective
    return _advance_social(validate_social(raw), tick)


def validate_social(raw):
    return raw.state if type(raw) is ClosedSocialWire else ClosedSocialState.model_validate(raw)


def _advance_social(state, tick):
    current = dict(state.current)
    remaining = []
    for patch in sorted(state.pending, key=lambda p: (p.effective_tick, p.actor_id)):
        if patch.effective_tick <= tick:
            current[patch.actor_id] = patch.state
        else:
            remaining.append(patch)
    network = RelationStore(state.network).advance(tick) if state.network is not None else None
    return state.updated(current=current, pending=remaining, network=network)


class ClosedSocialWire(SealedJSONObjectV1):
    __slots__ = ("state", "tick", "effective")

    def __new__(cls, raw, *, tick=None):
        if type(raw) is ClosedSocialState:
            state, wire = raw, raw.working_dump()
        else:
            state, wire = validate_social(raw), raw if type(raw) is cls else dict(raw)
        value = super().__new__(cls, wire)
        object.__setattr__(value, "state", state)
        object.__setattr__(value, "tick", tick)
        # This shell belongs to one transaction and keeps the same wire values.
        object.__setattr__(value, "effective", _advance_social(state, tick) if tick is not None else None)
        return value


def outcome_root(command_id):
    # Stable factual target: this exact accepted delivery, not a rolling forecast.
    return "delivery-outcome:" + command_id


def delivery_disclosure(command_id: str, seller_id: str, outcome: str, *,
                        allow_introduction=False, allow_follow=False) -> dict:
    body = {"kind": EXPERIENCE_KIND, "root_claim_id": outcome_root(command_id),
            "receipt_command_id": command_id, "seller_id": seller_id, "outcome": outcome}
    if allow_introduction and outcome == "purchased":
        body["allow_introduction"] = True
        if allow_follow:
            body["allow_follow"] = True
    return body


def _disclosed_receipt(body, receipts_by_command, sender_id, created_tick, *,
                       introduction_enabled=False, links_enabled=False):
    allows = "allow_introduction" in body
    if allows and (not introduction_enabled or body["allow_introduction"] is not True):
        return None
    follows = "allow_follow" in body
    if follows and (not links_enabled or not allows or body["allow_follow"] is not True):
        return None
    command_id = body.get("receipt_command_id")
    receipt = receipts_by_command.get(command_id) if isinstance(command_id, str) else None
    if (receipt is not None and receipt.actor_id == sender_id and receipt.tick < created_tick
            and receipt.outcome in DELIVERY_OUTCOMES
            and body == delivery_disclosure(command_id, receipt.counterparty_id, receipt.outcome,
                                            allow_introduction=allows, allow_follow=follows)):
        return receipt
    return None


def _verify_introduction(body, sender, recipient, tick, sources, neighbors, receipts, *, links_enabled,
                         opened_at):
    try:
        candidate = CandidateIntroduction.model_validate(body)
    except ValueError:
        return None
    source = sources.get(candidate.source_message_id)
    if (source is None or source.recipient_id != sender
            or recipient not in neighbors.get(sender, ())
            or sender not in neighbors.get(source.sender_id, ())
            or source.created_at.tick < opened_at.get((source.sender_id, sender), 0)
            or source.created_at.phase != "FEEDBACK" or source.created_at.tick + 1 != tick
            or (source.expires_at is not None and source.expires_at.key <= source.visible_at.key)
            or _disclosed_receipt(source.body, receipts, source.sender_id, source.created_at.tick,
                                  introduction_enabled=True, links_enabled=links_enabled) is None):
        return None
    return candidate if candidate == introduction_from(source, recipient, tick) else None


def update_score(score, success, learning_bps):
    target = 10000 if success else 0
    # Nonnegative weighted-average arithmetic avoids signed floor asymmetry.
    return (score * (10000 - learning_bps) + target * learning_bps) // 10000


def social_context(observation):
    return next((f for f in observation.market_view
                 if f.value.get("kind") == "closed_social_context"), None)


def assess_rehire(observation, employer, minimum_bps):
    own = social_context(observation)
    score = own.value.get("wage_performance_trust", {}).get(employer) if own is not None else None
    below = (score is not None and minimum_bps is not None
             and score["a"] * 10000 < minimum_bps * (score["a"] + score["b"]))
    return score, below


def appraise_social(observation, config, selected_roots=None):
    fact = social_context(observation)
    if fact is None:
        return {}, []
    scores = dict(fact.value["scores_bps"])
    seen = set(fact.value["consumed_roots"])
    used = []
    rate = config.trust_learning_bps * config.peer_weight_bps // 10000
    if config.social_mode != "adaptive" or rate == 0:
        return scores, used
    for message in sorted(observation.delivered_messages, key=lambda m: m.message_id):
        body = message.body
        root = body.get("root_claim_id")
        if (body.get("kind") != EXPERIENCE_KIND or root in seen
                or body.get("seller_id") not in scores
                or (selected_roots is not None and root not in selected_roots)):
            continue
        seller = body["seller_id"]
        old = scores[seller]
        scores[seller] = update_score(old, body["outcome"] == "purchased", rate)
        seen.add(root)
        used.append({"root_claim_id": root, "message_id": message.message_id,
                     "seller_id": seller, "old_score_bps": old,
                     "new_score_bps": scores[seller]})
    return scores, used


class ClosedEconomyObserver(ObservationBuilder):
    observation_data_fields = frozenset({SOCIAL_FIELD})
    observation_history_fields = ("messages", "memories", "receipts")
    effect_spec = MechanismSpec(mechanism_id="closed_fulfillment_feedback",
        version="1", phase="FEEDBACK", reads=[SOCIAL_FIELD], writes=[SOCIAL_FIELD])

    def __init__(self, config, *, available_link_cash=None):
        self.config = config
        if (config.link_fee_minor or config.partner_choice) and available_link_cash is None:
            raise ValueError("Fee admission requires the economic book's available-cash reader")
        self.available_link_cash = available_link_cash
        self._observation_view = None

    def observe_batch(self, snapshot, project):
        state, neighbors = self._social_view(snapshot)
        messages, memories = defaultdict(list), defaultdict(list)
        public = []
        for message in snapshot.messages:
            messages[message.recipient_id].append(message)
        for fact in snapshot.memories:
            if fact.category == "market" and fact.public:
                public.append(fact)
            else:
                memories[fact.owner_id].append(fact)
        receipts = {r.command_id: r for r in snapshot.receipts} if messages else {}
        sources = {m.message_id: m for m in snapshot.messages} if self.config.introductions else {}
        self._observation_view = snapshot, state, neighbors, messages, memories, public, receipts, sources
        try:
            return [self.observe(actor, snapshot, project(actor)) for actor in snapshot.actors]
        finally:
            self._observation_view = None

    def _social_view(self, snapshot):
        if self._observation_view is not None and self._observation_view[0] is snapshot:
            return self._observation_view[1:3]
        state = materialize_social(snapshot.data[SOCIAL_FIELD], snapshot.tick)
        return state, state.outgoing_contacts()

    def message_candidates(self, actor_id, snapshot):
        if self._observation_view is not None and self._observation_view[0] is snapshot:
            return self._observation_view[3].get(actor_id, ())
        return super().message_candidates(actor_id, snapshot)

    def select_memories(self, actor_id, snapshot):
        if self._observation_view is not None and self._observation_view[0] is snapshot:
            memories, public = self._observation_view[4:6]
            return [*memories.get(actor_id, ()), *public]
        return super().select_memories(actor_id, snapshot)

    def select_messages(self, actor_id, snapshot, messages):
        if not messages or self.config.social_mode == "none":
            return []
        state, neighbors = self._social_view(snapshot)
        if actor_id not in state.current:
            return []
        opened_at = ({(d.source_id, d.target_id): d.effective_tick for d in active_link_changes(state.network)}
                     if self.config.link_withdrawal_tick is not None else {})
        exposed = set(state.current[actor_id].exposed_messages)
        # Verification grants only the exact subset explicitly disclosed by its
        # receipt owner; it never inserts undisclosed balances or hidden states.
        view = self._observation_view
        batch = view is not None and view[0] is snapshot
        receipts = view[6] if batch else {r.command_id: r for r in snapshot.receipts}
        eligible = []
        offered = []
        for message in messages:
            if (message.message_id in exposed
                    or actor_id not in neighbors.get(message.sender_id, ())
                    or message.created_at.tick < opened_at.get((message.sender_id, actor_id), 0)):
                continue
            if self.config.introductions and message.body.get("kind") == INTRODUCTION_KIND:
                offered.append(message)
            elif (self.config.consumption_peer_uplift is not None and message.body.get("kind") == DEMAND_KIND
                  and message.created_at.tick == snapshot.tick-1
                  and consumption_source(message.body, receipts, message.sender_id, message.created_at.tick) is not None
                  and message.message_id == consumption_message_id(message.sender_id, actor_id,
                                                                   message.body["root_claim_id"])):
                eligible.append(message)
            elif _disclosed_receipt(message.body, receipts, message.sender_id, message.created_at.tick,
                                    introduction_enabled=self.config.introductions,
                                    links_enabled=state.network is not None) is not None:
                eligible.append(message)
        selected = sorted(eligible, key=lambda m: (m.visible_at.key, m.message_id))[:self.config.attention_limit]
        if not offered or len(selected) == self.config.attention_limit:
            return selected
        sources = view[7] if batch else {m.message_id: m for m in snapshot.messages}
        known_contacts = set(neighbors[actor_id])
        known_contacts.update(m.sender_id for m in selected)
        known_contacts.update(sources[key].sender_id for key in exposed if key in sources
                              and sources[key].body.get("kind") == EXPERIENCE_KIND
                              and (self.config.link_withdrawal_tick is None
                                   or actor_id in neighbors.get(sources[key].sender_id, ())))
        roots = set(state.current[actor_id].introduced_roots)
        for message in sorted(offered, key=lambda m: (m.visible_at.key, m.message_id)):
            candidate = _verify_introduction(message.body, message.sender_id, actor_id,
                message.created_at.tick, sources, neighbors, receipts,
                links_enabled=state.network is not None, opened_at=opened_at)
            if (candidate is None or message.created_at.phase != "FEEDBACK"
                    or message.visible_at.phase != "OBSERVE" or message.visible_at.tick != candidate.issued_tick + 1
                    or snapshot.tick >= candidate.expires_tick or message.message_id != candidate.message_id
                    or candidate.candidate_id in known_contacts
                    or candidate.root_claim_id in roots
                    or candidate.source_message_id not in state.current[message.sender_id].exposed_messages):
                continue
            selected.append(message)
            roots.add(candidate.root_claim_id)
            if len(selected) == self.config.attention_limit:
                break
        return selected

    def observe(self, actor_id, snapshot, candidates):
        state, neighbors = self._social_view(snapshot)
        if actor_id in state.current:
            own = state.current[actor_id]
            withdrawable = {}
            if self.config.link_withdrawal_tick is not None:
                withdrawable["withdrawable_links"] = [
                    {"edge_delta_id": d.delta_id, "source_id": d.source_id}
                    for d in active_link_changes(state.network) if d.target_id == actor_id]
            fee_terms = ({"link_fee_minor": self.config.link_fee_minor, "link_fee_recipient_id": state.sellers[0]}
                         if self.config.link_fee_minor else {})
            fact = ObservationFact(fact_id=f"closed-social:{snapshot.tick}:v{snapshot.world_version}:{actor_id}",
                category="market", owner_id=actor_id,
                visible_at={"tick": snapshot.tick, "phase": "OBSERVE"},
                value={"kind": "closed_social_context", **own.model_dump(exclude={PARTNER_TRUST}),
                       "neighbors": list(neighbors[actor_id]),
                       "mode": self.config.social_mode, **withdrawable, **fee_terms})
            candidates = [*candidates, fact]
            facts = ()
            if (self.config.partner_choice and (self.config.cooperation.comparison_trial is not None
                    or self.config.cooperation.trust_ranking is not None)
                    and snapshot.tick == self.config.cooperation.followup.offer_tick - 1
                    and actor_id == state.households[self.config.cooperation.household_indices[0]]):
                now = SimTime(tick=snapshot.tick, phase="OBSERVE")
                facts = [f for f in self.select_memories(actor_id, snapshot)
                         if f.owner_id == actor_id and f.visible_at.key <= now.key]
            option = partner_offer(self.config, state.network, actor_id, snapshot.tick, facts=facts,
                                   sponsor=state.sellers[0], available=self.available_link_cash)
            if option is not None:
                candidates.append(option)
                if self.config.cooperation.trust_ranking and self.config.cooperation.trust_ranking.enabled:
                    candidates.append(ObservationFact(
                        fact_id=f"partner-trust:{snapshot.tick}:{actor_id}", category="market", owner_id=actor_id,
                        visible_at=option.visible_at,
                        value={"kind": PARTNER_TRUST, "scores": {
                            a: counts.model_dump() for a, counts in own.partner_performance_trust.items()}}))
        return super().observe(actor_id, snapshot, candidates)

    def prepare_commit(self, snapshot, observations):
        state = materialize_social(snapshot.data[SOCIAL_FIELD], snapshot.tick)
        neighbors = state.outgoing_contacts()
        opened_at = ({(d.source_id, d.target_id): d.effective_tick for d in active_link_changes(state.network)}
                     if self.config.link_withdrawal_tick is not None else {})
        bundles = {b.actor_id: b for b in snapshot.decisions if b.tick == snapshot.tick}
        receipts = {r.command_id: r for r in snapshot.receipts}
        current_receipts = [r for r in snapshot.receipts if r.tick == snapshot.tick]
        trace = []
        if state.network is not None:
            for delta in state.network.applied:
                if delta.effective_tick != snapshot.tick:
                    continue
                row = {"kind": ("communication_edge_withdrawn" if delta.operation == "delete"
                               else "communication_edge_active"), "tick": snapshot.tick,
                       "delta_id": delta.delta_id, "source_id": delta.source_id, "target_id": delta.target_id}
                if self.config.link_fee_minor and delta.operation == "upsert":
                    row.update(fee_status="settled", fee_tx_id=f"econ:link-fee:{delta.delta_id}")
                trace.append(row)
        patches = list(state.pending)
        transfer = self.config.labor_transfer
        labor_enabled = transfer and transfer.outcome_evidence
        performance_enabled = labor_enabled and transfer.performance_trust
        rehire = transfer.rehire_at(snapshot.tick) if transfer is not None else None
        peer_uplift = self.config.consumption_peer_uplift
        if labor_enabled:
            labor_consumed = {(row["actor_id"], row["target_id"], row["root_claim_id"])
                              for row in chain(state.trace, trace) if row["kind"] == "labor_outcome_consumption"}
        for observation in observations:
            actor = observation.actor_id
            bundle = bundles[actor]
            if transfer is not None and actor == state.households[transfer.worker_index]:
                if transfer.second_rehire is not None and snapshot.tick == transfer.offer_tick:
                    self._record_rehire(observation, bundle, None, receipts, trace, history=state.trace, kinds=("labor_offer",),
                                        candidate_id=state.sellers[transfer.employer_index], trace_kind="labor_initial_choice")
                if rehire is not None and rehire.alternative_employer_index is None:
                    self._record_rehire(observation, bundle, rehire.minimum_trust_bps, receipts, trace, history=state.trace)
                elif rehire is not None:
                    for index in (transfer.employer_index, *rehire.alternative_indices):
                        self._record_rehire(observation, bundle, rehire.minimum_trust_bps, receipts, trace,
                                            history=state.trace, kinds=(REHIRE_OFFER, SWITCH_OFFER),
                                            candidate_id=state.sellers[index])
            old = state.current.get(actor)
            wage_trust = dict(old.wage_performance_trust) if old else {}
            shared = set()
            visible_sources = None
            for draft in bundle.message_drafts:
                if labor_enabled and (draft.body.get("kind") == LABOR_OUTCOME_KIND
                        or (isinstance(draft.body.get("root_claim_id"), str)
                            and draft.body["root_claim_id"].startswith(LABOR_OUTCOME_PREFIX))):
                    raise ValueError("Labor contract outcomes are private to the contract parties")
                if draft.body.get("kind") == INTRODUCTION_KIND:
                    if not self.config.introductions or self.config.social_mode == "none":
                        raise ValueError("Introductions are disabled")
                    if visible_sources is None:
                        visible_sources = {m.message_id: m for m in observation.delivered_messages
                                           if m.message_id in bundle.evidence_ids}
                    candidate = _verify_introduction(draft.body, actor, draft.recipient_id, snapshot.tick,
                        visible_sources, neighbors, receipts,
                        links_enabled=state.network is not None, opened_at=opened_at)
                    if candidate is None or draft.message_id != candidate.message_id:
                        raise ValueError("Introduction lacks an authorized, visible two-hop path")
                    trace.append({**candidate.model_dump(), "kind": "introduction_issued",
                        "tick": snapshot.tick, "message_id": draft.message_id})
                    continue
                if draft.body.get("kind") == DEMAND_KIND:
                    source = consumption_source(draft.body, receipts, actor, snapshot.tick)
                    if (peer_uplift is None or source is None or draft.recipient_id not in neighbors.get(actor, ())
                            or "memory:" + source.command_id not in bundle.evidence_ids
                            or draft.message_id != consumption_message_id(actor, draft.recipient_id,
                                                                           draft.body["root_claim_id"])):
                        raise ValueError("Unauthorized consumption disclosure")
                    continue
                if draft.body.get("kind") != EXPERIENCE_KIND:
                    continue
                receipt = _disclosed_receipt(draft.body, receipts, actor, snapshot.tick,
                    introduction_enabled=self.config.introductions, links_enabled=state.network is not None)
                if (self.config.social_mode == "none" or receipt is None
                        or draft.recipient_id not in neighbors.get(actor, ())
                        or "memory:" + receipt.command_id not in bundle.evidence_ids):
                    raise ValueError("Unauthorized fulfillment disclosure")
                shared.add(draft.body["root_claim_id"])
            if labor_enabled and LABOR_ASSESSMENT in bundle.belief_patch:
                candidates = labor_assessments(observation)
                # Archived facts stay readable. Only fresh earned outcomes count toward this axis.
                fresh_wages = {f.fact_id: f.value for f in observation.private_memory_refs
                    if f.visible_at == observation.visible_until and f.value.get("kind") == LABOR_OUTCOME_KIND
                    and f.value["worker_id"] == actor and f.value["outcome"] in ("paid", "arrears")
                } if performance_enabled and old else {}
                assessments = bundle.belief_patch[LABOR_ASSESSMENT]
                if not isinstance(assessments, list):
                    raise ValueError("Labor outcome assessments must be a list")
                for assessment in assessments:
                    fact_id = assessment.get("fact_id") if isinstance(assessment, dict) else None
                    if (not isinstance(fact_id, str) or fact_id not in bundle.evidence_ids
                            or assessment != candidates.get(fact_id)):
                        raise ValueError("Labor outcome assessment lacks matching visible evidence")
                    key = (actor, assessment["target_id"], assessment["root_claim_id"])
                    if key not in labor_consumed:
                        labor_consumed.add(key)
                        trace.append({"kind": "labor_outcome_consumption", "tick": snapshot.tick,
                            "actor_id": actor, "decision_id": bundle.decision_id, **assessment})
                        if result := fresh_wages.get(fact_id):
                            employer = result["employer_id"]
                            before = wage_trust.get(employer, WagePerformanceTrust())
                            after = WagePerformanceTrust(**before.counted(result["outcome"] == "paid"))
                            wage_trust[employer] = after
                            trace.append({"kind": "wage_trust_update", "tick": snapshot.tick,
                                "effective_tick": snapshot.tick + 1, "actor_id": actor,
                                "employer_id": employer, "decision_id": bundle.decision_id, **assessment,
                                "ledger_tx_ids": list(result["ledger_tx_ids"]),
                                "before": before.model_dump(), "after": after.model_dump()})
            if old is None:
                continue
            partner_trust = old.partner_performance_trust
            if self.config.cooperation and self.config.cooperation.trust_ranking:
                partner_trust = update_partner_performance(self.config, state.households, actor, snapshot.tick,
                                                           snapshot.memories, partner_trust, trace)
            choice = bundle.belief_patch.get("closed_choice", {})
            demand_used = set()
            if peer_uplift is not None:
                expected = demand_comparison(observation, peer_uplift)
                profile = next(f for f in observation.market_view if f.value.get("kind") == "household_preferences")
                if expected["peer_uplift_units"]:
                    demand_used.update(expected["consumption_message_ids"])
                if (any(choice.get(key) != value for key,value in expected.items())
                        or choice.get("target_units") != profile.value["target_units"] + expected["peer_uplift_units"]
                        or not demand_used <= set(bundle.evidence_ids)):
                    raise ValueError("Demand comparison disagrees with the authorized observations")
            requested = list(choice.get("consumed_roots", []))
            require_unique(requested, "requested consumed root")
            scores, used = appraise_social(observation, self.config, set(requested))
            if set(requested) != {x["root_claim_id"] for x in used}:
                raise ValueError("Policy consumed an unavailable or already used root")
            used_messages = {x["message_id"] for x in used}
            if not used_messages <= set(bundle.evidence_ids):
                raise ValueError("Social consumption needs a cited message")
            if choice and dict(choice.get("effective_scores_bps", {})) != scores:
                raise ValueError("Reported supplier appraisal differs from witnessed evidence")
            consumed = set(old.consumed_roots)
            for item in used:
                consumed.add(item["root_claim_id"])
                trace.append({"kind": "peer_consumption", "tick": snapshot.tick,
                    "effective_tick": snapshot.tick + 1, "actor_id": actor,
                    "decision_id": bundle.decision_id, **item})
            for receipt in current_receipts:
                if (receipt.actor_id != actor or self.config.social_mode == "static"
                        or receipt.outcome not in DELIVERY_OUTCOMES):
                    continue
                root = outcome_root(receipt.command_id)
                if root in consumed:
                    continue
                seller = receipt.counterparty_id
                before = scores[seller]
                scores[seller] = update_score(before, receipt.outcome == "purchased",
                                             self.config.trust_learning_bps)
                consumed.add(root)
                trace.append({"kind": "personal_fulfillment", "tick": snapshot.tick,
                    "effective_tick": snapshot.tick + 1, "actor_id": actor,
                    "seller_id": seller, "root_claim_id": root,
                    "receipt_command_id": receipt.command_id,
                    "ledger_tx_ids": list(receipt.ledger_tx_ids),
                    "outcome": receipt.outcome, "old_score_bps": before,
                    "new_score_bps": scores[seller]})
            shared.update(old.shared_roots)
            exposed = set(old.exposed_messages)
            introduced = set(old.introduced_roots)
            for message in observation.delivered_messages:
                exposed.add(message.message_id)
                if message.body.get("kind") == INTRODUCTION_KIND:
                    introduced.add(message.body["root_claim_id"])
                    trace.append({**message.body, "kind": "introduction_opportunity",
                        "tick": snapshot.tick, "message_id": message.message_id})
                    continue
                trace.append({"kind": "exposure", "tick": snapshot.tick,
                    "actor_id": actor, "message_id": message.message_id,
                    "root_claim_id": message.body["root_claim_id"],
                    "consumed": message.message_id in used_messages or message.message_id in demand_used})
            trace.append({"kind": "decision", "tick": snapshot.tick,
                "actor_id": actor, "decision_id": bundle.decision_id,
                "observation_id": observation.observation_id,
                "evidence_ids": list(bundle.evidence_ids), **dict(choice)})
            patches.append(SocialPatch(actor_id=actor, effective_tick=snapshot.tick + 1,
                state=SocialActorState(scores_bps=scores,
                    consumed_roots=sorted(consumed), exposed_messages=sorted(exposed),
                    shared_roots=sorted(shared), introduced_roots=sorted(introduced),
                    wage_performance_trust=wage_trust, partner_performance_trust=partner_trust)))
        network = schedule_links(state.network, observations, bundles, self.config, trace,
                                 available_link_cash=self.available_link_cash)
        network = schedule_partner_exchange(self.config, network, observations, bundles, trace,
                                            self.available_link_cash, sponsor=state.sellers[0])
        result = state.updated(pending=patches, trace=SealedJSONRowsV1(state.trace, trace), network=network)
        return {SOCIAL_FIELD: ClosedSocialWire(result)}

    @staticmethod
    def _record_rehire(observation, bundle, minimum_bps, receipts, trace, *, history,
                       kinds=(REHIRE_OFFER,), candidate_id=None, trace_kind="rehire_choice"):
        offer = next((f for f in observation.market_view if f.value.get("kind") in kinds
                      and (candidate_id is None or f.value["employer_id"] == candidate_id)), None)
        row = {"kind": trace_kind, "tick": bundle.tick, "actor_id": bundle.actor_id,
               "decision_id": bundle.decision_id, "observation_id": observation.observation_id,
               "minimum_trust_bps": minimum_bps, "offer": None, "score": None,
               "score_roots": [], "status": "not_visible"}
        if candidate_id is not None:
            row["candidate_employer_id"] = candidate_id
        if offer is not None:
            employer = offer.value["employer_id"]
            score, below = assess_rehire(observation, employer, minimum_bps)
            attempt = next((intent for intent in bundle.economic_intents
                            if isinstance(intent, LaborAcceptIntent) and intent.offer_id == offer.fact_id), None)
            roots = [r["root_claim_id"] for r in chain(history, trace) if r["kind"] == "wage_trust_update"
                     and r["actor_id"] == bundle.actor_id and r["employer_id"] == employer
                     and r["effective_tick"] <= bundle.tick] if score is not None else []
            row.update(offer=dict(offer.value), score=score, score_roots=roots,
                       status="skipped_low_trust" if below and offer.fact_id in bundle.evidence_ids
                       else "not_attempted")
            if attempt is not None:
                if offer.fact_id not in bundle.evidence_ids:
                    raise ValueError("A rehire acceptance requires citing the visible offer")
                receipt = receipts["command:" + attempt.intent_id]
                row.update(status=receipt.status, command_id=receipt.command_id,
                           reason_codes=list(receipt.reason_codes))
                if offer.value["kind"] == SWITCH_OFFER:
                    row["max_switch_fee_minor"] = attempt.max_switch_fee_minor
            elif candidate_id is not None and row["status"] == "not_attempted" and any(
                    isinstance(i, LaborAcceptIntent) for i in bundle.economic_intents):
                row["status"] = "not_selected"
        trace.append(row)
