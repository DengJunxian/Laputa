#!/usr/bin/env python3
"""Grouped calibration of the observed cooperation propensity."""
import argparse
from collections import defaultdict
import csv
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import re

import numpy as np
from scipy.special import expit
from scipy.stats import bootstrap
from statsmodels.genmod.families import Binomial
from statsmodels.genmod.generalized_linear_model import GLM


FEATURES = {"constant": ["intercept"], "own_previous": ["intercept", "prior_choice"],
            "own_previous_and_lag_offer": ["intercept", "prior_choice", "prior_offer_balance"]}


def parse_rows(source):
    rows, group_visibility = {}, {}
    exclusions = defaultdict(int)
    for raw in csv.DictReader(source):
        node = raw["node_id"]
        m = re.fullmatch(r"([1-9][0-9]*)_p[0-9]+", node)
        tick = int(raw["round"])
        choice, visibility = raw["decision"], raw["visibility"]
        cooperate, defect = (int(raw[c]) for c in ("offered_make_coop", "offered_make_defect"))
        if (not m or not 1 <= tick <= 10 or (node, tick) in rows or choice not in ("0", "1", "NA")
                or visibility not in ("0", "1") or min(cooperate, defect) < 0):
            raise ValueError("Invalid, duplicate or conflicting node/round row")
        group = int(m[1])
        if group_visibility.setdefault(group, visibility) != visibility:
            raise ValueError("Visibility changed within a game session")
        rows[node, tick] = group, choice, (cooperate-defect)/max(1, cooperate+defect)
    observations = []
    for (node, tick), (group, choice, _) in sorted(rows.items()):
        if tick == 1:
            exclusions["round_1_no_previous"] += 1
        elif choice == "NA":
            exclusions["missing_current_choice"] += 1
        elif (previous := rows.get((node, tick-1))) is None or previous[1] == "NA":
            exclusions["missing_previous_choice"] += 1
        else:
            observations.append((group, int(choice), 1., float(previous[1]), previous[2]))
    return observations, group_visibility, {"raw_rows": len(rows), "nodes":len({node for node, _ in rows}),
           "missing_choice_rows_in_raw":sum(r[1] == "NA" for r in rows.values()), "excluded":dict(exclusions)}


def split_sessions(groups, registered):
    counts = registered["split_groups_per_visibility"]
    out = {"calibration": [], "development": [], "holdout": []}
    for vis in ("0", "1"):
        order = sorted((g for g in groups if groups[g] == vis),
                       key=lambda g: sha256(f"{registered['split_namespace']}/{vis}/{g}".encode()).digest())
        if len(order) != sum(counts):
            raise ValueError("Unexpected set of source sessions")
        start = 0
        for (name, sessions), count in zip(out.items(), counts, strict=True):
            sessions.extend(order[start:start+count])
            start += count
    return {stage:sorted(v) for stage,v in out.items()}


def fit(x, y, groups=None):
    rank = int(np.linalg.matrix_rank(x))
    if rank != x.shape[1] or len(np.unique(y)) != 2:
        raise ValueError("Choice model is not identifiable in this design")
    model = GLM(y, x, family=Binomial(), missing="raise")
    used = model.fit() if groups is None else model.fit(cov_type="cluster", cov_kwds={"groups":groups})
    if not used.converged or not np.isfinite(used.params).all():
        raise ValueError("Unresolved cooperation model fit")
    weights = used.fittedvalues*(1-used.fittedvalues)
    information = x.T @ (weights[:, None]*x)
    return {"parameters":used.params.tolist(),"se":used.bse.tolist(),
            "n":len(y),"design_rank":rank,"information_condition_number":float(np.linalg.cond(information)),
            "information_min_eigenvalue":float(np.linalg.eigvalsh(information)[0])}


