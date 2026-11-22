"""Authorize incoming communication links against the frozen C/D observation."""
from civitas.contracts.base import content_hash
from civitas.contracts.economic import CooperationPartnerProposal, NetworkWithdrawal
from civitas.relations.store import RelationStore
from civitas.relations.types import CommunicationEdge, EdgeDelta
from .closed_economy_introductions import CandidateIntroduction, INTRODUCTION_KIND


def active_link_changes(network):
    latest = {(d.source_id, d.target_id): d for d in network.applied if d.layer == "communication"}
    return [d for d in latest.values() if d.operation == "upsert"]


def _proposal_order(proposal):
    if isinstance(proposal, NetworkWithdrawal):
        return 0, proposal.edge_delta_id, ""
    return 1, proposal.introduction_message_id, proposal.candidate_id


def schedule_links(network, observations, bundles, config, trace, *, available_link_cash=None):
    if config.partner_choice and config.partner_choice.pin_incumbent:
        return network
    proposals = [(observation, bundles[observation.actor_id]) for observation in observations
                 if any(not isinstance(p, CooperationPartnerProposal)
                        for p in bundles[observation.actor_id].network_proposals)]
    if network is None:
        if proposals:
            raise ValueError("Link proposals require the communication graph")
        return None

    tick = network.tick
    incoming = {actor: set() for actor in network.actors}
    for edge in network.communication:
        incoming[edge.target_id].add(edge.source_id)
    known = {d.delta_id for d in network.applied + network.pending}
    added = {d.delta_id: d for d in active_link_changes(network)}
    pending = []
    for observation, bundle in proposals:
        actor = observation.actor_id
        evidence_ids = set(bundle.evidence_ids)
        seen = {m.message_id: m for m in observation.delivered_messages
                if m.body.get("kind") == INTRODUCTION_KIND and m.message_id in evidence_ids}
        withdrawable = {edge["edge_delta_id"] for fact in observation.market_view
                        if fact.value.get("kind") == "closed_social_context" and fact.fact_id in evidence_ids
                        for edge in fact.value.get("withdrawable_links", ())}
        used = 0
        for proposal in sorted((p for p in bundle.network_proposals if not isinstance(p, CooperationPartnerProposal)),
                               key=_proposal_order):
            if isinstance(proposal, NetworkWithdrawal):
                origin = added.get(proposal.edge_delta_id)
                delta_id = "unfollow:" + content_hash([actor, proposal.edge_delta_id])
                event = {"kind": "network_withdrawal", "tick": tick, "actor_id": actor,
                         "decision_id": bundle.decision_id, "edge_delta_id": proposal.edge_delta_id,
                         "delta_id": delta_id, "slots_used": 0}
                if config.link_withdrawal_tick is None or config.social_mode == "none":
                    status = "withdrawal_disabled"
                elif delta_id in known:
                    status = "duplicate"
                elif (origin is None or origin.target_id != actor or proposal.edge_delta_id not in withdrawable
                        or origin.source_id not in incoming[actor]):
                    status = "edge_unavailable"
                elif used >= config.link_budget_per_tick:
                    status = "period_budget_exhausted"
                else:
                    pending.append(EdgeDelta(delta_id=delta_id, proposed_tick=tick, effective_tick=tick + 1,
                        layer="communication", source_id=origin.source_id, target_id=actor, operation="delete"))
                    known.add(delta_id)
                    used += 1
                    event.update(slots_used=1, source_id=origin.source_id, effective_tick=tick + 1)
                    status = "scheduled"
                trace.append({**event, "status": status})
                continue
            event = {"kind": "network_proposal", "tick": tick, "actor_id": actor,
                     "decision_id": bundle.decision_id,
                     "introduction_message_id": proposal.introduction_message_id,
                     "candidate_id": proposal.candidate_id, "slots_used": 0}
            message = seen.get(proposal.introduction_message_id)
            candidate = CandidateIntroduction.model_validate(message.body) if message is not None else None
            if (config.social_mode == "none" or actor not in incoming or candidate is None
                    or candidate.allow_follow is not True or candidate.observer_id != actor
                    or candidate.candidate_id != proposal.candidate_id
                    or not candidate.issued_tick < tick < candidate.expires_tick):
                trace.append({**event, "status": "introduction_unavailable"})
                continue
            delta_id = "follow:" + content_hash([actor, candidate.root_claim_id])
            event.update(root_claim_id=candidate.root_claim_id, delta_id=delta_id)
            if delta_id in known:
                status = "duplicate"
            elif candidate.candidate_id in incoming[actor]:
                status = "already_connected"
            elif used >= config.link_budget_per_tick:
                status = "period_budget_exhausted"
            elif len(incoming[actor]) >= config.network_neighbors + 1:
                status = "in_degree_limit"
            elif config.link_fee_minor and (proposal.fee_limit_minor or 0) < config.link_fee_minor:
                status = "link_fee_not_authorized"
            elif config.link_fee_minor and available_link_cash(actor) < config.link_fee_minor:
                status = "insufficient_link_fee_cash"
            else:
                edge = CommunicationEdge(source_id=candidate.candidate_id, target_id=actor)
                pending.append(EdgeDelta(delta_id=delta_id, proposed_tick=tick, effective_tick=tick + 1,
                    layer="communication", source_id=edge.source_id, target_id=edge.target_id,
                    operation="upsert", value=edge))
                known.add(delta_id)
                incoming[actor].add(edge.source_id)
                used += 1
                event.update(slots_used=1, effective_tick=tick + 1)
                if config.link_fee_minor:
                    event.update(fee_minor=config.link_fee_minor, fee_status="due")
                status = "scheduled"
            trace.append({**event, "status": status})
    return RelationStore(network).propose(pending, tick=tick)
