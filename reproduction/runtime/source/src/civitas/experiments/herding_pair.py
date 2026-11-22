"""Freeze and audit the single-root Scenario A research preview."""
from collections import Counter, defaultdict
import re
from pathlib import Path

from civitas.application.economic_run import current_implementation
from civitas.application.inspection import _cursor
from civitas.contracts.base import content_hash
from civitas.experiments.registry import history
from civitas.experiments.scenario_archive import _archive_path
from civitas.experiments.scenario_catalog import security_drawdown
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.security_market import SHOCK, validate_security_receipts
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from civitas.storage.history import publish_directory, retain_history

ANALYSIS = 'wp10.A.herding-pair.v1'
SIDES = {'on': True, 'off': False}
TECHNICAL_FAILURES = {'failed', 'projection_failed'}


def _plan(raw, implementation):
    config = ClosedEconomyConfig.model_validate(raw)
    if not config.securities or not config.archive_observations or config.fault_tick is not None:
        raise ValueError('The pair requires Scenario A, recorded OBSERVE and a fixed fault-free plan')
    base = config.model_dump(mode='json')
    return {'analysis': ANALYSIS, 'endpoint': 'maximum_trade_close_drawdown_after_shock',
            'root_seed': config.root_seed, 'natural_stopping': False,
            'shock_tick': config.securities.shock_tick, 'final_tick': config.ticks,
            'investors': [f'investor:{i:04d}' for i in range(config.households)],
            'implementation': {key: implementation[key] for key in ('source_hash', 'dependency_hash')},
            'configs': {side: {**base, 'securities': {**base['securities'], 'herding': herding}}
                        for side, herding in SIDES.items()}}


def freeze_pair(config, output):
    plan = _plan(config, current_implementation())
    sealed = {'plan': plan, 'plan_hash': content_hash(plan)}
    with publish_directory(output) as staging:
        atomic_json(staging/'plan.json', sealed)
        for side, raw in plan['configs'].items():
            atomic_json(staging/f'{side}.json', raw)
    return {'status': 'frozen', 'plan_hash': sealed['plan_hash']}


def _bindings(sealed, download):
    plan = sealed['plan']
    if (sealed['plan_hash'] != content_hash(plan)
            or plan != _plan(plan['configs']['on'], plan['implementation'])):
        raise ValueError('Invalid frozen herding plan')
    tick = plan['final_tick']
    if (download['schema_version'] != 1 or download['audience'] != 'researcher'
            or download['authority'] is not False or download['tick'] != tick
            or download['shared_parent_commit'] is not None or len(download['arms']) != 2):
        raise ValueError('Choose a researcher comparison download for the registered final tick')
    arms = {}
    for row in download['arms']:
        job = row['job']
        if not re.fullmatch('[a-f0-9]{32}', job):
            raise ValueError('Invalid workbench run identifier')
        declared = row['plan']
        side = 'on' if declared['config']['securities']['herding'] else 'off'
        config = plan['configs'][side]
        if (side in arms or declared['config'] != config or declared['config_hash'] != content_hash(config)
                or declared['terminal_tick'] != tick or declared['intervention'] is not None
                or declared['parent'] is not None
                or any(declared[key] != value for key, value in plan['implementation'].items())):
            raise ValueError('An exported arm differs from the frozen treatment or implementation')
        arms[side] = row
    if len({row['job'] for row in arms.values()}) != 2:
        raise ValueError('The two arms must identify distinct runs')
    return plan, arms


