"""Funded two-person rounds held with the existing C/D scenario state."""

from civitas.contracts.economic import EconomicExecutionReceipt
from civitas.contracts.observations import ObservationFact
from civitas.economy.runtime import EconomicReject
from civitas.mechanisms.games import COOPERATION_MAX_SUBSIDY_MINOR, COOPERATION_STAKE_MINOR


FIRST_ROUND = "cooperation_round"
COMPARISON_ROUND = "cooperation_comparison_round"
FOLLOWUP_ROUND = "cooperation_followup_round"
COOPERATION_OUTCOME = "cooperation_outcome"
OUTCOME_PREFIX = "cooperation-outcome:"


def trust_trial_slot(tick):
    return f"cooperation_trial:{tick}"


def cooperation_slot(settings, tick):
    if settings is not None:
        if settings.trust_ranking is not None:
            if any(t.offer_tick == tick for t in settings.trust_ranking.scheduled_trials):
                return trust_trial_slot(tick)
        elif tick == settings.offer_tick:
            return FIRST_ROUND
        if settings.comparison_trial is not None and tick == settings.comparison_trial.offer_tick:
            return COMPARISON_ROUND
        if settings.followup is not None and tick == settings.followup.offer_tick:
            return FOLLOWUP_ROUND
    return None


def offered_round(settings, households, sponsor, economy, tick, *, alternate=None):
    slot = cooperation_slot(settings, tick)
    if slot is None:
        return None
    seats = [households[i] for i in settings.household_indices]
    if settings.trust_ranking is not None and slot != FOLLOWUP_ROUND:
        trial = next(t for t in settings.trust_ranking.scheduled_trials if t.offer_tick == tick)
        alternate = households[trial.alternate_index]
    elif settings.trust_ranking is not None and alternate is None:
        return {"round_id": f"cooperation:{tick}", "offer_tick": tick, "sponsor_id": sponsor,
                "seats": [], "offered": [], "choices": {}, "status": "no_active_exchange"}
    elif settings.comparison_trial is not None:
        if slot == FIRST_ROUND:
            alternate = households[settings.followup.partner_choice.alternate_index]
        elif slot == COMPARISON_ROUND:
            alternate = households[settings.comparison_trial.alternate_index]
        elif alternate is None:
            return {"round_id": f"cooperation:{tick}", "offer_tick": tick, "sponsor_id": sponsor,
                    "seats": [], "offered": [], "choices": {}, "status": "no_active_exchange"}
    if alternate is not None:
        seats[1] = alternate
    funded = (economy.account_view(sponsor).available_cash_minor >= COOPERATION_MAX_SUBSIDY_MINOR
              and all(economy.account_view(a).available_cash_minor >= COOPERATION_STAKE_MINOR for a in seats))
    return {"round_id": f"cooperation:{tick}", "offer_tick": tick, "sponsor_id": sponsor,
            "seats": seats, "offered": seats if funded else [], "choices": {},
            "status": "unmatched" if funded else "ineligible"}


def scripted_partner_action(settings, candidate_index):
    history = [t.action for t in settings.trust_ranking.scheduled_trials if t.alternate_index == candidate_index]
    if settings.followup.partner_outcome_rule == "recent":
        return history[-1]
    return "cooperate" if history.count("cooperate") >= 2 else "defect"


def partner_continuation(settings, candidate_index, candidate, rng):
    baseline = scripted_partner_action(settings, candidate_index)
    dose = settings.followup.partner_noise_bps
    draw = None if dose is None else rng.u64("closed.cooperation.continuation", candidate, settings.followup.offer_tick, "flip")
    flipped = draw is not None and draw < (dose << 64) // 10000
    assigned = {"cooperate": "defect", "defect": "cooperate"}[baseline] if flipped else baseline
    return {"baseline_action": baseline, "assigned_action": assigned, "flipped": flipped,
            "draw_u64_hex": f"{draw:016x}" if draw is not None else None}


