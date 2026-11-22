"""Registered labor experiments over durable closed-economy worlds."""

import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from statistics import covariance, fmean, stdev
from typing import Literal

from pydantic import Field, model_validator

from civitas.application import economic_run
from civitas.contracts.base import NonNegative, Record, content_hash, require_unique
from civitas.core.rng import AddressedRNG
from civitas.mechanisms.games import COOPERATION_MAX_SUBSIDY_MINOR
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_cooperation import (
    COMPARISON_ROUND, FIRST_ROUND, FOLLOWUP_ROUND, OUTCOME_PREFIX, previous_partner_result,
    partner_continuation, trial_partner_result, trust_trial_slot,
)
from civitas.scenarios.closed_economy_labor_outcomes import SWITCH_OFFER
from civitas.scenarios.closed_economy_partner import (
    PARTNER_DRAW, PARTNER_TRACE, PARTNER_TRUST, PARTNER_UPDATE, PartnerPerformanceTrust,
    alternate_actors, partner_endpoints, select_partner,
)
from civitas.scenarios.closed_economy_policy import LABOR_DRAW, labor_random_draw, labor_wage_rank
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from . import registry
from .closed_economy import audit_snapshot
from .d1_analysis import D1_ANALYSIS, D1_ARMS, summarize_d1

ARMS = {"t0r0": [False, False], "t0r1": [False, True], "t1r0": [True, False], "t1r1": [True, True]}
ANALYSIS = {
    "id": "labor-factorial-pilot.v1",
    "unit": "root_seed_world",
    "factor_order": ["use_wage_trust_threshold", "allow_selective_switch"],
    "endpoint": {
        "id": "second_window_worked",
        "units": "worked shifts per assigned worker-window",
        "denominator_per_completed_world": 1,
        "time": "second offer tick + 1",
        "no_offer_or_no_work": 0,
        "failed_or_incomplete": None,
    },
    "contrast": "t1r1 - t0r1 - t1r0 + t0r0",
    "matched_quartet": "equal verified pretreatment state trajectory and frozen candidate offer terms",
    "missing": "retain every assignment; bound missing binary outcomes in [0, 1]",
    "uncertainty": "descriptive sample SD, MCSE and covariance; no significance test",
    "scope": "fixed two-window engineering pilot",
}
SWITCH_ARMS = {"selective": [True, True], "random": [False, True]}
SWITCH_ANALYSIS = {
    "id": "labor-random-control.v1",
    "unit": ANALYSIS["unit"],
    "endpoint": ANALYSIS["endpoint"],
    "contrast": "selective - random",
    "matched_pair": ANALYSIS["matched_quartet"],
    "missing": ANALYSIS["missing"],
    "uncertainty": ANALYSIS["uncertainty"],
    "allocated_switch_slots_per_world": 1,
    "randomization": "one addressed uniform draw from visible offers ordered by ID; no replacement on rejection",
    "scope": "matched opportunity quota; realized switch counts are outcomes",
}
MULTILATERAL_ANALYSIS = {
    **SWITCH_ANALYSIS,
    "id": "labor-matched-attempts.v1",
    "randomization": "one addressed uniform draw from visible switch offers only; no replacement on rejection",
    "ranking": "descending mature wage score; symmetric prior for unknown; tie by offer ID",
    "scope": "one attempted switch per world with a visible alternative; actual work is an outcome",
}
STOPPING_ANALYSIS = {
    **MULTILATERAL_ANALYSIS,
    "id": "labor-common-stopping.v1",
    "endpoint": {
        **ANALYSIS["endpoint"],
        "id": "second_window_worked_by_assigned_stop",
        "time": "scheduled work tick if reached by assigned stop",
        "stopped_before_work": 0,
    },
    "stopping_rule": "shared geometric hazard after each commit before the public administrative cap",
    "scope": "all assigned pairs; report unreached, censored and observed work windows separately",
}
COOPERATION_ARMS = {"cooperate": [True, True], "defect": [True, True]}
COOPERATION_ANALYSIS = {
    "id": "bilateral-cooperation.v1",
    "unit": ANALYSIS["unit"],
    "endpoint": "cooperative choices / eligible offered seats; undefined with zero offers",
    "contrast": "cooperate - defect",
    "matched_pair": "equal verified pre-offer states and cooperation opportunities",
    "stopping_rule": STOPPING_ANALYSIS["stopping_rule"],
    "missing": "retain every assignment; bound missing all-assigned cooperation shares in [0, 1]",
    "uncertainty": "descriptive sample SD and MCSE across paired worlds",
    "scope": "one funded two-person round with scripted choices and a shared hidden stopping draw",
}
FEEDBACK_ARMS = {"conditional": [True, True], "fixed": [True, True]}
FEEDBACK_ANALYSIS = {
    **COOPERATION_ANALYSIS,
    "id": "bilateral-cooperation-feedback.v1",
    "endpoint": "follow-up cooperative choices / eligible offered follow-up seats; undefined with zero offers",
    "contrast": "conditional - fixed",
    "matched_pair": "equal verified states through the first round and equal follow-up opportunities",
    "scope": "same two fixed partners; copy the previous settled partner action versus always cooperate",
}
PARTNER_ARMS = {"evidence": [True, True], "uniform": [True, True]}
PARTNER_ANALYSIS = {
    **FEEDBACK_ANALYSIS,
    "id": "degree-preserving-partner-choice.v1",
    "endpoint": "active replacements by assigned stop per allocated chooser",
    "contrast": "evidence - uniform",
    "matched_pair": "equal states before selection and equal private terms and candidates",
    "missing": "retain every assignment; bound missing replacement outcomes in [0, 1]",
    "scope": "one choice including retention, one directed exchange slot, common fee and follow-up rules",
}
PARTNER_RANKING_ANALYSIS = {
    **FEEDBACK_ANALYSIS,
    "id": "matched-exchange-partner-ranking.v1",
    "endpoint": "cooperative choices per two assigned follow-up seats",
    "contrast": "evidence - uniform",
    "matched_pair": "equal pre-selection states and options, equal realized replacements, fees and follow-up opportunity counts",
    "scope": "two settled trial partners, both arms choose one eligible alternate from the same observed candidate set",
}
PARTNER_TRUST_ARMS = {"cumulative": [True, True], "recent": [True, True]}
PARTNER_TRUST_ANALYSIS = {
    **PARTNER_RANKING_ANALYSIS,
    "id": "bounded-partner-performance-trust.v1",
    "contrast": "cumulative - recent",
    "matched_pair": "equal pre-selection histories after masking only private trust counters and update records, equal offers, replacements, fees and follow-up opportunity counts",
    "scope": "three direct trial outcomes per candidate; bounded Beta(1,1) counts versus latest witnessed action",
    "counterbalance": "successive two-seed blocks: original, swap labels, reverse within-wave order, swap both",
}
PARTNER_FACTORIAL_ANALYSIS = {
    **{key: value for key, value in PARTNER_TRUST_ANALYSIS.items() if key != "matched_pair"},
    "id": "matched-exchange-partner-trust-factorial.v1",
    "factor_order": ["update_partner_trust", "rank_visible_partners"],
    "contrast": ANALYSIS["contrast"],
    "matched_quartet": PARTNER_TRUST_ANALYSIS["matched_pair"],
    "randomization": "uniform arms share one addressed draw over the two eligible alternatives",
    "scope": "trust update crossed with evidence ranking under one matched realized exchange slot",
}
PARTNER_SIGNAL_ANALYSIS = {
    **PARTNER_FACTORIAL_ANALYSIS,
    "id": "held-out-partner-signal-factorial.v1",
    "counterbalance": "cycle original, swap labels, reverse within-wave order, swap both",
    "scope": "same four selection arms with a private, scripted alternate action after exchange",
    "prediction_check": "unweighted binary Brier loss on both candidates in each matched root where both ranked branches settle; six trials precede selection",
}
PARTNER_STATIC_ARMS = {"static": [False, False], "uniform": [True, False], "ranked": [True, True]}
PARTNER_STATIC_ANALYSIS = {
    **PARTNER_RANKING_ANALYSIS,
    "id": "matched-opportunity-static-partner.v1",
    "factor_order": ["allow_exchange", "rank_visible_partners"],
    "contrast": ["uniform - static", "ranked - uniform", "ranked - static"],
    "matched_pair": "equal history, admission and realized seats; uniform and ranked also match swaps and fees",
    "scope": "one retained or replaced partner, with common incumbent preparation and real exchange fees",
    "counterbalance": PARTNER_SIGNAL_ANALYSIS["counterbalance"],
    "money": "settled follow-up gain of both players minus real link fees, excluding the sponsor",
}
PARTNER_NOISE_ANALYSIS = {
    **PARTNER_STATIC_ANALYSIS,
    "id": "matched-opportunity-partner-noise.v1",
    "noise_process": "closed.cooperation.continuation, addressed by root, replica, candidate and follow-up tick; slot flip",
    "prediction_check": "unweighted binary Brier on unique settled root-candidate outcomes in matched triplets",
}


