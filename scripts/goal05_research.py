#!/usr/bin/env python3
"""Freeze, execute and independently audit the registered three-part Goal 05 matrix."""
import argparse
import asyncio
from copy import deepcopy
from hashlib import sha256
from importlib.metadata import version
from math import ceil, sqrt
from pathlib import Path
from statistics import covariance, fmean, stdev

from scipy.stats import binomtest, chi2, norm, ttest_1samp
from statsmodels.stats.multitest import multipletests

from civitas.application.economic_run import current_implementation, replay_run
from civitas.contracts.base import content_hash
from civitas.experiments.labor_choice_preview import PREVIEW_SEAT, audit_labor_choice, run_labor_choice
from civitas.experiments.registry import run_lock
from civitas.experiments.scenario_archive import _replay_receipt, audit_suite
from civitas.experiments.scenario_catalog import ScenarioSuite, run_suite
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.storage.commit_store import CommitStore, atomic_json, json_bytes, read_json

VERSION = 'ecosim.goal05.paired.v1'
SCENES = {'A1': 'A', 'D1': 'D'}
# This pilot predates the confirmation-only correction to the scene key lookup.
REGISTERED_PILOT_DRIVER = '7ca9dda3f8c8c2c0896b9fbb73396e0698236b980651f9748b7bf427b2ec5ef3'
KNOWN_FAILURES = (OSError, ValueError, RuntimeError, KeyError, TypeError, AssertionError)


def interval(pair):
    first, last = pair
    if type(first) is not int or type(last) is not int or not 0 <= first <= last:
        raise ValueError('Expected an inclusive nonnegative seed range')
    return list(range(first, last + 1))


def implementation():
    runtime = current_implementation()
    return {**{k: runtime[k] for k in ('source_hash', 'dependency_hash')},
            'statistics': {name: version(name) for name in ('scipy', 'statsmodels')},
            'driver_sha256': sha256(Path(__file__).read_bytes()).hexdigest()}


def freeze(protocol_path, output, pilot=None):
    protocol = read_json(protocol_path)
    if protocol['version'] != VERSION or protocol['primary_family'] != list(SCENES):
        raise ValueError('Unknown registered research protocol')
    scene_template = read_json(protocol_path.parent / protocol['scene_spec'])
    strategy = read_json(protocol_path.parent / protocol['strategy_template'])
    studies = protocol['studies']
    ranges = [interval(studies[name][key]) for name in SCENES for key in ('pilot_roots', 'confirmation_pool')]
    ranges.extend(interval(protocol['strategy'][key]) for key in ('pilot_roots', 'confirmation_roots'))
    addresses = [seed for group in ranges for seed in group]
    if len(set(addresses)) != len(addresses):
        raise ValueError('Pilot, confirmation and independent study pools must be disjoint')
    allocation = {}
    pilot_hash = None
    pilot_plan = pilot_report = None
    if pilot is not None:
        pilot_plan = load_plan(pilot/'plan.json')
        if pilot_plan['phase'] != 'pilot' or pilot_plan['protocol'] != protocol:
            raise ValueError('Confirmation requires the originally registered pilot')
        pilot_report = audit(pilot)
        pilot_hash = pilot_plan['plan_hash']
        for name in SCENES:
            allocation[name] = allocate(protocol, studies[name], pilot_report['questions'][name])
    suite = deepcopy(scene_template)
    for name, scene in SCENES.items():
        rule = studies[name]
        seeds = (interval(rule['confirmation_pool'])[:allocation[name]['allocated_roots']] if pilot else
                 interval(rule['pilot_roots']))
        suite['scenarios'][scene]['seeds'] = seeds
        suite['scenarios'][scene]['config']['root_seed'] = seeds[0]
    suite = ScenarioSuite.model_validate(suite).model_dump(mode='json')
    strategy = ClosedEconomyConfig.model_validate(strategy).model_dump(mode='json')
    strategy_seeds = interval(protocol['strategy']['confirmation_roots' if pilot else 'pilot_roots'])
    if pilot and (pilot_plan['strategy'] != strategy or any(
        {k:v for k,v in pilot_plan['suite']['scenarios'][scene]['config'].items() if k != 'root_seed'}
        != {k:v for k,v in suite['scenarios'][scene]['config'].items() if k != 'root_seed'}
        for scene in SCENES.values())):
        raise ValueError('Configuration differs from the piloted study')
    body = {'version': VERSION, 'phase': 'confirmation' if pilot else 'pilot',
            'protocol': protocol, 'implementation': implementation(),
            'suite': suite, 'strategy': strategy, 'strategy_seeds': strategy_seeds,
            'pilot_plan_hash': pilot_hash,
            'pilot_driver_sha256': pilot_plan['implementation']['driver_sha256'] if pilot else None,
            'pilot_report_hash': content_hash(pilot_report) if pilot else None,
            'allocation': allocation}
    result = {'plan_hash': content_hash(body), **body}
    atomic_json(output, result, replace=False)
    return result


