"""One institutionally authorized, degree-preserving communication exchange."""
from fractions import Fraction

from pydantic import Field, model_validator

from civitas.contracts.base import Id, require_unique
from civitas.contracts.economic import CooperationPartnerProposal
from civitas.contracts.observations import ObservationFact
from civitas.mechanisms.games import COOPERATION_MAX_SUBSIDY_MINOR, COOPERATION_STAKE_MINOR
from civitas.relations.store import RelationStore
from civitas.relations.types import CommunicationEdge, EdgeDelta

from .closed_economy_cooperation import OUTCOME_PREFIX, previous_partner_result, trial_partner_result
from .closed_economy_labor_outcomes import BinaryPerformanceCounts

PARTNER_OFFER = "cooperation_partner_offer"
PARTNER_TRACE = "cooperation_partner_choice"
PARTNER_DRAW = "cooperation_partner_draw"
PARTNER_TRUST = "partner_performance_trust"
PARTNER_UPDATE = "partner_performance_update"


class PartnerPerformanceTrust(BinaryPerformanceCounts):
    used_roots: list[Id] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def accounts_for_roots(self):
        require_unique(self.used_roots, "partner outcome root")
        if self.a + self.b - 2 != len(self.used_roots):
            raise ValueError("Partner counts must match distinct settled roots")
        return self

    def consume(self, root, success):
        if root in self.used_roots:
            return self
        return PartnerPerformanceTrust(**self.counted(success), used_roots=[*self.used_roots, root])


def update_partner_performance(config, households, actor, tick, facts, current, trace):
    ranking = config.cooperation.trust_ranking
    if not ranking.enabled or actor != households[config.cooperation.household_indices[0]]:
        return current
    trial = next((t for t in ranking.scheduled_trials if t.offer_tick + 1 == tick), None)
    if trial is None:
        return current
    candidate = households[trial.alternate_index]
    result = previous_partner_result(
        [f for f in facts if f.owner_id == actor and f.visible_at.tick == tick + 1 and not f.public],
        trial.offer_tick, candidate)
    if result is None:
        return current
    root, action = result.value["root_claim_id"], result.value["partner_action"]
    if (root != OUTCOME_PREFIX + result.value["ledger_tx_id"] or result.fact_id != f"{root}:{actor}"
            or result.visible_at.phase != "OBSERVE" or action not in ("cooperate", "defect")):
        raise ValueError("Partner update requires a direct, settled outcome")
    before = current.get(candidate, PartnerPerformanceTrust())
    after = before.consume(root, action == "cooperate")
    if after == before:
        return current
    trace.append({"kind": PARTNER_UPDATE, "tick": tick, "effective_tick": tick+1, "actor_id": actor,
                  "candidate_id": candidate, "root_claim_id": root, "fact_id": result.fact_id,
                  "ledger_tx_id": result.value["ledger_tx_id"], "partner_action": action,
                  "before": before.model_dump(), "after": after.model_dump()})
    return {**current, candidate: after}


def alternate_actors(config, households):
    return tuple(households[i] for i in config.partner_choice.alternate_indices)


def partner_endpoints(config, households, alternate=None):
    focal, incumbent = (households[i] for i in config.cooperation.household_indices)
    i = config.partner_choice.alternate_index if alternate is None else households.index(alternate)
    return focal, incumbent, households[i - 1], households[i]


def partner_fee_id(config):
    return f"coop-partner:{config.cooperation.followup.offer_tick - 1}:add:0"


def ranking_funded(config, actor, alternatives, sponsor, available, *, incumbent):
    stake = COOPERATION_STAKE_MINOR
    ranking = config.cooperation.trust_ranking
    candidates = (*alternatives, incumbent) if ranking and ranking.incumbent_trials else alternatives
    return (available(actor) >= config.partner_choice.fee_minor + stake
            and available(sponsor) >= COOPERATION_MAX_SUBSIDY_MINOR
            and all(available(candidate) >= stake for candidate in candidates))


def partner_offer(config, network, actor, tick, *, facts, sponsor, available):
    if config.partner_choice is None or tick != config.cooperation.followup.offer_tick - 1:
        return None
    focal, incumbent, _, _ = partner_endpoints(config, network.actors)
    if actor != focal:
        return None
    graph = RelationStore(network)
    alternatives = alternate_actors(config, network.actors)
    for candidate in alternatives:
        _, _, anchor, alternate = partner_endpoints(config, network.actors, candidate)
        if (not graph.can_deliver(focal, incumbent) or not graph.can_deliver(anchor, alternate)
                or graph.can_deliver(focal, alternate) or graph.can_deliver(anchor, incumbent)):
            return None
    candidates = (incumbent, *alternatives)
    choice = config.partner_choice
    if choice.comparison_trial is not None or choice.trust_ranking is not None:
        ranking = choice.trust_ranking
        verified = (all(previous_partner_result(facts, t.offer_tick, network.actors[t.alternate_index])
                        for t in ranking.scheduled_trials) if ranking is not None
                    else all(trial_partner_result(facts, config.cooperation, a) for a in alternatives))
        if not verified or not ranking_funded(config, actor, alternatives, sponsor, available, incumbent=incumbent):
            return None
        candidates = alternatives
    return ObservationFact(
        fact_id=f"cooperation-partner:{tick}:{actor}", category="market", owner_id=actor,
        visible_at={"tick": tick, "phase": "OBSERVE"},
        value={"kind": PARTNER_OFFER, "incumbent_id": incumbent, "candidate_ids": sorted(candidates),
               "fee_minor": config.partner_choice.fee_minor, "effective_tick": tick + 1},
    )