class LaborPilotSpec(Record):
    schema_version: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14] = 1
    base_config: ClosedEconomyConfig
    seeds: list[NonNegative] = Field(min_length=1)

    @model_validator(mode="after")
    def requires_two_windows(self):
        require_unique(self.seeds, "pilot seed")
        if self.base_config.offline_cooperation_action is not None:
            raise ValueError("Labor pilots require the rule policy")
        transfer = self.base_config.labor_transfer
        window = transfer.second_rehire if transfer is not None else None
        if window is None or window.switch_only != (self.schema_version >= 3):
            raise ValueError("Pilot v3+ require a second window with two alternatives; v1/v2 use one")
        if (self.base_config.stopping_hazard_bps is not None) != (self.schema_version >= 4):
            raise ValueError("A stopping hazard is required exclusively for pilot v4+")
        cooperation = self.base_config.cooperation
        if (cooperation is not None) != (self.schema_version >= 5):
            raise ValueError("The cooperation round is required exclusively for pilot v5+")
        if (cooperation is not None and cooperation.followup is not None) != (self.schema_version >= 6):
            raise ValueError("The follow-up cooperation round is required exclusively for pilot v6+")
        if (self.base_config.partner_choice is not None) != (self.schema_version >= 7):
            raise ValueError("Partner selection is required exclusively for pilot v7+")
        if (cooperation is not None and cooperation.comparison_trial is not None) != (self.schema_version == 8):
            raise ValueError("Two trial partners are required exclusively for pilot v8")
        if (cooperation is not None and cooperation.trust_ranking is not None) != (self.schema_version >= 9):
            raise ValueError("Bounded partner trust is required exclusively for pilot v9+")
        if bool(cooperation and cooperation.followup and cooperation.followup.partner_outcome_rule) != (self.schema_version >= 11):
            raise ValueError("Scripted held-out partner outcomes are required exclusively for pilot v11+")
        noisy = cooperation and cooperation.followup and cooperation.followup.partner_noise_bps is not None
        if bool(noisy) != (self.schema_version >= 13):
            raise ValueError("An explicit continuation noise dose is required for pilot v13+ only")
        if self.schema_version >= 9:
            if bool(cooperation.trust_ranking.incumbent_trials) != (self.schema_version >= 12):
                raise ValueError("Incumbent trials are required exclusively for pilot v12+")
            if self.schema_version == 9 and self.base_config.partner_choice.random_choice:
                raise ValueError("Pilot v9 requires both policies to rank candidates")
            trials = cooperation.trust_ranking.trials
            if any(a.alternate_index == b.alternate_index for a,b in zip(trials[::2], trials[1::2])):
                raise ValueError("Each trust counterbalance wave needs one trial per candidate")
        if cooperation is not None and cooperation.offer_tick != window.offer_tick:
            raise ValueError("The cooperation and second labor offer ticks must agree")
        if self.schema_version < 3 and not window.minimum_trust_bps:
            raise ValueError("The pilot needs two windows and a nonzero second-window trust threshold")
        if self.schema_version > 1 and "accept_labor_offer" in self.base_config.disabled_actions:
            raise ValueError("The random control requires labor acceptance enabled")
        return self


def _protocol(spec):
    if spec.schema_version == 1:
        return ARMS, ANALYSIS
    if spec.schema_version == 5:
        return COOPERATION_ARMS, COOPERATION_ANALYSIS
    if spec.schema_version == 6:
        return FEEDBACK_ARMS, FEEDBACK_ANALYSIS
    if spec.schema_version == 7:
        return PARTNER_ARMS, PARTNER_ANALYSIS
    if spec.schema_version == 8:
        return PARTNER_ARMS, PARTNER_RANKING_ANALYSIS
    if spec.schema_version == 9:
        return PARTNER_TRUST_ARMS, PARTNER_TRUST_ANALYSIS
    if spec.schema_version == 10:
        return ARMS, PARTNER_FACTORIAL_ANALYSIS
    if spec.schema_version == 11:
        return ARMS, PARTNER_SIGNAL_ANALYSIS
    if spec.schema_version == 12:
        return PARTNER_STATIC_ARMS, PARTNER_STATIC_ANALYSIS
    if spec.schema_version == 13:
        return PARTNER_STATIC_ARMS, PARTNER_NOISE_ANALYSIS
    if spec.schema_version == 14:
        return D1_ARMS, D1_ANALYSIS
    return SWITCH_ARMS, {
        2: SWITCH_ANALYSIS,
        3: MULTILATERAL_ANALYSIS,
        4: STOPPING_ANALYSIS,
    }[spec.schema_version]


def _stopping_schedule(spec):
    if spec.schema_version < 4:
        return {}
    config = spec.base_config
    threshold = (config.stopping_hazard_bps << 64) // 10000
    schedule = {}
    for seed in spec.seeds:
        rng = AddressedRNG(seed, config.replica)
        for tick in range(1, config.ticks):
            draw = rng.u64("closed.labor.ending", "world", tick, "after-commit")
            if draw < threshold:
                schedule[str(seed)] = {"tick": tick, "reason": "geometric", "u64_hex": f"{draw:016x}"}
                break
        else:
            schedule[str(seed)] = {"tick": config.ticks, "reason": "administrative_cap"}
    return schedule