def _signals(store, marker, state, plan, herding):
    observations = {obs.actor_id: obs for obs in store.observation_rows(marker)}
    decisions = {row.actor_id: row for row in state.decisions if row.tick == marker.tick}
    if (set(observations) != set(state.actors) or decisions.keys() != observations.keys()
            or any(row.observation_id != observations[actor].observation_id for actor, row in decisions.items())):
        raise ValueError('OBSERVE and committed decisions do not identify the same actors and period')
    result = {}
    for actor, obs in observations.items():
        facts = [fact for fact in obs.market_view if fact.value.get('kind') == 'security_signal']
        eligible = actor in plan['investors'] and marker.tick >= plan['shock_tick']
        if len(facts) != int(eligible):
            raise ValueError('An opportunity lacks its authorized signal or reached an ineligible actor')
        if not eligible:
            continue
        fact, = facts
        decision = decisions[actor]
        appraisal = decision.belief_patch['security_appraisal']
        source, value = fact.value['source'], fact.value['sentiment_minor']
        order, = decision.economic_intents
        if (fact.owner_id != actor or fact.public or fact.value['root_event_id'] != SHOCK
                or fact.fact_id not in decision.evidence_ids or appraisal['exposed_source'] != source
                or appraisal['raw_signal_minor'] != value
                or appraisal['applied_herding_minor'] != (value if herding else 0)
                or order.side != 'sell' or order.price_ticks != appraisal['limit_minor']):
            raise ValueError('Authorized signals do not match the invested order and applied mechanism')
        result[marker.tick, actor] = (source, value, order.price_ticks)
    return result


def _arm(root, plan, side, export):
    store = CommitStore(_archive_path(root, side))
    config = plan['configs'][side]
    terminal = plan['final_tick']
    identity = store.identity
    if (identity.run_id != export['job'] or identity.config_hash != content_hash(config)
            or identity.config != config or identity.code_hash != plan['implementation']['source_hash']
            or identity.metadata['implementation']['dependency_hash'] != plan['implementation']['dependency_hash']
            or identity.metadata.get('terminal_tick', config['ticks']) != terminal
            or identity.metadata.get('parent') is not None):
        raise ValueError('An archived arm differs from its assigned run or config')
    commits, signals = [], {}
    for marker, state in store.states():
        if marker.tick:
            signals.update(_signals(store, marker, state, plan, SIDES[side]))
        commits.append(_cursor(store, marker))
    if commits[0]['tick'] != 0 or marker.tick > terminal:
        raise ValueError('The archived arm does not cover a valid initial prefix')
    validate_security_receipts(state)
    trades = defaultdict(list)
    for row in state.data['economic_runtime']['journal']:
        if row['kind'] == 'security_trade':
            trades[row['tick']].append(row)
    periods = []
    for tick in range(1, marker.tick+1):
        rows = trades[tick]
        periods.append({'tick': tick, 'close_minor': rows[-1]['payload']['price_ticks'] if rows else None,
                        'volume_units': sum(row['payload']['quantity_units'] for row in rows),
                        'fill_count': len(rows), 'journal_tx_ids': [row['tx_id'] for row in rows]})
    if periods != state.data['security_market']['history']:
        raise ValueError('Recorded closes or volumes disagree with actual ledger settlements')
    completed = marker.tick == terminal
    report = export['report']
    if completed:
        view, = report['views']
        expected = {f"security_{key}": periods[-1][key] for key in ('close_minor', 'volume_units', 'fill_count')}
        if (report['status'] != 'available' or report['audience'] != 'researcher'
                or report['run_id'] != identity.run_id or view['cursor'] != commits[-1]
                or view['comparison_metrics'] != expected):
            raise ValueError('Downloaded cursor or market metrics differ from the authoritative terminal commit')
    elif report != {'status': 'uncommitted', 'tick': terminal}:
        raise ValueError('Downloaded period disagrees with the archived commit watermark')
    attempts = read_json(_archive_path(root, f'{side}-attempts.json'))
    if not isinstance(attempts, list):
        raise ValueError('Expected retained execution receipts')
    for row in attempts:
        if row.get('tick') is not None and (row['run_id'] != identity.run_id or not 0 <= row['tick'] <= marker.tick
                or row['state_hash'] != commits[row['tick']]['state_hash']):
            raise ValueError('An execution receipt disagrees with its committed state')
    counts = dict(Counter(row['status'] for row in attempts))
    if completed and not any(row.get('tick') == terminal for row in attempts):
        raise ValueError('Missing terminal execution receipt')
    if completed and view['attempts'] != [{'status': row['status'], 'tick': row['tick']} for row in attempts
                                           if row.get('tick') is not None]:
        raise ValueError('Execution receipts differ from the browser download')
    nonzero = sum(value != 0 for _, value, _ in signals.values())
    return {'run_id': identity.run_id, 'config_hash': identity.config_hash, 'commits': commits,
            'completed': completed, 'last_attempt_status': attempts[-1]['status'] if attempts else None,
            'attempt_status_counts': counts,
            'order_receipts': dict(Counter(row.status for row in state.receipts)),
            'delivered_opportunities': len(signals), 'nonzero_signals': nonzero,
            'nonzero_applications': nonzero if SIDES[side] else 0,
            'periods': periods, 'drawdown': security_drawdown(periods, plan['shock_tick']) if completed else None}, signals