def allocate(protocol, study, question):
    values = [root['value'] for root in question['blocks'] if root['value'] is not None]
    if not question['valid'] or len(values) < protocol['minimum_eligible_pilot_roots']:
        raise ValueError('Pilot cannot estimate this registered confirmation sample')
    df = len(values) - 1
    sd = stdev(values)
    confidence = protocol['planning_confidence']
    upper = max(study['planning_sd_floor'], sd * sqrt(df / chi2.ppf(1 - confidence, df)))
    eligible = binomtest(len(values), len(question['blocks'])).proportion_ci(
        confidence_level=confidence, method='wilson').low
    alpha = protocol['alpha'] / len(SCENES)
    powered = ((norm.ppf(1 - alpha) + norm.ppf(protocol['power'])) * upper / study['important_effect'])**2
    precise = (norm.ppf(1 - protocol['alpha']/2) * upper / study['ci_halfwidth_target'])**2
    required = ceil(max(powered, precise))
    width = study['allocation_block']
    total = max(study['minimum_allocated'], ceil(required / eligible / width) * width)
    cap = len(interval(study['confirmation_pool']))
    return {'eligible_pilot_roots': len(values), 'pilot_sd': sd, 'planning_sd_upper': upper,
            'wilson_lower_eligible_fraction': float(eligible), 'required_eligible_roots': required,
            'required_allocated_roots': total, 'allocated_roots': min(total, cap),
            'resource_cap_reached': total > cap}


def load_plan(path):
    body = read_json(path)
    identity = implementation()
    recorded = body['implementation']
    if body['phase'] == 'pilot' and recorded['driver_sha256'] == REGISTERED_PILOT_DRIVER:
        identity['driver_sha256'] = REGISTERED_PILOT_DRIVER
    if (content_hash({k:v for k,v in body.items() if k != 'plan_hash'}) != body['plan_hash']
            or body['version'] != VERSION or recorded != identity):
        raise ValueError('Frozen matrix plan, source or analysis dependencies differ')
    return body


