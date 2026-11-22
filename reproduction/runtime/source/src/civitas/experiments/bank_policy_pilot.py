"""Frozen, independent-root feasibility pilot for the same-dose H-E2 contrast."""
import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from math import sqrt
import multiprocessing
from pathlib import Path
from statistics import mean, stdev

from civitas.application import economic_run
from civitas.contracts.base import content_hash, require_unique
from civitas.experiments.institutions import (
    _BANK_POLICY_OUTCOMES, _bank_policy_arm, _bank_policy_issues, _bank_policy_outcomes,
    _bank_policy_terms, _run,
)
from civitas.experiments.registry import run_lock
from civitas.storage.commit_store import CommitStore, atomic_json, read_json


DESIGN = 'H-E2.fixed_component_dose.independent_root_feasibility.v1'
WEIGHTS = {'D0S0': 1, 'D0S1': -1, 'D1S0': -1, 'D1S1': 1}


def freeze_pilot(base, seeds, through):
    base = economic_run.parse_config(base)
    seeds = list(seeds)
    if not seeds or any(type(seed) is not int or seed < 0 for seed in seeds):
        raise ValueError('Use distinct nonnegative independent root seeds')
    require_unique(seeds, 'pilot root seed')
    if (base.banking is None or base.fiscal is None or base.fault_tick is not None
            or base.stopping_hazard_bps is not None or base.cognition_mode != 'rule'
            or type(through) is not int or not 1 <= through <= base.ticks):
        raise ValueError('H-E2 pilot needs an offline fixed-horizon budgeted banking world')
    base = economic_run.parse_config({**base.model_dump(mode='json'), 'ticks': through})
    notice, support, source, intended, _ = _bank_policy_terms(base, fixed_dose_only=True)
    if not source.tick < support.maturity_tick <= through:
        raise ValueError('H-E2 pilot cutoff must include Treasury maturity')
    fee, principal = sum(intended.values()), support.amount_minor
    doses = {'D0S0': [0, 0], 'D0S1': [0, principal], 'D1S0': [fee, 0], 'D1S1': [fee, principal]}
    configs = {}
    for seed in seeds:
        seeded = base.model_copy(update={'root_seed': seed})
        for arm, (disclosure, support_principal) in doses.items():
            configs[f'{seed}/{arm}'] = _bank_policy_arm(seeded, disclosure, support_principal)
    plan = {
        'design': DESIGN, 'implementation': economic_run.current_implementation(),
        'base_config': base.model_dump(mode='json'), 'seeds': seeds,
        'policy_window': [notice.tick, through], 'source_tick': source.tick,
        'normalized_pair_cursor_tick': notice.tick - 1,
        'excluded_pair_hash_field': 'data.economic_runtime.institution_config_hash',
        'expected_doses_minor': doses, 'dose_order': ['disclosure_paid_minor', 'support_disbursed_minor'],
        'contrast_weights': WEIGHTS, 'primary_outcome': 'deposit_loss_rate',
        'loss_denominator': 'aggregate deposits immediately before the source withdrawal period',
        'loss_missing_if': ['zero pre-shock deposits', 'unresolved receivership deposits at cutoff'],
        'outcomes': list(_BANK_POLICY_OUTCOMES),
        'analysis': {'unit': 'independent root block', 'test': None,
                     'retain_unimplemented_assignments': True, 'confirmation_gate': 'requires a revised pilot if zero observed dispersion',
                     'technical_failure_accounting': 'all assigned worlds and all assigned root blocks',
                     'natural_termination_before_cutoff': 'primary endpoint missing without imputation'},
        'arms': configs,
    }
    return {'plan_hash': content_hash(plan), 'plan': plan}


def summarize_pilot(plan, rows):
    arms = plan['contrast_weights']
    blocks, resolved = [], {}
    for seed in plan['seeds']:
        panel = {arm: rows.get(f'{seed}/{arm}', {'status': 'not_run'}) for arm in arms}
        resolved.update({f'{seed}/{arm}': row for arm, row in panel.items()})
        complete = all(row['status'] == 'completed' for row in panel.values())
        matched = complete and len({row['pre_policy_state_hash'] for row in panel.values()}) == 1
        dose_ok = matched and all(not row['manipulation_issues'] for row in panel.values())
        primary = {arm: row.get(plan['primary_outcome']) for arm, row in panel.items()}
        observed = matched and all(value is not None for value in primary.values())
        interaction = sum(arms[arm] * primary[arm] for arm in arms) if observed else None
        block = {'seed': seed, 'matched_start': matched, 'full_dose': dose_ok,
                 'primary_by_arm': primary, 'primary_interaction': interaction,
                 'technical_failure': any(row['status'] in ('failed', 'interrupted') for row in panel.values())}
        for key in ('requested_minor', 'withdrawals_paid_by_end_minor', 'withdrawal_remaining_minor',
                    'public_net_cash_outlay_minor'):
            if matched and all(row.get(key) is not None for row in panel.values()):
                block[key + '_interaction'] = sum(arms[arm] * row[key] for arm, row in panel.items())
        blocks.append(block)
    values = [b['primary_interaction'] for b in blocks if b['primary_interaction'] is not None]
    dispersion = stdev(values) if len(values) > 1 else None
    statuses = Counter(row['status'] for row in resolved.values())
    failures = sum(block['technical_failure'] for block in blocks)
    return {
        'assigned_roots': len(blocks), 'assigned_worlds': len(resolved), 'world_statuses': dict(statuses),
        'observed_primary_roots': len(values),
        'full_dose_primary_roots': sum(b['full_dose'] and b['primary_interaction'] is not None for b in blocks),
        'root_blocks_with_technical_failure': failures,
        'zero_failure_one_sided_95_upper_per_root': (
            1 - 0.05 ** (1 / len(blocks)) if blocks and not failures and not statuses['not_run'] else None),
        'primary_mean': mean(values) if values else None, 'primary_sample_sd': dispersion,
        'nondegenerate_primary_mcse': dispersion / sqrt(len(values)) if dispersion else None,
        'confirmatory_test': None, 'blocks': blocks, 'rows': resolved,
    }