def _recompute(root):
    sealed, download = (read_json(_archive_path(root, name)) for name in ('plan.json', 'comparison.json'))
    plan, arms = _bindings(sealed, download)
    on, on_signals = _arm(root, plan, 'on', arms['on'])
    off, off_signals = _arm(root, plan, 'off', arms['off'])
    paired = on_signals.keys() & off_signals.keys()
    if (any(on_row['state_hash'] != off_row['state_hash']
                   for on_row, off_row in zip(on['commits'][:plan['shock_tick']], off['commits'][:plan['shock_tick']]))
            or any(on_signals[key][:2] != off_signals[key][:2] for key in paired)):
        raise ValueError('The two arms do not share the initial state, pre-shock prefix and exogenous signals')
    planned = len(plan['investors']) * (plan['final_tick'] - plan['shock_tick'] + 1)
    shifted = sum(on_signals[key][2] != off_signals[key][2] for key in paired)
    completed = int(on['completed']) + int(off['completed'])
    manipulation = len(paired) == planned and shifted > 0
    measurable = on['drawdown'] is not None and off['drawdown'] is not None
    return {'status': 'completed' if completed == 2 and manipulation and measurable else 'failed',
            'analysis': plan['analysis'], 'plan_hash': sealed['plan_hash'], 'root_seed': plan['root_seed'],
            'endpoint': plan['endpoint'], 'window': [plan['shock_tick'], plan['final_tick']],
            'denominators': {'assigned_worlds': 2, 'completed_worlds': completed, 'incomplete_worlds': 2-completed,
                             'natural_stops': 0,
                             'worlds_with_terminal_execution_failure': sum(
                                 arm['last_attempt_status'] in TECHNICAL_FAILURES for arm in (on, off)),
                             'failed_attempts': sum(
                                 arm['attempt_status_counts'].get(status, 0)
                                 for arm in (on, off) for status in TECHNICAL_FAILURES),
                             'planned_opportunities_per_arm': planned, 'matched_signal_opportunities': len(paired)},
            'manipulation_passed': manipulation, 'different_sell_limits': shifted,
            'drawdown_on_minus_off': on['drawdown'] - off['drawdown'] if manipulation and measurable else None,
            'arms': {'on': on, 'off': off}}


def pack_pair(plan_path, download_path, workspace, output):
    sealed, download = read_json(plan_path), read_json(download_path)
    _, arms = _bindings(sealed, download)
    runs = _archive_path(workspace.resolve(), 'runs')
    if output.resolve().is_relative_to(runs):
        raise ValueError('Publish the bundle outside the runs directory')
    with publish_directory(output) as staging:
        atomic_json(staging/'plan.json', sealed)
        atomic_json(staging/'comparison.json', download)
        for side, arm in arms.items():
            source = _archive_path(runs, arm['job'])
            retain_history(source, staging/side)
            atomic_json(staging/f'{side}-attempts.json', history(source))
        result = _recompute(staging)
        atomic_json(staging/'result.json', result)
    return result


def audit_pair(root):
    root = root.resolve()
    result = _recompute(root)
    if read_json(_archive_path(root, 'result.json')) != result:
        raise ValueError('Reported comparison differs from the independent archive recomputation')
    return result