def _score(array, fitted, candidate):
    width = len(FEATURES[candidate])
    predicted = expit(array[:,2:2+width] @ fitted["parameters"])
    target = array[:,1]
    safe = np.clip(predicted,1e-12,1-1e-12)
    rows = {}
    for group in sorted(set(array[:,0])):
        selected = array[:,0] == group
        p, y = predicted[selected], target[selected]
        logs = -(target*np.log(safe)+(1-target)*np.log1p(-safe))
        rows[int(group)] = {"n":int(selected.sum()), "brier":float(np.mean((p-y)**2)),
                            "logloss":float(np.mean(logs[selected])),
                            "observed_cooperation":float(np.mean(y)), "predicted_cooperation":float(np.mean(p))}
    return {"n":len(array), "sessions":rows,
            **{k:float(np.mean([row[k] for row in rows.values()]))
               for k in ("brier","logloss","observed_cooperation","predicted_cooperation")}}


def recovery(spec):
    truth = np.array(spec["parameters"])
    coefficients = []
    for seed in range(spec["first_seed"], spec["first_seed"]+spec["replicates"]):
        rng = np.random.default_rng(seed)
        n = spec["rows_per_replicate"]
        x = np.column_stack([np.ones(n), rng.integers(0,2,n), rng.uniform(-1,1,n)])
        y = rng.binomial(1,expit(x@truth))
        coefficients.append(fit(x,y)["parameters"])
    errors = np.asarray(coefficients) - truth
    rmse = np.sqrt(np.mean(errors**2,axis=0))
    return {"truth":truth.tolist(), "replicates":len(coefficients), "rmse":rmse.tolist(),
            "bias":np.mean(errors,axis=0).tolist(), "estimates":coefficients,
            "gate_passed":bool(np.max(rmse)<=spec["maximum_rmse"])}


def evaluate(path, registered):
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != registered["dataset"]["sha256"] or path.stat().st_size != registered["dataset"]["bytes"]:
        raise ValueError("Raw dataset hash or byte count differs from frozen release")
    with path.open(newline="") as source:
        rows, groups, waterline = parse_rows(source)
    splits = split_sessions(groups, registered)
    array = np.asarray(rows)
    fitting, development, holdout = (array[np.isin(array[:,0], split)] for split in splits.values())
    fitted = {name:fit(fitting[:,2:2+len(FEATURES[name])], fitting[:,1], fitting[:,0])
              for name in registered["candidates"]}
    development_scores = {name:_score(development, model, name) for name,model in fitted.items()}
    selected = min(development_scores, key=lambda name:development_scores[name]["brier"])
    constant = _score(holdout, fitted["constant"], "constant")
    prediction = _score(holdout, fitted[selected], selected)
    gaps = np.asarray([prediction["sessions"][g]["brier"]-constant["sessions"][g]["brier"]
                       for g in splits["holdout"]])
    interval = [0., 0.]
    if np.any(gaps):
        ci = bootstrap((gaps,), np.mean, method="percentile", n_resamples=10000,
                       rng=np.random.default_rng(44006)).confidence_interval
        interval = [float(ci.low),float(ci.high)]
    return {"dataset":registered["dataset"],"spec":registered,"raw":waterline,"splits":splits,
            "candidate_features":FEATURES,"fitted":fitted,"development_scores":development_scores,
            "chosen_model":selected,"holdout":{"chosen":prediction,"constant":constant,
                 "chosen_minus_constant_brier":float(gaps.mean()),"session_bootstrap_ci95":interval},
            "synthetic_parameter_recovery":recovery(registered["synthetic_recovery"]),
            "software":{name:version(name) for name in ("numpy","scipy","statsmodels","pandas")},
            "scope":"predictive association on observed own transitions in network public goods; economic trust and the causal effect of rewiring fees are not identified"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec",type=Path,required=True)
    parser.add_argument("--raw",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    report=evaluate(args.raw,json.loads(args.spec.read_text()))
    with args.output.open("x") as output:
        json.dump(report,output,indent=2,allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()