def _run_arm(destination, sealed, assignment):
    plan = sealed['plan']
    config = plan['arms'][assignment]
    path = destination / 'worlds' / assignment
    evidence = destination / 'receipts' / assignment
    evidence.mkdir(parents=True, exist_ok=False)
    identity = {'assignment': assignment, 'plan_hash': sealed['plan_hash'], 'config_hash': content_hash(config)}
    atomic_json(evidence/'started.json', identity)
    world, row = asyncio.run(_run(config, path))
    if world is not None:
        if row['replay']['final_tick'] != plan['policy_window'][1]:
            row.update(status='ended_before_cutoff', termination_tick=row['replay']['final_tick'])
        else:
            try:
                store = CommitStore(path)
                notice, support, source, intended, _ = _bank_policy_terms(
                    economic_run.parse_config(plan['base_config']), fixed_dose_only=True)
                row.update(_bank_policy_outcomes(world, store, row, support, source))
                row['pre_policy_marker'] = store.markers()[notice.tick-1].commit_hash
                row['manipulation_issues'] = _bank_policy_issues(
                    row, *plan['expected_doses_minor'][assignment.split('/')[1]], notice, source, intended)
                row['bank_policy_rejections'] = [
                    {'tick': event.tick, 'tx_id': event.tx_id, 'action': event.payload['action'],
                     'reason': event.details['reason']}
                    for event in world.scenario.economy.journal if event.kind == 'bank'
                    and event.payload['action'] in ('notice', 'support_disburse') and 'reason' in event.details
                ]
            except (OSError, ValueError, KeyError, AssertionError) as exc:
                row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
    atomic_json(evidence/'result.json', {'identity': identity, 'row': row})


def _read_pilot(destination):
    sealed = read_json(destination/'plan.json')
    plan = sealed['plan']
    if plan['design'] != DESIGN or content_hash(plan) != sealed['plan_hash']:
        raise ValueError('H-E2 frozen plan has changed')
    rows, pending = {}, []
    for assignment, config in plan['arms'].items():
        folder = destination/'receipts'/assignment
        if (folder/'result.json').exists():
            receipt = read_json(folder/'result.json')
            if receipt['identity'] != {'assignment': assignment, 'plan_hash': sealed['plan_hash'],
                                      'config_hash': content_hash(config)}:
                raise ValueError('H-E2 arm receipt does not match its assignment')
            rows[assignment] = receipt['row']
        elif folder.exists() or (destination/'worlds'/assignment).exists():
            rows[assignment] = {'status': 'interrupted', 'error': 'Started without a final receipt'}
        else:
            pending.append(assignment)
    return sealed, rows, pending


def run_pilot(destination, *, base=None, seeds=(), through=None, workers=1, resume=False, analyze_only=False):
    destination = Path(destination).resolve()
    if type(workers) is not int or not 1 <= workers <= 8 or (resume and analyze_only):
        raise ValueError('Choose 1–8 workers and one execution mode')
    if not (resume or analyze_only):
        sealed = freeze_pilot(base, seeds, through)
        destination.mkdir(parents=True, exist_ok=False)
        atomic_json(destination/'plan.json', sealed)

    with run_lock(destination):
        sealed, rows, pending = _read_pilot(destination)
        if not analyze_only:
            actual = economic_run.current_implementation()
            recorded = sealed['plan']['implementation']
            if any(actual[key] != recorded[key] for key in ('source_hash', 'dependency_hash')):
                raise ValueError('H-E2 execution needs the frozen source and dependencies')
            if workers == 1:
                for assignment in pending:
                    _run_arm(destination, sealed, assignment)
            elif pending:
                with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn')) as pool:
                    tasks = [pool.submit(_run_arm, destination, sealed, name) for name in pending]
                    for task in as_completed(tasks):
                        task.result()
            sealed, rows, _ = _read_pilot(destination)
        report = summarize_pilot(sealed['plan'], rows)
        report['plan_hash'] = sealed['plan_hash']
        if not analyze_only:
            atomic_json(destination/'report.json', report)
    return report