def _jobs(spec):
    arms, _ = _protocol(spec)
    for seed_index, seed in enumerate(spec.seeds):
        for arm, factors in arms.items():
            trust, switch = (True, True) if spec.schema_version >= 10 else factors
            raw = spec.base_config.model_dump(mode="json")
            raw["root_seed"] = seed
            window = raw["labor_transfer"]["second_rehire"]
            if not trust:
                window.pop("minimum_trust_bps", None)
            window["allow_switch"] = switch
            if arm == "random":
                window["random_offer_choice"] = True
            if spec.schema_version == 5:
                raw["cooperation"]["actions"] = [arm, arm]
            if spec.schema_version == 6:
                raw["cooperation"]["followup"]["use_partner_result"] = arm == "conditional"
            if spec.schema_version in (7, 8):
                raw["cooperation"]["followup"]["partner_choice"]["random_choice"] = arm == "uniform"
            if spec.schema_version >= 9:
                choice = raw["cooperation"]["followup"]["partner_choice"]
                ranking = choice["trust_ranking"]
                if spec.schema_version == 14:
                    ranking["enabled"], exchange = factors
                    choice["pin_incumbent"] = not exchange
                    choice["random_choice"] = arm == "uniform"
                elif spec.schema_version >= 12:
                    ranking["enabled"] = True
                    choice["pin_incumbent"] = not factors[0]
                    choice["random_choice"] = arm == "uniform"
                elif spec.schema_version >= 10:
                    ranking["enabled"], ranked = factors
                    choice["random_choice"] = not ranked
                else:
                    ranking["enabled"] = arm == "cumulative"
                trials = ranking["trials"]
                layout = (seed_index if spec.schema_version >= 11 else seed_index // 2) % 4
                if layout & 1:
                    first, second = spec.base_config.partner_choice.alternate_indices
                    for trial in trials:
                        trial["alternate_index"] = second if trial["alternate_index"] == first else first
                if layout & 2:
                    for left, right in zip(trials[::2], trials[1::2]):
                        for key in ("alternate_index", "action"):
                            left[key], right[key] = right[key], left[key]
            yield seed, arm, ClosedEconomyConfig.model_validate(raw)


def _matched_blocks(rows, seeds, signs, block_name, *, outcome="worked", maximum=1, exchange_mismatches=()):
    require_unique(seeds, "pilot seed")
    by_key = {(row["seed"], row["arm"]): row for row in rows}
    if not seeds or len(by_key) != len(rows) or any(seed not in seeds or arm not in signs for seed, arm in by_key):
        raise ValueError("Rows must map uniquely to registered pilot assignments")
    blocks, excluded, lower, upper = [], [], 0, 0
    for seed in seeds:
        block = {arm: by_key.get((seed, arm), {}) for arm in signs}
        values = {arm: row[outcome] if row.get("status") == "completed" else None for arm, row in block.items()}
        for arm, value in values.items():
            sign = signs[arm]
            if block[arm].get("status") == "completed" and (type(value) is not int or not 0 <= value <= maximum):
                raise ValueError(f"A completed world must have an integer {outcome} outcome in [0, {maximum}]")
            lower += maximum * min(0, sign) if value is None else sign * value
            upper += maximum * max(0, sign) if value is None else sign * value
        missing = {arm: block[arm].get("status", "not_run") for arm, value in values.items() if value is None}
        if missing:
            excluded.append({"seed": seed, "reason": f"incomplete_{block_name}", "arms": missing})
        elif len({row["pretreatment_hash"] for row in block.values()}) != 1:
            excluded.append({"seed": seed, "reason": "pretreatment_mismatch"})
        elif len({row["opportunity_hash"] for row in block.values()}) != 1:
            excluded.append({"seed": seed, "reason": "opportunity_mismatch"})
        elif seed in exchange_mismatches:
            excluded.append({"seed": seed, "reason": "realized_exchange_mismatch"})
        else:
            blocks.append((seed, values))
    return blocks, excluded, [lower / (maximum * len(seeds)), upper / (maximum * len(seeds))]


def _difference_stats(estimates):
    n = len(estimates)
    sd = stdev(estimates) if n > 1 else None
    return {
        "mean": fmean(estimates) if n else None,
        "sample_sd": sd,
        "monte_carlo_se": sd / n**0.5 if sd is not None else None,
    }


def summarize_interaction(rows, seeds, *, partner=False):
    """Paired contrasts and worst-case bounds over the full assignment denominator."""
    maximum, outcome = (2, "cooperative_choices") if partner else (1, "worked")
    effect_on, effect_off = (("trust_effect_ranked", "trust_effect_uniform") if partner
                             else ("trust_effect_switch_on", "trust_effect_switch_off"))
    mismatches = ()
    offered = {}
    if partner:
        mismatches = _exchange_mismatches(rows, seeds, ARMS)
        offered = {r["seed"]: r["cooperation"]["offered"] for r in rows if r["status"] == "completed"}
        rows = [dict(r, cooperative_choices=r["cooperation"]["cooperative_choices"])
                if r["status"] == "completed" else r for r in rows]
    blocks, excluded, bounds = _matched_blocks(
        rows, seeds, {"t0r0": 1, "t0r1": -1, "t1r0": -1, "t1r1": 1}, "quartet",
        outcome=outcome, maximum=maximum, exchange_mismatches=mismatches,
    )
    quartets = []
    for seed, values in blocks:
        on = (values["t1r1"] - values["t0r1"]) / maximum if partner else values["t1r1"] - values["t0r1"]
        off = (values["t1r0"] - values["t0r0"]) / maximum if partner else values["t1r0"] - values["t0r0"]
        quartet = {"seed": seed, effect_on: on, effect_off: off, "interaction": on - off}
        if partner:
            quartet.update(choices_by_arm=values, offered_per_world=offered[seed])
        quartets.append(quartet)
    eligible = [r["interaction"] * maximum / r["offered_per_world"] for r in quartets
                if partner and r["offered_per_world"]]
    n = len(quartets)
    return {
        "assigned_quartets": len(seeds),
        "matched_quartets": n,
        "excluded_quartets": excluded,
        "contrasts": quartets,
        **_difference_stats([row["interaction"] for row in quartets]),
        "paired_simple_effect_covariance": covariance(
            [row[effect_on] for row in quartets],
            [row[effect_off] for row in quartets],
        )
        if n > 1
        else None,
        "all_assigned_bounds": bounds,
        **({"offered_quartets": len(eligible), "offered_rate": _difference_stats(eligible),
            "exchange_mismatches": mismatches} if partner else {}),
    }


def summarize_switch_control(rows, seeds, *, arms=("selective", "random"), outcome="worked"):
    treatment, control = arms
    blocks, excluded, bounds = _matched_blocks(rows, seeds, {treatment: 1, control: -1}, "pair", outcome=outcome)
    contrasts = [{"seed": seed, **v, "difference": v[treatment] - v[control]} for seed, v in blocks]
    return {
        "assigned_pairs": len(seeds),
        "matched_pairs": len(blocks),
        "excluded_pairs": excluded,
        "contrasts": contrasts,
        **_difference_stats([row["difference"] for row in contrasts]),
        "paired_outcome_covariance": covariance([v[treatment] for _, v in blocks], [v[control] for _, v in blocks])
        if len(blocks) > 1
        else None,
        "all_assigned_bounds": bounds,
    }


def summarize_cooperation(rows, seeds, *, arms=("cooperate", "defect"), match_selection=False, mismatches=()):
    treatment, control = arms
    measured = [
        {
            **row,
            "opportunity_hash": row["opportunity_hash"] if match_selection else row["cooperation"]["opportunity_hash"],
            "cooperative_choices": row["cooperation"]["cooperative_choices"],
        }
        if row["status"] == "completed"
        else row
        for row in rows
    ]
    blocks, excluded, bounds = _matched_blocks(
        measured, seeds, {treatment: 1, control: -1}, "pair", outcome="cooperative_choices", maximum=2,
        exchange_mismatches=mismatches,
    )
    offered = {(row["seed"], row["arm"]): row["cooperation"]["offered"] for row in rows if row["status"] == "completed"}
    contrasts = [
        {"seed": seed, **values, "offered_per_world": offered[seed, treatment],
         "assigned_difference": (values[treatment] - values[control]) / 2}
        for seed, values in blocks
    ]
    eligible = [r[treatment] / r["offered_per_world"] - r[control] / offered[r["seed"], control]
                for r in contrasts if r["offered_per_world"] and offered[r["seed"], control]]
    return {
        "assigned_pairs": len(seeds), "matched_pairs": len(blocks), "excluded_pairs": excluded,
        "offered_pairs": len(eligible), "contrasts": contrasts,
        "offered_rate": _difference_stats(eligible),
        "all_assigned_share": _difference_stats([r["assigned_difference"] for r in contrasts]),
        "all_assigned_bounds": bounds,
    }


def _measure_cooperation(snapshot, slot=FIRST_ROUND):
    state = snapshot.data["closed_economy"]
    round_ = state[slot]
    entries = [e for e in snapshot.data["economic_runtime"]["journal"] if e["kind"] == "cooperation_settlement"
               and (FOLLOWUP_ROUND not in state or (round_ and e["payload"]["round_id"] == round_["round_id"]))]
    status = round_["status"] if round_ else "not_reached"
    offered = len(round_["offered"]) if round_ else 0
    choices = Counter(choice["action"] for choice in round_["choices"].values()) if round_ else Counter()
    if choices.total() > offered or len(entries) != int(status == "settled"):
        raise ValueError("Cooperation choices or settlement disagree with the committed round")
    entry = entries[0] if entries else None
    if entry and entry["tx_id"] != round_["settlement_tx_id"]:
        raise ValueError("Cooperation round references a different settlement")
    deltas = entry["details"]["cash_delta_minor"] if entry else None
    return {
        "assigned_seats": 2, "offered": offered, "cooperative_choices": choices["cooperate"],
        "defections": choices["defect"], "declines": choices["decline"], "unanswered": offered - choices.total(),
        "cooperation_rate": choices["cooperate"] / offered if offered else None,
        "settlement_status": "censored" if status == "pending" else status,
        "settlement_tx_id": entry["tx_id"] if entry else None,
        "cash_delta_minor": deltas,
        "sponsor_subsidy_minor": -deltas[round_["sponsor_id"]] if deltas is not None else None,
        "opportunity_hash": content_hash([round_[k] for k in ("offer_tick", "seats", "offered", "sponsor_id")])
        if round_ else None,
    }


def _measure_feedback(snapshot, config):
    followup = config.cooperation.followup
    state = snapshot.data["closed_economy"]
    round_ = state[FOLLOWUP_ROUND]
    offered = round_["offered"] if round_ else []
    visible = []
    slots = (FIRST_ROUND, COMPARISON_ROUND)
    if config.cooperation.trust_ranking:
        slots = tuple(trust_trial_slot(t.offer_tick) for t in config.cooperation.trust_ranking.scheduled_trials)
    for slot in slots:
        prior = state.get(slot)
        if not prior or prior["status"] != "settled":
            continue
        root = OUTCOME_PREFIX + prior["settlement_tx_id"]
        facts = [f for f in snapshot.memories if f.value.get("root_claim_id") == root]
        expected_tick = prior["offer_tick"] + 2
        if (len(facts) != 2 or {f.owner_id for f in facts} != set(prior["seats"])
                or any(f.public or f.visible_at.tick != expected_tick or f.visible_at.phase != "OBSERVE" for f in facts)):
            raise ValueError("Trial settlement must produce one next-tick private result per participating seat")
        visible.extend(f for f in facts if f.visible_at.tick <= snapshot.tick)
    uses = {}
    for decision in snapshot.decisions:
        if decision.tick != followup.offer_tick:
            continue
        actual_refs = [ref for ref in decision.evidence_ids if ref.startswith(OUTCOME_PREFIX)]
        fact = None
        if followup.use_partner_result and decision.actor_id in offered:
            partner = next(a for a in offered if a != decision.actor_id)
            fact = trial_partner_result([f for f in visible if f.owner_id == decision.actor_id],
                                        config.cooperation, partner)
        if actual_refs != ([fact.fact_id] if fact else []):
            raise ValueError("Follow-up decision references unexpected cooperation evidence")
        if decision.actor_id in offered:
            expected = fact.value["partner_action"] if fact else "cooperate"
            if followup.partner_outcome_rule and decision.actor_id == round_["seats"][1]:
                households = snapshot.data["closed_social"]["households"]
                expected = partner_continuation(config.cooperation, households.index(decision.actor_id), decision.actor_id,
                                                AddressedRNG(config.root_seed, config.replica))["assigned_action"]
            if round_["choices"].get(decision.actor_id, {}).get("action") != expected:
                raise ValueError("Follow-up choice disagrees with the registered policy")
        if fact:
            uses[decision.actor_id] = fact.fact_id
    if round_ and set(round_["choices"]) != set(offered):
        raise ValueError("Follow-up choices do not cover the registered offers")
    return {"feedback_visible_seats": len({f.owner_id for f in visible}), "feedback_uses": len(uses),
            "feedback_evidence": uses}


def _measure_partner(snapshot, config):
    social = snapshot.data["closed_social"]
    network = social["network"]
    focal, incumbent, _, _ = partner_endpoints(config, network["actors"])
    alternatives = alternate_actors(config, network["actors"])
    ranking = config.cooperation.trust_ranking
    rank = config.partner_choice.comparison_trial is not None or ranking is not None
    effective = config.cooperation.followup.offer_tick
    tick = effective - 1
    trace = [r for r in social["trace"] if r["kind"] == PARTNER_TRACE and r["actor_id"] == focal and r["tick"] == tick]
    choice = trace[0] if trace else None
    offers = choice["offers"] if choice else None
    pinned = config.partner_choice.pin_incumbent
    if pinned and (network["pending"] or network["applied"]):
        raise ValueError("A static graph cannot contain edge operations")
    if (len(trace) != int(snapshot.tick >= tick) or (choice and choice["attempted"] != int(offers is not None and not pinned))
            or (choice and not rank and offers is None)):
        raise ValueError("Expected one planned partner attempt at the selection barrier")
    fees = [e for e in snapshot.data["economic_runtime"]["journal"] if e["kind"] == "link_fee"]
    status = choice["status"] if choice else "not_reached"
    active = status == "scheduled" and snapshot.tick >= effective
    if len(fees) != int(active):
        raise ValueError("Fee must coincide with an active partner exchange")
    if pinned:
        candidate, result_refs, draw = incumbent, [], None
        if status == "fixed_incumbent" and (offers is None or choice["candidate_id"] != incumbent):
            raise ValueError("Static cooperation needs a committed incumbent admission")
        if choice is not None:
            decision = next(d for d in snapshot.decisions if d.decision_id == choice["decision_id"])
            if (PARTNER_DRAW in decision.belief_patch or any(ref.startswith(OUTCOME_PREFIX) or
                    ref == f"partner-trust:{tick}:{focal}" for ref in decision.evidence_ids)):
                raise ValueError("A static partner cannot consume selection evidence")
    elif offers is not None:
        rng = AddressedRNG(config.root_seed, config.replica)
        facts = [f for f in snapshot.memories if f.owner_id == focal and f.visible_at.tick <= tick]
        scores = social["current"][focal].get(PARTNER_TRUST, {})
        candidate, result_refs, draw = select_partner(config, focal, tick, facts, offers, rng, trust_scores=scores)
        decision = next(d for d in snapshot.decisions if d.decision_id == choice["decision_id"])
        if (choice["candidate_id"] != candidate or decision.belief_patch.get(PARTNER_DRAW) != draw
                or [ref for ref in decision.evidence_ids if ref.startswith(OUTCOME_PREFIX)] != result_refs):
            raise ValueError("Partner attempt disagrees with its registered rule or draw")
        if ranking is not None and (f"partner-trust:{tick}:{focal}" in decision.evidence_ids) != (
                ranking.enabled and not config.partner_choice.random_choice):
            raise ValueError("Partner decision disagrees with its trust update assignment")
    else:
        candidate, result_refs, draw = None, [], None
    edges = {(e["source_id"], e["target_id"]) for e in network["communication"]}
    if ((focal, incumbent) in edges) == active:
        raise ValueError("Committed incumbent edge disagrees with exchange status")
    for alternate in alternatives:
        _, _, anchor, _ = partner_endpoints(config, network["actors"], alternate)
        chosen = active and candidate == alternate
        if (((focal, alternate) in edges) != chosen or ((anchor, incumbent) in edges) != chosen
                or ((anchor, alternate) in edges) == chosen):
            raise ValueError("Committed alternate edges disagree with the planned exchange")
    following = snapshot.data["closed_economy"][FOLLOWUP_ROUND]
    selected = candidate if active else incumbent
    expected_seats = [] if rank and not (active or (pinned and status == "fixed_incumbent")) else [focal, selected]
    if following and following["seats"] != expected_seats:
        raise ValueError("Follow-up cooperation does not use the committed partner")
    return {
        "choice": choice, "status": "active" if active else "censored" if status == "scheduled" else status,
        "active_partner": selected, "followup_seats": following["seats"] if following else None,
        **({"excluded_candidates": sorted(set((incumbent, *alternatives)) - set(expected_seats)) if following else [],
            "result_fact_ids": result_refs}
           if rank else {"excluded_candidate": (incumbent if active else alternatives[0]) if following else None,
                         "result_fact_id": result_refs[0] if result_refs else None}),
        "draw_u64_hex": draw,
        "fee_minor": fees[0]["payload"]["amount_minor"] if fees else 0,
        "fee_tx_id": fees[0]["tx_id"] if fees else None, "replaced": int(active),
        "opportunity_hash": content_hash(offers) if offers is not None else None,
    }


def _exchange_mismatches(rows, seeds, arms):
    counts = {(r["seed"], r["arm"]): (r["partner_replaced"], r["partner_selection"]["fee_minor"],
                                      r["cooperation"]["offered"])
              for r in rows if r["status"] == "completed"}
    return [seed for seed in seeds if len({counts[seed, arm] for arm in arms if (seed, arm) in counts}) > 1]


def _matched_trust_history(snapshot):
    raw = snapshot.model_dump(mode="json")
    social = raw["data"]["closed_social"]
    social["trace"] = [r for r in social["trace"] if r["kind"] != PARTNER_UPDATE]
    for state in [*social["current"].values(), *(p["state"] for p in social["pending"])]:
        state.pop(PARTNER_TRUST, None)
    return content_hash(raw)


def _measure_trust(snapshot, config):
    social = snapshot.data["closed_social"]
    scenario = snapshot.data["closed_economy"]
    households = social["households"]
    focal = households[config.cooperation.household_indices[0]]
    ranking = config.cooperation.trust_ranking
    entries = {e["tx_id"]: e for e in snapshot.data["economic_runtime"]["journal"] if e["kind"] == "cooperation_settlement"}
    expected, scores, effective = [], {}, {}
    for trial in ranking.scheduled_trials:
        slot = scenario[trust_trial_slot(trial.offer_tick)]
        if not ranking.enabled or not slot or slot["status"] != "settled":
            continue
        partner = households[trial.alternate_index]
        tx_id = slot["settlement_tx_id"]
        entry = entries[tx_id]
        if entry["tick"] != trial.offer_tick + 1 or set(entry["payload"]["contributions"]) != {focal, partner}:
            raise ValueError("Trust outcome disagrees with its own settled trial")
        success = entry["payload"]["contributions"][partner] > 0
        root = OUTCOME_PREFIX + tx_id
        before = scores.get(partner, {"a": 1, "b": 1, "used_roots": []})
        after = {"a": before["a"] + int(success), "b": before["b"] + int(not success),
                 "used_roots": [*before["used_roots"], root]}
        expected.append({"kind": PARTNER_UPDATE, "tick": entry["tick"], "effective_tick": entry["tick"] + 1,
                         "actor_id": focal, "candidate_id": partner, "root_claim_id": root, "fact_id": f"{root}:{focal}",
                         "ledger_tx_id": tx_id, "partner_action": "cooperate" if success else "defect",
                         "before": before, "after": after})
        scores[partner] = after
        if entry["tick"] + 1 <= snapshot.tick:
            effective[partner] = after
    recorded = [r for r in social["trace"] if r["kind"] == PARTNER_UPDATE]
    active = {actor: state[PARTNER_TRUST] for actor,state in social["current"].items() if state.get(PARTNER_TRUST)}
    pending = {p["actor_id"]: p["state"][PARTNER_TRUST] for p in social["pending"] if p["state"].get(PARTNER_TRUST)}
    if recorded != expected or active != ({focal: effective} if effective else {}) or pending != ({focal: scores} if scores else {}):
        raise ValueError("Committed partner trust does not match its distinct trial receipts")
    return {"effective_scores": effective, "scheduled_scores": scores, "updates": recorded}


def _checked_store(path, config, terminal_tick):
    store = CommitStore(path)
    economic_run.verify_implementation(store)
    if store.identity.config_hash != content_hash(config):
        raise ValueError("World configuration differs from the frozen assignment")
    if store.identity.metadata.get("terminal_tick") != terminal_tick:
        raise ValueError("World terminal tick differs from the frozen assignment")
    return store


def _measure_partner_forecast(before, snapshot, config, selection, settlement_id):
    if selection["status"] not in ("active", "censored", "fixed_incumbent"):
        return None
    settings = config.cooperation
    choice = selection["choice"]
    focal, candidate = choice["actor_id"], choice["candidate_id"]
    facts = [f for f in before.memories if f.owner_id == focal and not f.public
             and f.visible_at.phase == "OBSERVE" and f.visible_at.tick <= choice["tick"]]
    households = before.data["closed_social"]["households"]
    trials = (t for t in settings.trust_ranking.scheduled_trials if households[t.alternate_index] == candidate)
    score = PartnerPerformanceTrust()
    refs = []
    for trial in trials:
        result = previous_partner_result(facts, trial.offer_tick, candidate)
        if result is None or result.value["partner_action"] not in ("cooperate", "defect"):
            raise ValueError("A forecast needs three private settled trials before selection")
        latest = int(result.value["partner_action"] == "cooperate")
        score = score.consume(result.value["root_claim_id"], latest)
        refs.append(result.fact_id)
    if len(score.used_roots) != 3:
        raise ValueError("The selected candidate forecast requires distinct historical roots")
    outcome = None
    if settlement_id is not None:
        entry = next(e for e in snapshot.data["economic_runtime"]["journal"] if e["tx_id"] == settlement_id)
        outcome = int(entry["payload"]["contributions"][candidate] > 0)
    forecast = {"candidate_id": candidate, "as_of_tick": choice["tick"], "evidence_ids": refs,
                "probabilities": {"cumulative": score.a / (score.a + score.b), "recent": latest},
                "outcome": outcome, "settlement_tx_id": settlement_id}
    if settings.followup.partner_noise_bps is not None:
        c = partner_continuation(settings, households.index(candidate), candidate, AddressedRNG(config.root_seed, config.replica))
        if outcome is not None and outcome != int(c["assigned_action"] == "cooperate"):
            raise ValueError("Settled continuation differs from the registered candidate draw")
        forecast["continuation"] = c
    return forecast


def _forecast_brier(outcomes):
    return {name: fmean((f["probabilities"][name] - f["outcome"]) ** 2 for f in outcomes) if outcomes else None
            for name in ("cumulative", "recent")}


def _summarize_predictions(rows, contrasts, rule):
    ranked = {(r["seed"], r["arm"]): r["partner_forecast"] for r in rows
              if r["status"] == "completed" and r["arm"] in ("t0r1", "t1r1")}
    outcomes, roots = [], []
    for contrast in contrasts:
        seed = contrast["seed"]
        pair = [ranked[seed, arm] for arm in ("t0r1", "t1r1")]
        if all(f is not None and f["outcome"] is not None for f in pair):
            if pair[0]["candidate_id"] == pair[1]["candidate_id"]:
                raise ValueError("Prediction check requires distinct selected candidates in the ranked pair")
            outcomes.extend(pair)
            roots.append(seed)
    return {"rule": rule, "scored_roots": roots, "settled_candidate_outcomes": len(outcomes),
            "brier": _forecast_brier(outcomes)}


def _summarize_noise(rows, comparisons, dose):
    matched = set.intersection(*({c["seed"] for c in pair["contrasts"]} for pair in comparisons.values()))
    offered, paid = {}, {}
    for row in rows:
        if row["seed"] not in matched or not row["cooperation"]["offered"]:
            continue
        forecast = row["partner_forecast"]
        key = row["seed"], forecast["candidate_id"]
        if offered.setdefault(key, forecast["continuation"]) != forecast["continuation"]:
            raise ValueError("Continuation draws differ for the same root and candidate")
        if forecast["outcome"] is not None and paid.setdefault(key, forecast) != forecast:
            raise ValueError("Settled forecasts disagree for the same root and candidate")
    return {"dose_bps": dose, "offered_root_candidates": len(offered),
            "flipped_offered_root_candidates": sum(c["flipped"] for c in offered.values()),
            "scored_roots": sorted({seed for seed, _ in paid}), "settled_candidate_outcomes": len(paid),
            "brier": _forecast_brier(list(paid.values()))}


def _measure(row, store, config, *, switch_control, match_trust=False):
    transfers = config.labor_transfer
    offer_tick = transfers.second_rehire.offer_tick
    work_tick = offer_tick + 1
    choices, choice_snapshot, outcome = [], None, None
    terminal_tick = store.identity.metadata.get("terminal_tick", config.ticks)
    ranking = config.cooperation.trust_ranking if config.cooperation is not None else None
    signal = ranking is not None and config.cooperation.followup.partner_outcome_rule is not None and (
        not ranking.incumbent_trials or config.cooperation.followup.partner_noise_bps is not None)
    selection_snapshot = None
    shared_history = []
    for marker, snapshot in store.states():
        if ranking is not None and marker.tick < config.cooperation.followup.offer_tick - 1:
            shared_history.append(_matched_trust_history(snapshot)
                                  if match_trust or not ranking.incumbent_trials else marker.state_hash)
        if signal and marker.tick == config.cooperation.followup.offer_tick - 1:
            selection_snapshot = snapshot
        audit = audit_snapshot(snapshot) if marker.tick else None
        row["periods"].append(
            {
                "tick": marker.tick,
                "commit_hash": marker.commit_hash,
                "state_hash": marker.state_hash,
                "ledger_audit": audit,
            }
        )
        if marker.tick == offer_tick:
            choice_snapshot = snapshot
            choices = [
                r
                for r in snapshot.data["closed_social"]["trace"]
                if r["kind"] == "rehire_choice" and r["tick"] == offer_tick
            ]
        if marker.tick == work_tick:
            outcome = snapshot
    if marker.tick > terminal_tick:
        raise ValueError("Stored world exceeds its assigned terminal tick")
    if marker.tick < terminal_tick:
        if switch_control and choice_snapshot is not None:
            choice_evidence = _measure_switch_control(choices, choice_snapshot, config, [])
            row.update(choices=choices, random_draw=choice_evidence["random_draw"])
        return
    planned = 1 + len(transfers.second_rehire.alternative_indices)
    if choice_snapshot is not None and (
        len(choices) != planned or len({r["candidate_employer_id"] for r in choices}) != planned
    ):
        raise ValueError("Second window lacks its planned candidate records")
    if not transfers.second_rehire.allow_switch and any(
        r["offer"] and r["offer"]["kind"] == SWITCH_OFFER and r.get("command_id") for r in choices
    ):
        raise ValueError("The fixed-choice policy attempted a switch")
    settlement = outcome if outcome is not None else snapshot
    state = settlement.data["closed_economy"]
    shifts = [s for s in state["labor_events"] if s["tick"] == work_tick] if outcome is not None else []
    contract = state["second_rehire_contract"]
    work = [s for s in shifts if contract and s.get("contract_id") == contract["contract_id"]]
    if len(work) > 1 or (work and work[0]["worker_id"] != choices[0]["actor_id"]):
        raise ValueError("Invalid second-window work denominator")
    fees = [
        e
        for e in settlement.data["economic_runtime"]["journal"]
        if e["kind"] == "labor_switch_fee" and contract and e["payload"]["contract_id"] == contract["contract_id"]
    ]
    reasons = dict(Counter(r["status"] for r in choices))
    control = _measure_switch_control(choices, choice_snapshot, config, work) if switch_control else {}
    if "stopping" in row:
        window_status = "observed"
        if outcome is None:
            window_status = "censored" if choice_snapshot is not None else "not_reached"
        row.update(window_status=window_status, signed_pending_at_stop=int(outcome is None and contract is not None))
    match_tick = offer_tick
    if config.cooperation is not None:
        if config.cooperation.followup is None:
            row["cooperation"] = _measure_cooperation(snapshot)
        else:
            if ranking is None:
                row["first_cooperation"] = _measure_cooperation(snapshot)
            else:
                row["trust_trials"] = [{"plan": t.model_dump(mode="json"),
                                       "outcome": _measure_cooperation(snapshot, trust_trial_slot(t.offer_tick))}
                                      for t in ranking.scheduled_trials]
                row["candidate_trust"] = _measure_trust(snapshot, config)
            row["cooperation"] = _measure_cooperation(snapshot, FOLLOWUP_ROUND) | _measure_feedback(snapshot, config)
            match_tick = config.cooperation.followup.offer_tick
            if config.cooperation.comparison_trial is not None:
                row["comparison_trial"] = _measure_cooperation(snapshot, COMPARISON_ROUND)
    if config.partner_choice:
        partner = _measure_partner(snapshot, config)
        row.update(partner_selection=partner, partner_replaced=partner["replaced"])
        if signal:
            row["partner_forecast"] = _measure_partner_forecast(
                selection_snapshot, snapshot, config, partner, row["cooperation"]["settlement_tx_id"])
        if ranking is not None and ranking.incumbent_trials:
            outcome = row["cooperation"]
            settled_gain = outcome["sponsor_subsidy_minor"] or 0
            outcome.update(settled_rounds=int(outcome["settlement_tx_id"] is not None),
                           settled_players_gain_minor=settled_gain, net_players_gain_minor=settled_gain-partner["fee_minor"])
        match_tick -= 1
    row.update(
        status="completed",
        worked=len(work),
        choices=choices,
        reasons=reasons,
        visible_candidates=sum(r["offer"] is not None for r in choices),
        pretreatment_hash=content_hash(shared_history if ranking is not None else
                                       [r["state_hash"] for r in row["periods"] if r["tick"] < match_tick]),
        opportunity_hash=content_hash([[r["actor_id"], r["candidate_employer_id"], r["offer"]] for r in choices])
        if choice_snapshot is not None
        else None,
        household_count=config.households,
        unemployed_households=config.households - len(shifts) if outcome is not None else None,
        work=work[0] if work else None,
        second_window_fee_minor=sum(e["payload"]["amount_minor"] for e in fees),
        **control,
    )
    if config.partner_choice:
        row["opportunity_hash"] = partner["opportunity_hash"]


def _measure_switch_control(choices, snapshot, config, work):
    window = config.labor_transfer.second_rehire
    recorded = None
    if choices:
        worker = choices[0]["actor_id"]
        bundle = next(d for d in snapshot.decisions if d.actor_id == worker and d.tick == snapshot.tick)
        recorded = bundle.belief_patch.get(LABOR_DRAW)
    visible = [r for r in choices if r["offer"] is not None]
    alternatives = [r for r in visible if r["offer"]["kind"] == SWITCH_OFFER]
    eligible = alternatives if window.switch_only else visible
    offers = [r["offer"]["offer_id"] for r in eligible]
    attempts = [r for r in choices if r.get("command_id")]
    if len(attempts) > 1:
        raise ValueError("Second window exceeded one offer attempt")
    expected = None
    if window.random_offer_choice and offers:
        expected = labor_random_draw(
            AddressedRNG(config.root_seed, config.replica), config, worker, snapshot.tick, offers
        )
    if window.switch_only or expected is not None:
        selected = None
        if expected is not None:
            selected = expected["selected_offer_id"]
        elif eligible:
            ranked = min(eligible, key=lambda r: labor_wage_rank(r["offer"]["offer_id"], r["score"]))
            selected = ranked["offer"]["offer_id"]
        if [r["offer"]["offer_id"] for r in attempts] != ([selected] if selected is not None else []):
            raise ValueError("Policy did not attempt exactly the assigned visible offer")
    if recorded != expected:
        raise ValueError("Committed random draw disagrees with the registered candidate set and address")
    switches = [r for r in attempts if r["offer"]["kind"] == SWITCH_OFFER]
    chosen = switches[0] if switches else None
    return {
        "random_draw": recorded,
        "switch_offer_visible": len(alternatives),
        "switch_attempted": len(switches),
        "switch_accepted": sum(r["status"] == "accepted" for r in switches),
        "switch_worked": sum(bool(work) and r["command_id"] == work[0]["contract_id"] for r in switches),
        **(
            {
                "chosen_employer": chosen["candidate_employer_id"] if chosen else None,
                "chosen_score": chosen["score"] if chosen else None,
            }
            if window.switch_only
            else {}
        ),
    }


def _frozen_plan(root):
    receipt = read_json(root / "plan.json")
    plan = receipt["plan"]
    if receipt["plan_hash"] != content_hash(plan):
        raise ValueError("Frozen pilot plan checksum mismatch")
    spec = LaborPilotSpec.model_validate(plan["spec"])
    arms, analysis = _protocol(spec)
    if plan["analysis"] != analysis or plan["arms"] != arms:
        raise ValueError("Pilot analysis or treatment contract changed")
    stopping = _stopping_schedule(spec)
    if plan.get("stopping", {}) != stopping:
        raise ValueError("Frozen stopping schedule differs from its common random addresses")
    before = plan["implementation"]
    now = economic_run.current_implementation()
    if any(before[key] != now[key] for key in ("source_hash", "dependency_hash")):
        raise ValueError("Pilot implementation identity changed")
    return receipt["plan_hash"], spec, stopping


def _compare_static_partners(rows, seeds):
    measured = [r for r in rows if r["status"] == "completed"]
    offers = {(r["seed"], r["arm"]): r["cooperation"]["offered"] for r in measured}
    opportunity_mismatches = [seed for seed in seeds
                              if len({offers[seed, a] for a in PARTNER_STATIC_ARMS if (seed, a) in offers}) > 1]
    dynamic_mismatches = _exchange_mismatches(rows, seeds, ("ranked", "uniform"))
    comparisons = {}
    for pair in (("uniform", "static"), ("ranked", "uniform"), ("ranked", "static")):
        mismatches = set(opportunity_mismatches)
        if pair == ("ranked", "uniform"):
            mismatches.update(dynamic_mismatches)
        comparisons[f"{pair[0]}_minus_{pair[1]}"] = summarize_cooperation(
            [r for r in rows if r["arm"] in pair], seeds, arms=pair, match_selection=True, mismatches=mismatches)
    return {"comparisons": comparisons, "opportunity_count_mismatches": opportunity_mismatches,
            "dynamic_exchange_mismatches": dynamic_mismatches}


def _report(root, plan_hash, spec, stopping, *, open_store=_checked_store):
    arms, analysis = _protocol(spec)
    switch_control = spec.schema_version > 1
    last_attempt = {row["job"]: row for row in registry.history(root)}
    rows = []
    for seed, arm, config in _jobs(spec):
        assignment = stopping.get(str(seed))
        job = f"seed-{seed}/{arm}"
        folder = root / job
        attempt = last_attempt.get(job, {})
        row = {
            "seed": seed,
            "arm": arm,
            "status": attempt.get("status", "not_run"),
            "worked": None,
            "relative_path": job,
            "periods": [],
            "config_hash": content_hash(config),
            **({"stopping": assignment} if assignment is not None else {}),
        }
        if row["status"] in ("running", "finished"):
            row["status"] = "incomplete"
        if "error" in attempt:
            row["error"] = attempt["error"]
        try:
            if folder.exists():
                store = open_store(folder, config, assignment["tick"] if assignment is not None else None)
                _measure(row, store, config, switch_control=switch_control, match_trust=spec.schema_version == 14)
                if row["status"] != "completed" and row["status"] == "not_run":
                    row["status"] = "incomplete"
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        rows.append(row)
    exchange_mismatches = []
    if spec.schema_version == 14:
        d1 = summarize_d1(rows, spec.seeds, spec.base_config.cooperation.followup.offer_tick)
        summary = {"d1": d1}
        matched = len(spec.seeds) - len(d1["incomplete_roots"]) - len(d1["mechanism_mismatches"])
    elif spec.schema_version >= 12:
        summary = _compare_static_partners(rows, spec.seeds)
        if spec.schema_version == 13:
            dose = spec.base_config.cooperation.followup.partner_noise_bps
            summary["noise_check"] = _summarize_noise(rows, summary["comparisons"], dose)
        exchange_mismatches = summary["opportunity_count_mismatches"] + summary["dynamic_exchange_mismatches"]
        matched = min(r["matched_pairs"] for r in summary["comparisons"].values())
    elif spec.schema_version >= 10:
        interaction = summarize_interaction(rows, spec.seeds, partner=True)
        summary = {"interaction": interaction}
        if spec.schema_version == 11:
            rule = spec.base_config.cooperation.followup.partner_outcome_rule
            summary["predictive_check"] = _summarize_predictions(rows, interaction["contrasts"], rule)
        exchange_mismatches = interaction["exchange_mismatches"]
        matched = interaction["matched_quartets"]
    elif spec.schema_version >= 8:
        summary = {"comparison": summarize_cooperation(rows, spec.seeds, arms=tuple(arms), match_selection=True)}
        exchange_mismatches = _exchange_mismatches(rows, spec.seeds, tuple(arms))
        summary["comparison"]["exchange_mismatches"] = exchange_mismatches
        matched = summary["comparison"]["matched_pairs"]
    elif spec.schema_version == 7:
        summary = {"comparison": summarize_switch_control(rows, spec.seeds, arms=tuple(arms),
                                                           outcome="partner_replaced")}
        matched = summary["comparison"]["matched_pairs"]
    elif spec.schema_version >= 5:
        summary = {"comparison": summarize_cooperation(rows, spec.seeds, arms=tuple(arms))}
        matched = summary["comparison"]["matched_pairs"]
    elif switch_control:
        summary = {"comparison": summarize_switch_control(rows, spec.seeds)}
        matched = summary["comparison"]["matched_pairs"]
    else:
        summary = {"interaction": summarize_interaction(rows, spec.seeds)}
        matched = summary["interaction"]["matched_quartets"]
    counts = {}
    for arm in arms:
        completed = [r for r in rows if r["arm"] == arm and r["status"] == "completed"]
        counts[arm] = {
            "assigned_worlds": len(spec.seeds),
            "completed_worlds": len(completed),
            "visible_candidates": sum(r["visible_candidates"] for r in completed),
            "no_visible_offer_worlds": sum(
                r.get("window_status") != "not_reached" and r["visible_candidates"] == 0 for r in completed
            ),
            "worked_worlds": sum(r["worked"] for r in completed),
            "second_window_fee_minor": sum(r["second_window_fee_minor"] for r in completed),
        }
        if switch_control:
            counts[arm]["allocated_switch_slots"] = len(spec.seeds)
            for field in ("switch_offer_visible", "switch_attempted", "switch_accepted", "switch_worked"):
                counts[arm][field] = sum(r[field] for r in completed)
        if spec.schema_version >= 3:
            counts[arm]["worlds_with_two_alternatives"] = sum(r["switch_offer_visible"] == 2 for r in completed)
            counts[arm]["chosen_employers"] = dict(
                Counter(r["chosen_employer"] for r in completed if r["chosen_employer"])
            )
        if stopping:
            counts[arm]["window_status_counts"] = dict(Counter(r["window_status"] for r in completed))
            counts[arm]["signed_pending_at_stop"] = sum(r["signed_pending_at_stop"] for r in completed)
        if spec.schema_version >= 5:
            outcomes = [r["cooperation"] for r in completed]
            cooperation = {
                key: sum(r[key] or 0 for r in outcomes)
                for key in ("offered", "cooperative_choices", "defections", "declines", "unanswered", "sponsor_subsidy_minor")
            }
            offered = cooperation["offered"]
            cooperation.update(
                assigned_seats=2 * len(spec.seeds),
                observed_assigned_seats=2 * len(outcomes),
                cooperation_rate=cooperation["cooperative_choices"] / offered if offered else None,
                settlement_status_counts=dict(Counter(r["settlement_status"] for r in outcomes)),
            )
            if spec.schema_version >= 6:
                for key in ("feedback_visible_seats", "feedback_uses"):
                    cooperation[key] = sum(r[key] for r in outcomes)
            if spec.schema_version >= 12:
                for key in ("settled_rounds", "settled_players_gain_minor", "net_players_gain_minor"):
                    cooperation[key] = sum(r[key] for r in outcomes)
            counts[arm]["cooperation"] = cooperation
        if spec.schema_version >= 7:
            selections = [r["partner_selection"] for r in completed]
            counts[arm]["partner_selection"] = {
                "allocated_choosers": len(spec.seeds),
                "attempted": sum(r["choice"]["attempted"] for r in selections if r["choice"]),
                "accepted_exchanges": sum(r["choice"]["status"] == "scheduled" for r in selections if r["choice"]),
                "replaced": sum(r["replaced"] for r in selections),
                "fee_minor": sum(r["fee_minor"] for r in selections),
                "status_counts": dict(Counter(r["status"] for r in selections)),
            }
            if spec.schema_version >= 8:
                counts[arm]["partner_selection"]["active_alternates"] = dict(
                    Counter(r["active_partner"] for r in selections if r["replaced"]))
    return {
        "status": "completed" if matched == len(spec.seeds) and not exchange_mismatches else "failed",
        "plan_hash": plan_hash,
        "experiment_id": analysis["id"],
        "expected_worlds": len(spec.seeds) * len(arms),
        "arms": counts,
        "runs": rows,
        **summary,
    }


def analyze_pilot(root):
    """Recompute result from pinned run chains and retained execution receipts."""
    root = Path(root)
    with registry.run_lock(root):
        plan_hash, spec, stopping = _frozen_plan(root)
        return _report(root, plan_hash, spec, stopping)


def _fee_outcome(row, expected_fee):
    if row["status"] != "completed":
        return {"status": row["status"], "net_minor": None, **({"error": row["error"]} if "error" in row else {})}
    partner, cooperation = row["partner_selection"], row["cooperation"]
    choice = partner["choice"]
    fee, gain = partner["fee_minor"], cooperation["settled_players_gain_minor"]
    if fee != expected_fee * partner["replaced"] or not 0 <= gain <= COOPERATION_MAX_SUBSIDY_MINOR:
        raise ValueError("Committed fee or cooperation gain differs from the registered terms")
    return {"status": row["status"], "selection": partner["status"],
            "admitted": choice["offers"] is not None if choice is not None else None,
            "seats_offered": cooperation["offered"], "active_partner": partner["active_partner"],
            "cooperative_choices": cooperation["cooperative_choices"], "settlement": cooperation["settlement_status"],
            "settled_gain_minor": gain, "paid_fee_minor": fee, "net_minor": gain - fee}


def _fee_effects(low, high, seeds, fees, arm):
    by_cohort = [{r["seed"]: r for r in report["runs"] if r["arm"] == arm} for report in (low, high)]
    actual_fees = (0, 0) if arm == "static" else fees
    roots, complete, matched = [], [], []
    lower = upper = 0
    for seed in seeds:
        before, after = (rows[seed] for rows in by_cohort)
        a, b = (_fee_outcome(row, fee) for row, fee in zip((before, after), actual_fees, strict=True))
        intervals = [(net, net) if (net := value["net_minor"]) is not None
                     else (-fee, COOPERATION_MAX_SUBSIDY_MINOR)
                     for value, fee in zip((a, b), actual_fees, strict=True)]
        (a_min, a_max), (b_min, b_max) = intervals
        lower += b_min - a_max
        upper += b_max - a_min
        delta = None
        opportunity = "technical_missing"
        if a["net_minor"] is not None and b["net_minor"] is not None:
            if before["pretreatment_hash"] != after["pretreatment_hash"]:
                raise ValueError(f"Fee pair has different pretreatment state for seed {seed}, arm {arm}")
            delta = b["net_minor"] - a["net_minor"]
            complete.append(delta)
            if a["seats_offered"] == b["seats_offered"] == 0:
                opportunity = "no_offers"
            elif a["seats_offered"] == b["seats_offered"] == 2 and a["active_partner"] == b["active_partner"]:
                opportunity = "matched_offered"
                matched.append(delta)
            else:
                opportunity = "changed"
        roots.append({"seed": seed, "stop_tick": before["stopping"]["tick"], "opportunity": opportunity,
                      "low": a, "high": b, "high_minus_low_minor": delta})
    n = len(seeds)
    return {"assigned_roots": n, "complete_roots": len(complete), "matched_offered_roots": len(matched),
            "all_assigned_high_minus_low": _difference_stats(complete if len(complete) == n else []),
            "all_assigned_bounds": [lower / n, upper / n],
            "matched_offered_high_minus_low": _difference_stats(matched), "roots": roots}


def compare_partner_fees(low, high):
    """Compare two v13 plans that assign the same worlds and differ only in the fee."""
    low, high = Path(low).resolve(), Path(high).resolve()
    if low == high:
        raise ValueError("Fee comparison needs two different pilot directories")
    with registry.run_lock(low), registry.run_lock(high):
        left, right = _frozen_plan(low), _frozen_plan(high)
        low_spec, high_spec = left[1], right[1]
        if low_spec.schema_version != 13 or high_spec.schema_version != 13:
            raise ValueError("Fee comparison requires two version 13 pilots")
        low_input, high_input = (spec.model_dump(mode="json") for spec in (low_spec, high_spec))
        fees = tuple(raw["base_config"]["cooperation"]["followup"]["partner_choice"].pop("fee_minor")
                     for raw in (low_input, high_input))
        if low_input != high_input or fees[0] >= fees[1]:
            raise ValueError("Pilot plans must differ only in the registered partner fee, low fee first")
        reports = _report(low, *left), _report(high, *right)
    return {"status": "completed" if all(r["status"] == "completed" for r in reports) else "failed",
            "experiment_id": "matched-partner-fee-range.v1", "unit": "minor currency units per assigned root-world",
            "endpoint": "settled cooperation gain of both participants minus the paid exchange fee",
            "low": {"fee_minor": fees[0], "plan_hash": left[0]}, "high": {"fee_minor": fees[1], "plan_hash": right[0]},
            "arms": {arm: _fee_effects(*reports, low_spec.seeds, fees, arm) for arm in PARTNER_STATIC_ARMS}}


async def _run_assignment(root, seed, arm, config, terminal_tick):
    job = f"seed-{seed}/{arm}"
    folder = root / job
    try:
        if folder.exists():
            head = _checked_store(folder, config, terminal_tick).verified_markers()[-1]
            if head.tick == (terminal_tick if terminal_tick is not None else config.ticks):
                return
        else:
            economic_run.create_run(config, folder, run_id=f"seed-{seed}:{arm}", terminal_tick=terminal_tick)
        registry.record(root, {"job": job, "status": "running"})
        await economic_run.resume_run(folder)
        registry.record(root, {"job": job, "status": "finished"})
    except Exception as exc:
        registry.record(root, {"job": job, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})


def _run_assignment_process(job):
    root, seed, arm, raw_config, terminal_tick = job
    asyncio.run(_run_assignment(root, seed, arm, ClosedEconomyConfig.model_validate(raw_config), terminal_tick))


async def run_pilot(root, spec=None, *, workers=1):
    """Create a fresh pilot or resume only unfinished worlds in an existing one."""
    root = Path(root)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("Pilot workers must be between 1 and 8")
    if spec is not None:
        spec = LaborPilotSpec.model_validate(spec)
        arms, analysis = _protocol(spec)
        stopping = _stopping_schedule(spec)
        plan = {
            "spec": spec.model_dump(mode="json"),
            "analysis": analysis,
            "arms": arms,
            "implementation": economic_run.current_implementation(),
            "external_api_calls": 0,
            **({"stopping": stopping} if stopping else {}),
        }
        root.mkdir(parents=True, exist_ok=False)
        atomic_json(root / "plan.json", {"plan_hash": content_hash(plan), "plan": plan})
    with registry.run_lock(root):
        plan_hash, spec, stopping = _frozen_plan(root)
        jobs = ((root, seed, arm, config, stopping.get(str(seed), {}).get("tick"))
                for seed, arm, config in _jobs(spec))
        if workers == 1:
            for job in jobs:
                await _run_assignment(*job)
        else:
            payloads = ((path, seed, arm, config.model_dump(mode="json"), tick)
                        for path, seed, arm, config, tick in jobs)
            with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
                for _ in pool.map(_run_assignment_process, payloads):
                    pass
        report = _report(root, plan_hash, spec, stopping)
        atomic_json(root / "report.json", report)
        return report