def question_summary(suite, plan):
    market = [row for row in suite['cases'] if row['scenario'] == 'A' and not row['boundary']]
    d, = [row for row in suite['cases'] if row['scenario'] == 'D']
    d1 = d.get('d1', {})
    eligible = {r['seed']: r['delta'] for r in d1.get('eligible', {}).get('contrasts', [])}
    a_blocks = [{'seed': r['seed'], 'value': r.get('outcomes', {}).get('A1') if r['gate'] else None}
                for r in market]
    d_blocks = [{'seed': seed, 'value': eligible.get(seed)} for seed in plan['suite']['scenarios']['D']['seeds']]
    factors = [[r['rows'][high]['drawdown'] - r['rows'][low]['drawdown'] for r in market if r['gate']
                and r['outcomes'].get('A1') is not None] for high,low in (('S1H1','S1H0'),('S0H1','S0H0'))]
    result = {}
    for name, blocks in (('A1', a_blocks), ('D1', d_blocks)):
        values = [r['value'] for r in blocks if r['value'] is not None]
        n = len(values)
        valid = len(a_blocks) == n if name == 'A1' else bool(d and d1 and
            d['assigned_worlds'] == d['completed_worlds'] and not d1['incomplete_roots']
            and not d1['mechanism_mismatches'])
        sd = stdev(values) if n > 1 else None
        row = {'valid': valid, 'blocks': blocks, 'assigned_roots': len(blocks), 'eligible_roots': n,
               'mean': fmean(values) if values else None, 'sd': sd, 'mcse': sd/sqrt(n) if sd is not None else None,
               'ci95': None, 'p_raw': None, 'p_holm': None,
               'simple_effect_covariance': (covariance(*factors) if len(factors[0]) > 1 else None)
                    if name == 'A1' else d1.get('eligible', {}).get('simple_effect_covariance')}
        if plan['phase'] == 'confirmation' and valid and sd:
            primary = ttest_1samp(values, 0, alternative='greater')
            ci = ttest_1samp(values, 0).confidence_interval(.95)
            row.update(p_raw=float(primary.pvalue), ci95=[float(ci.low), float(ci.high)],
                       df=n-1, ci_halfwidth=float((ci.high-ci.low)/2))
        if plan['phase'] == 'confirmation':
            row['target_eligible_roots_met'] = n >= plan['allocation'][name]['required_eligible_roots']
            row['precision_target_met'] = (row['ci95'] is not None
                and row['ci_halfwidth'] <= plan['protocol']['studies'][name]['ci_halfwidth_target'])
        result[name] = row
    p_values = [result[name]['p_raw'] for name in SCENES]
    adjusted = multipletests([p if p is not None else 1. for p in p_values], method='holm')[1]
    for name,p in zip(SCENES,adjusted,strict=True):
        if result[name]['p_raw'] is not None:
            result[name]['p_holm'] = float(p)
    return result


async def execute_strategy(root, seed, config):
    path = root / str(seed)
    if (root / f'{seed}-attempt.json').exists():
        return
    atomic_json(root / f'{seed}-attempt.json', {'root_seed': seed}, replace=False)
    try:
        report = await run_labor_choice({**config, 'root_seed': seed}, PREVIEW_SEAT, path)
        status = report['status']
        for side in ('rule', 'archive'):
            outcome = await replay_run(path/side, destination=path/(side+'-replay'))
            if outcome['status'] != 'matched':
                status = 'failed'
        receipt = {'status': status}
    except KNOWN_FAILURES as exc:
        receipt = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
    atomic_json(root / f'{seed}-receipt.json', receipt, replace=False)


def strategy_rows(root, plan):
    results = []
    for seed in plan['strategy_seeds']:
        location = root / str(seed)
        receipt = root / f'{seed}-receipt.json'
        status = (read_json(receipt) if receipt.exists() else
                  {'status': 'interrupted' if (root / f'{seed}-attempt.json').exists() else 'not_run'})
        row = {'seed': seed, **status}
        if (location/'shareable-comparison.json').exists():
            frozen = read_json(location/'plan.json')['plan']
            if frozen['arms']['rule'] != {**plan['strategy'], 'root_seed':seed}:
                raise ValueError('Strategy arm differs from its frozen root')
            report = audit_labor_choice(location)
            row.update(denominators=report['denominators'], difference=report['difference'])
        elif status['status'] == 'completed':
            raise ValueError('Completed strategy assignment is missing its audited result')
        if status['status'] == 'completed':
            row['replays'] = {}
            for side in ('rule', 'archive'):
                archive = location / side
                store = CommitStore(archive)
                end = store.markers()[-1]
                row['replays'][side] = _replay_receipt(root, archive, store, end, plan['strategy']['ticks'])
        results.append(row)
    return results