def cooperation_fact(round_, actor, *, assigned_action=None):
    if round_ is None or actor not in round_["offered"]:
        return None
    return ObservationFact(
        fact_id=round_["round_id"], category="market", owner_id=actor,
        visible_at={"tick": round_["offer_tick"], "phase": "OBSERVE"},
        value={"kind": "cooperation_offer", "seat": round_["seats"].index(actor),
               "partner_id": next(a for a in round_["seats"] if a != actor),
               "sponsor_id": round_["sponsor_id"], "stake_minor": COOPERATION_STAKE_MINOR,
               "multiplier": {"numerator": 3, "denominator": 2}, "settlement_tick": round_["offer_tick"] + 1,
               **({"assigned_action": assigned_action} if assigned_action is not None else {})})


def record_cooperation_choice(round_, command, sequence):
    actor, intent = command.actor_id, command.intent
    reason = None
    if round_ is None or actor not in round_["offered"] or intent.opportunity_id != round_["round_id"]:
        reason = "cooperation_opportunity_unavailable"
    elif actor in round_["choices"]:
        reason = "duplicate_cooperation_choice"
    else:
        round_["choices"][actor] = {"action": intent.action, "command_id": command.command_id}
        if len(round_["choices"]) == 2 and all(choice["action"] != "decline" for choice in round_["choices"].values()):
            round_["status"] = "pending"
    return EconomicExecutionReceipt(
        command_id=command.command_id, decision_id=command.decision_id, actor_id=actor,
        execution_seq=sequence, tick=command.tick, world_version=command.world_version,
        status="rejected" if reason else "accepted",
        outcome="rejected" if reason else "cooperation_choice_recorded", unit="cooperation_choice",
        executed_quantity=0, reason_codes=[reason] if reason else [],
    )


def settle_pending_cooperation(round_, economy, tick):
    if round_ is None or round_["status"] != "pending" or tick != round_["offer_tick"] + 1:
        return
    contributions = {actor: COOPERATION_STAKE_MINOR if choice["action"] == "cooperate" else 0
                     for actor, choice in round_["choices"].items()}
    try:
        entry = economy.settle_cooperation(round_["round_id"], round_["offer_tick"], round_["sponsor_id"], contributions)
    except EconomicReject as exc:
        round_.update(status="funding_rejected", reason=str(exc))
    else:
        round_.update(status="settled", settlement_tx_id=entry.tx_id)


def cooperation_outcome_facts(round_, tick, journal):
    if round_ is None or round_["status"] != "settled":
        return []
    entry = next((e for e in reversed(journal) if e.tx_id == round_["settlement_tx_id"]), None)
    if (entry is None or entry.kind != "cooperation_settlement" or entry.tick != tick
            or entry.payload["round_id"] != round_["round_id"]
            or set(entry.payload["contributions"]) != set(round_["seats"])):
        raise ValueError("Private cooperation result disagrees with its journal transaction")
    root = OUTCOME_PREFIX + entry.tx_id
    facts = []
    first, second = round_["seats"]
    for actor, partner in ((first, second), (second, first)):
        facts.append(ObservationFact(
            fact_id=f"{root}:{actor}", category="memory", owner_id=actor,
            visible_at={"tick": tick + 1, "phase": "OBSERVE"},
            value={"kind": COOPERATION_OUTCOME, "ledger_tx_id": entry.tx_id, "root_claim_id": root,
                   "target_id": round_["round_id"], "partner_id": partner,
                   "partner_action": "cooperate" if entry.payload["contributions"][partner] else "defect",
                   "cash_delta_minor": entry.details["cash_delta_minor"][actor]},
        ))
    return facts


def previous_partner_result(facts, first_tick, partner):
    return next((f for f in facts if f.value.get("kind") == COOPERATION_OUTCOME
                 and f.value["target_id"] == f"cooperation:{first_tick}" and f.value["partner_id"] == partner), None)


def trial_partner_result(facts, settings, partner):
    if settings.trust_ranking is not None:
        return next((r for t in reversed(settings.trust_ranking.scheduled_trials)
                     if (r := previous_partner_result(facts, t.offer_tick, partner)) is not None), None)
    result = previous_partner_result(facts, settings.offer_tick, partner)
    if result is None and settings.comparison_trial is not None:
        result = previous_partner_result(facts, settings.comparison_trial.offer_tick, partner)
    return result
