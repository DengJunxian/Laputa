"""D1 estimands for a funded, one-slot network exchange in the late window."""
from statistics import covariance, fmean, stdev

D1_ARMS = {"t0r0": [False, False], "t0r1": [False, True],
           "t1r0": [True, False], "t1r1": [True, True], "uniform": [True, True]}
D1_ANALYSIS = {
    "id": "d1-funded-late-slot.v1",
    "unit": "root_seed_world",
    "factors": ["cumulative_private_trial_trust_vs_latest_result", "selective_exchange_vs_fixed_graph"],
    "extra_control": "addressed uniform exchange among the same two candidates; one realized swap and same fee",
    "primary": "late-window cooperative actions / eligible offered seats, within shared exogenous risk set",
    "contrast": "t1r1 - t1r0 - t0r1 + t0r0",
    "secondary": "cooperative choices per two assigned late-window seats; natural no-offer contributes zero",
    "missing": "retain all roots; bound missing eligible arm shares in [0,1] at risk; full assignment separately",
    "scope": "one late bilateral offer after three directly settled trials per candidate; first graph layer",
}
_SIGNS = {"t0r0": 1, "t0r1": -1, "t1r0": -1, "t1r1": 1}


def _stats(values):
    n = len(values)
    sd = stdev(values) if n > 1 else None
    return {"n": n, "mean": fmean(values) if n else None,
            "sd": sd, "mcse": sd / n**0.5 if sd is not None else None}


def _bounds(values):
    if not values:
        return [None, None]
    return [fmean(v[i] for v in values) for i in (0, 1)]


def summarize_d1(rows, seeds, offer_tick):
    """Reduce one outcome per root and arm; keep non-offers and failures visible."""
    by_key = {(r["seed"], r["arm"]): r for r in rows}
    if (not seeds or len(set(seeds)) != len(seeds) or len(by_key) != len(rows)
            or any(seed not in seeds or arm not in D1_ARMS for seed, arm in by_key)):
        raise ValueError("Invalid D1 assignments")
    assigned, eligible, uniform, blocks = [], [], [], []
    bounds, risk_bounds = [], []
    pre_offer_stops, no_offers, incomplete, mismatches = [], [], [], []
    for seed in seeds:
        block = {a: by_key.get((seed, a), {}) for a in D1_ARMS}
        stops = {r["stopping"]["tick"] for r in block.values() if "stopping" in r}
        if len(stops) > 1:
            raise ValueError("D1 worlds disagree on the exogenous stopping address")
        stop = next(iter(stops), None)
        completed = {a: r for a, r in block.items() if r.get("status") == "completed"}
        if len(completed) != len(block):
            incomplete.append(seed)
        outcomes = {a: r["cooperation"] for a, r in completed.items()}
        for value in outcomes.values():
            count, seats = value["cooperative_choices"], value["offered"]
            if type(count) is not int or seats not in (0, 2) or not 0 <= count <= seats:
                raise ValueError("Invalid D1 choice and opportunity counts")
        primary_rows = [completed[a] for a in _SIGNS if a in completed]
        mismatch = (len({r["pretreatment_hash"] for r in primary_rows}) > 1
                    or len({r["opportunity_hash"] for r in primary_rows}) > 1
                    or len({outcomes[a]["offered"] for a in _SIGNS if a in outcomes}) > 1)
        control = (completed.get("t1r1", {}), completed.get("uniform", {}))
        if all("partner_selection" in r for r in control):
            mismatch |= (control[0]["pretreatment_hash"] != control[1]["pretreatment_hash"]
                         or control[0]["opportunity_hash"] != control[1]["opportunity_hash"]
                         or any(control[0]["partner_selection"][k] != control[1]["partner_selection"][k]
                                for k in ("fee_minor", "replaced"))
                         or outcomes["t1r1"]["offered"] != outcomes["uniform"]["offered"])
        if mismatch:
            mismatches.append(seed)
        early = stop is not None and stop < offer_tick
        if early:
            pre_offer_stops.append(seed)
        choices = {a: outcomes[a]["cooperative_choices"] / 2 for a in _SIGNS if a in outcomes}
        full = len(choices) == len(_SIGNS) and not mismatch
        low = sum(sign * choices[a] if a in choices else min(0, sign) for a, sign in _SIGNS.items())
        high = sum(sign * choices[a] if a in choices else max(0, sign) for a, sign in _SIGNS.items())
        interval = [-2., 2.] if mismatch else [low, high]
        bounds.append(interval)
        if full:
            on = choices["t1r1"] - choices["t0r1"]
            off = choices["t1r0"] - choices["t0r0"]
            assigned.append(on - off)
            if outcomes["t1r1"]["offered"] == 2:
                if early:
                    raise ValueError("D1 offer predates the registered opportunity window")
                eligible.append({"seed": seed, "delta": on - off, "trust_when_dynamic": on,
                                 "trust_when_static": off, "rates": choices})
                if "uniform" in outcomes and outcomes["uniform"]["offered"] == 2:
                    uniform.append(choices["t1r1"] - outcomes["uniform"]["cooperative_choices"]/2)
            elif not early:
                no_offers.append(seed)
        if not early and seed not in no_offers:
            risk_bounds.append(interval)
        blocks.append({"seed": seed, "stop_tick": stop, "observed_choices_by_arm": choices,
                       "offers_by_arm": {a: r["offered"] for a, r in outcomes.items()},
                       "assigned_interaction_bounds": interval})
    return {"blocks": blocks, "pre_offer_stops": pre_offer_stops, "unoffered_at_risk_roots": no_offers,
            "incomplete_roots": incomplete, "mechanism_mismatches": mismatches,
            "assigned": _stats(assigned) | {"all_assigned_bounds": _bounds(bounds)},
            "eligible": _stats([r["delta"] for r in eligible]) | {
                "roots": [r["seed"] for r in eligible], "contrasts": eligible,
                "all_at_risk_bounds": _bounds(risk_bounds),
                "simple_effect_covariance": covariance([r["trust_when_dynamic"] for r in eligible],
                                                       [r["trust_when_static"] for r in eligible])
                if len(eligible) > 1 else None},
            "selective_minus_same_count_uniform": _stats(uniform)}