def report(root, plan, *, verify):
    suite_dir = root/'scenarios'
    sealed = read_json(suite_dir/'plan.json')['plan']
    if (sealed['spec'] != plan['suite'] or list(sealed['catalog']) != ['A','D']
            or any(sealed['implementation'][k] != plan['implementation'][k]
                   for k in ('source_hash','dependency_hash'))):
        raise ValueError('Scenario stage differs from the frozen research assignment')
    suite = read_json(suite_dir/'report.json')
    if verify:
        authority = audit_suite(suite_dir)
        if authority['status'] != 'completed':
            raise ValueError('Scenario recomputation did not verify: ' + str(authority['failures']))
    rows = strategy_rows(root/'strategy',plan)
    network, = [case for case in suite['cases'] if case['scenario'] == 'D']
    markets = [case for case in suite['cases'] if case['scenario'] == 'A']
    market_expected = plan['suite']['scenarios']['A']['config']
    exposure_n = sum(case['assigned_worlds'] for case in markets) * market_expected['households'] * (
        market_expected['ticks']-market_expected['securities']['shock_tick']+1)
    visible = sum(len(row.get('appraisal',[])) for case in markets for row in case['rows'].values())
    strategy_complete = sum(row['status'] == 'completed' for row in rows)
    strategy_deltas = [r['difference']['paid_contract_wages_minor'] for r in rows
                       if r['status'] == 'completed' and r['difference'] is not None]
    return {'plan_hash': plan['plan_hash'], 'phase': plan['phase'],
            'status': 'completed' if suite['completed_worlds']==suite['assigned_worlds']
                    and strategy_complete==len(rows) else 'incomplete',
            'questions': question_summary(suite,plan), 'suite_plan_hash': suite['plan_hash'],
            'scenes': {'assigned_worlds': suite['assigned_worlds'], 'completed_worlds': suite['completed_worlds'],
                       'failed_or_unfinished_worlds': suite['assigned_worlds'] - suite['completed_worlds'],
                       'security_assigned_exposures': exposure_n, 'security_observed_appraisals': visible,
                       'security_undefined_drawdown_worlds': sum(
                           row.get('drawdown') is None for case in markets for row in case['rows'].values()),
                       'network_denominators': network.get('d1', {})},
            'strategy': {'assigned_roots': len(rows), 'completed_roots': strategy_complete, 'roots': rows,
                         'paired_wage_difference_mean_minor': fmean(strategy_deltas) if strategy_deltas else None},
            'external_api_calls': 0}


def audit(root):
    plan = load_plan(root/'plan.json')
    recomputed = report(root, plan, verify=True)
    if content_hash(read_json(root/'report.json')) != content_hash(recomputed):
        raise ValueError('Registered result differs from the independently recomputed matrix')
    return recomputed


def run(plan_path, root, workers, resume):
    plan = load_plan(plan_path)
    if not resume:
        root.mkdir(parents=True)
        atomic_json(root/'plan.json',plan,replace=False)
    with run_lock(root):
        if load_plan(root/'plan.json') != plan:
            raise ValueError('Resume requires the original frozen matrix')
        if (root/'report.json').exists():
            return audit(root)
        location = root/'scenarios'
        run_suite(plan['suite'],location,scenarios=('A','D'),workers=workers,resume=location.exists())
        strategy = root/'strategy'
        strategy.mkdir(exist_ok=True)
        for seed in plan['strategy_seeds']:
            asyncio.run(execute_strategy(strategy,seed,plan['strategy']))
        summary = report(root,plan,verify=False)
        atomic_json(root/'report.json',summary,replace=False)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    registration = modes.add_parser('freeze')
    registration.add_argument('--protocol', type=Path, required=True)
    registration.add_argument('--pilot', type=Path)
    registration.add_argument('--output', type=Path, required=True)
    execution = modes.add_parser('run')
    execution.add_argument('--plan', type=Path, required=True)
    execution.add_argument('--output', type=Path, required=True)
    execution.add_argument('--workers', type=int, default=1)
    execution.add_argument('--resume', action='store_true')
    verification = modes.add_parser('audit')
    verification.add_argument('root', type=Path)
    args = parser.parse_args()
    if args.mode == 'freeze':
        value = freeze(args.protocol.resolve(),args.output.resolve(),args.pilot.resolve() if args.pilot else None)
    elif args.mode == 'run':
        value = run(args.plan.resolve(),args.output.resolve(),args.workers,args.resume)
    else:
        value = audit(args.root.resolve())
    print(json_bytes(value).decode(), end='')
    return int(value.get('status') == 'incomplete')


if __name__ == '__main__':
    raise SystemExit(main())
