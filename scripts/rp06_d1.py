#!/usr/bin/env python3
"""Freeze D1 confirmation size from its pilot and read root-level inference."""
import argparse
from hashlib import sha256
from importlib.metadata import version
import json
from math import ceil, sqrt
from pathlib import Path

from scipy.stats import binomtest, chi2, norm, ttest_1samp
from statsmodels.stats.multitest import multipletests

from civitas.contracts.base import content_hash
from civitas.experiments.labor_factorial import LaborPilotSpec


def read(path):
    return json.loads(path.read_text())


def write(path, data):
    with path.open("x") as output:
        json.dump(data, output, indent=2, allow_nan=False)
        output.write("\n")


def roots(pair):
    return list(range(pair[0], pair[1]+1))


def validated_report(root):
    receipt = read(root/"plan.json")
    frozen = receipt["plan"]
    report = read(root/"report.json")
    spec = LaborPilotSpec.model_validate(frozen["spec"])
    if (receipt["plan_hash"] != content_hash(frozen) or report["plan_hash"] != receipt["plan_hash"]
            or spec.schema_version < 13):
        raise ValueError("Study receipt does not match the frozen pilot plan")
    return spec, report, frozen["implementation"]["source_hash"]


def prepare(registered, pilot_spec, pilot):
    spec, report, runtime = validated_report(pilot)
    if spec != pilot_spec or list(spec.seeds) != roots(registered["pilot_root_range"]):
        raise ValueError("Pilot was not registered")
    d1 = report["d1"]
    eligible = d1["eligible"]
    if (report["status"] != "completed" or eligible["n"] < registered["minimum_eligible_pilot_roots"]
            or d1["incomplete_roots"] or d1["mechanism_mismatches"]):
        raise ValueError("Pilot cannot estimate the registered sample size")
    alpha = registered["alpha"]
    tail = 1 - registered["pilot_sd_upper_confidence"]
    sd_floor = registered["minimum_planning_sd_rate"]
    df = eligible["n"]-1
    sd = max(sd_floor, eligible["sd"] * sqrt(df / chi2.ppf(tail, df)))
    lower_rate = binomtest(eligible["n"],len(spec.seeds)).proportion_ci(
        confidence_level=registered["eligible_fraction_lower_confidence"], method="wilson").low
    power = ((norm.ppf(1-alpha)+norm.ppf(registered["power"]))*sd/registered["important_effect_rate"])**2
    precision = (norm.ppf(1-alpha/2)*sd/registered["allowed_mc_halfwidth_rate"])**2
    pairs = ceil(max(power,precision))
    block = registered["counterbalance_block_size"]
    needed = max(registered["confirmation_minimum_roots"],ceil(pairs/lower_rate/block)*block)
    pool = roots(registered["confirmation_root_pool"])
    if set(pool) & set(spec.seeds) or len(pool) != registered["confirmation_cap_roots"]:
        raise ValueError("Confirmation pool overlaps or differs from preregistration")
    selected = pool[:min(len(pool),needed)]
    raw = spec.model_dump(mode="json")
    raw["seeds"], raw["base_config"]["root_seed"] = selected, selected[0]
    return {"spec":raw,"pilot_plan_hash":report["plan_hash"],"runtime_source_hash":runtime,
            "eligible_pilot_roots":eligible["n"], "pilot_sd":eligible["sd"],"planning_sd_upper":sd,
            "wilson_lower_eligible_fraction":float(lower_rate),
            "planned_eligible_roots":pairs,"required_allocated_roots":needed,"allocated_roots":len(selected),
            "resource_cap_reached":needed>len(selected)}


def one_sample(values, *, alternative="two-sided"):
    if len(values)<2:
        return {"p_value":None,"ci95":None,"issue":"insufficient independent roots"}
    if min(values)==max(values):
        return {"p_value":None,"ci95":None,"issue":"zero observed variance on the registered t test"}
    test = ttest_1samp(values,0,alternative=alternative)
    ci = ttest_1samp(values,0).confidence_interval(.95)
    return {"p_value":float(test.pvalue),"ci95":[float(ci.low),float(ci.high)],"df":len(values)-1}


def confirm(plan, registered, root):
    spec, report, source = validated_report(root)
    if spec.model_dump(mode="json") != plan["spec"] or source != plan["runtime_source_hash"]:
        raise ValueError("Confirmation differs from the frozen pilot-based plan")
    d1 = report["d1"]
    eligible = d1["eligible"]
    allowed = report["status"] == "completed" and not d1["mechanism_mismatches"] and not d1["incomplete_roots"]
    outcome = (one_sample([r["delta"] for r in eligible["contrasts"]], alternative="greater") if allowed else
               {"p_value":None, "ci95":None, "issue":"incomplete worlds or failed mechanism gate"})
    adjusted = multipletests([outcome["p_value"]], alpha=registered["alpha"], method="holm")[1][0] if (
        allowed and outcome["p_value"] is not None) else None
    halfwidth = (outcome["ci95"][1]-outcome["ci95"][0])/2 if outcome["ci95"] else None
    precision = halfwidth is not None and halfwidth <= registered["allowed_mc_halfwidth_rate"]
    return {"primary":outcome | {"holm_p":adjusted, "interpretation":
            "positive_on_registered_synthetic_test" if adjusted is not None and adjusted < registered["alpha"]
            else "inconclusive", "ci_halfwidth":halfwidth, "precision_target_met":precision,
            "required_eligible_roots_met":eligible["n"]>=plan["planned_eligible_roots"]},
            "confirmation_plan":plan,"source_plan_hash":report["plan_hash"],
            "allocated_roots":len(spec.seeds), "experiment":d1,"arm_denominators":report["arms"],
            "statistics_versions":{n:version(n) for n in ("numpy","scipy","statsmodels")}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol",type=Path,required=True)
    parser.add_argument("--pilot",type=Path,required=True)
    parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--spec-output",type=Path)
    parser.add_argument("--confirmation",type=Path)
    parser.add_argument("--result",type=Path)
    args=parser.parse_args()
    config=read(args.protocol)
    pilot_spec=LaborPilotSpec.model_validate(read(args.protocol.parent/config["pilot_spec"]))
    plan=prepare(config,pilot_spec,args.pilot)
    plan["protocol_sha256"]=sha256(args.protocol.read_bytes()).hexdigest()
    plan["analysis_script_sha256"]=sha256(Path(__file__).read_bytes()).hexdigest()
    if args.confirmation is None:
        if args.spec_output is None:
            parser.error("--spec-output required to freeze confirmation")
        write(args.plan,plan)
        write(args.spec_output,plan["spec"])
    else:
        if args.result is None or plan != read(args.plan):
            parser.error("registered plan differs or --result was not supplied")
        write(args.result,confirm(plan,config,args.confirmation))


if __name__ == "__main__":
    main()