def select_partner(config, actor, tick, facts, terms, rng, *, trust_scores=None):
    candidates, incumbent = terms["candidate_ids"], terms["incumbent_id"]
    if config.partner_choice.random_choice:
        draw = rng.u64("closed.cooperation.partner", actor, tick, "choice")
        return candidates[draw % len(candidates)], [], f"{draw:016x}"
    ranking = config.partner_choice.trust_ranking
    if ranking is not None and ranking.enabled:
        scores = {a: PartnerPerformanceTrust.model_validate(trust_scores[a]) for a in candidates}
        if any(len(s.used_roots) != 3 for s in scores.values()):
            raise ValueError("Selection requires complete candidate performance counts")
        selected = min(candidates, key=lambda a: (-Fraction(scores[a].a, scores[a].a+scores[a].b), a))
        refs = sorted(f"{root}:{actor}" for s in scores.values() for root in s.used_roots)
        return selected, refs, None
    if config.partner_choice.comparison_trial is not None or ranking is not None:
        results = {a: trial_partner_result(facts, config.cooperation, a) for a in candidates}
        if any(result is None for result in results.values()):
            raise ValueError("Partner ranking needs both settled and visible results")
        selected = min(candidates, key=lambda a: (results[a].value["partner_action"] != "cooperate", a))
        return selected, sorted(r.fact_id for r in results.values()), None
    result = previous_partner_result(facts, config.cooperation.offer_tick, incumbent)
    if result is not None and result.value["partner_action"] == "defect":
        return next(a for a in candidates if a != incumbent), [result.fact_id], None
    return incumbent, [result.fact_id] if result else [], None


def schedule_partner_exchange(config, network, observations, bundles, trace, available_link_cash, *, sponsor):
    deltas = []
    for obs in observations:
        bundle = bundles[obs.actor_id]
        proposals = [p for p in bundle.network_proposals if isinstance(p, CooperationPartnerProposal)]
        fact = next((f for f in obs.market_view if f.value.get("kind") == PARTNER_OFFER), None)
        rank_window = (config.partner_choice is not None and (config.partner_choice.comparison_trial is not None
                       or config.partner_choice.trust_ranking is not None)
                       and obs.visible_until.tick == config.cooperation.followup.offer_tick - 1
                       and obs.actor_id == network.actors[config.cooperation.household_indices[0]])
        if not proposals and fact is None and not rank_window:
            continue
        tick = obs.visible_until.tick
        row = {"kind": PARTNER_TRACE, "tick": tick, "actor_id": obs.actor_id, "offers": fact.value if fact else None,
               "offered_fact_id": fact.fact_id if fact else None, "decision_id": bundle.decision_id,
               "attempted": int(bool(proposals)), "status": "unavailable"}
        if config.partner_choice and config.partner_choice.pin_incumbent:
            if proposals:
                row["status"] = "static_proposal_denied"
            elif fact is None:
                row["status"] = "admission_unavailable"
            elif not ranking_funded(config, obs.actor_id, fact.value["candidate_ids"], sponsor, available_link_cash,
                                    incumbent=fact.value["incumbent_id"]):
                row["status"] = "funding_rejected"
            else:
                row.update(status="fixed_incumbent", candidate_id=fact.value["incumbent_id"])
        elif len(proposals) > 1:
            row["status"] = "duplicate_proposal"
        elif not proposals:
            row["status"] = "unanswered" if fact else "admission_unavailable"
        else:
            proposal = proposals[0]
            candidate = proposal.candidate_id
            row["candidate_id"] = candidate
            if (fact is not None and fact.fact_id == proposal.option_id and fact.fact_id in bundle.evidence_ids
                    and candidate in fact.value["candidate_ids"]):
                incumbent = fact.value["incumbent_id"]
                if candidate == incumbent:
                    row["status"] = "retained"
                elif proposal.fee_limit_minor < config.partner_choice.fee_minor:
                    row["status"] = "fee_not_authorized"
                elif (available_link_cash(obs.actor_id) < config.partner_choice.fee_minor
                      or (rank_window and not ranking_funded(
                          config, obs.actor_id, fact.value["candidate_ids"], sponsor, available_link_cash,
                          incumbent=incumbent))):
                    row["status"] = "funding_rejected"
                else:
                    focal, incumbent, anchor, alternate = partner_endpoints(config, network.actors, candidate)
                    for operation, pairs in (("delete", ((focal, incumbent), (anchor, alternate))),
                                             ("upsert", ((focal, alternate), (anchor, incumbent)))):
                        verb = "add" if operation == "upsert" else "remove"
                        for i, (source, target) in enumerate(pairs):
                            deltas.append(EdgeDelta(delta_id=f"coop-partner:{tick}:{verb}:{i}",
                                proposed_tick=tick, effective_tick=tick + 1, layer="communication",
                                source_id=source, target_id=target, operation=operation,
                                value=CommunicationEdge(source_id=source, target_id=target) if verb == "add" else None))
                    row.update(status="scheduled", fee_delta_id=partner_fee_id(config), effective_tick=tick + 1)
        trace.append(row)
    return RelationStore(network).propose(deltas, tick=network.tick) if deltas else network
