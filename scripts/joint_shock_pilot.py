"""Execute and reconcile the frozen offline Laputa joint-shock pilot."""
import argparse
import asyncio
from collections import Counter, defaultdict
from hashlib import sha256
from importlib.metadata import distributions, version
from pathlib import Path
import platform
import statistics
import subprocess
import time

from scipy.stats import t as student_t

from civitas.application import economic_run
from civitas.contracts.base import content_hash
from civitas.experiments.institutions import audit_institutions
from civitas.storage.commit_store import CommitStore, atomic_json, read_json


ROOT = Path(__file__).resolve().parents[1]
REGISTERED = ROOT / 'examples/joint-shock-protocol.v1.json'
REGISTERED_HASH = '2afa31dcd74622031e0c7238f252022f9646353f5572a091609aaf36902b98cd'
REGISTRATION_COMMIT = '4215ebd39c156065b75e78c34a422882d4ca2c70'


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def load_protocol(path):
    if file_hash(path) != REGISTERED_HASH:
        raise ValueError('Protocol differs from the preregistered joint shock pilot')
    protocol = read_json(path)
    fixture = ROOT / protocol['base_fixture']
    if file_hash(fixture) != protocol['base_fixture_sha256']:
        raise ValueError('Joint economy fixture differs from the frozen specification')
    return protocol, read_json(fixture)


def assigned_configuration(protocol, base, seed, arm):
    settings = protocol['arm_settings'][arm]
    config = economic_run.parse_config({
        **base,
        'root_seed': seed,
        'replica': protocol['replica'],
        'security_feedback': {**base['security_feedback'], 'shock_enabled': settings['shock_enabled']},
        'reference_loss': {'lambda_bps': settings['lambda_bps'],
                           'peer_disclosure': protocol['frozen_variables']['peer_disclosure']},
    })
    if (config.cognition_mode != 'rule' or config.stopping_hazard_bps is not None
            or not config.archive_observations or config.ticks != protocol['clock']['terminal_tick']):
        raise ValueError('The registered pilot requires offline fixed-horizon rule worlds with archived observations')
    return config


def realized_return(close, previous_close):
    if close is None:
        return None, 'no_realized_close'
    if previous_close is None:
        return None, 'no_adjacent_prior_realized_close'
    return close / previous_close - 1, None


def period_evidence(store, marker, snapshot, previous_session):
    tick = marker.tick
    world = snapshot.data['closed_economy']
    book = snapshot.data['economic_runtime']
    journal = defaultdict(list)
    for entry in book['journal']:
        if entry['tick'] == tick or entry['kind'] == 'security_mark':
            journal[entry['kind']].append(entry)
    session = world['security_session']
    session = session if session is not None and session['tick'] == tick else None
    close = session['close_minor'] if session else None
    previous_close = previous_session['close_minor'] if previous_session else None
    period_return, missing = realized_return(close, previous_close)
    mark = next(iter(journal['security_mark']), None)
    decisions = {d.actor_id: d for d in snapshot.decisions if d.tick == tick}
    receipts = [r for r in snapshot.receipts if r.tick == tick]
    observations = store.observation_rows(marker)
    information = {}
    households = []
    for obs in observations:
        actor = obs.actor_id
        information[actor] = {
            'accounts': len(obs.account_view), 'market': len(obs.market_view),
            'memory_refs': len(obs.private_memory_refs), 'delivered_messages': len(obs.delivered_messages),
            'consumption_disclosures': sum(m.body.get('kind') == 'disclosed_consumption'
                                           for m in obs.delivered_messages),
        }
        if not actor.startswith('household:'):
            continue
        decision = decisions[actor]
        wealth = next((f for f in obs.market_view if f.value.get('kind') == 'security_wealth'), None)
        response = decision.belief_patch.get('security_wealth_response')
        if wealth is not None:
            if (wealth.owner_id != actor or wealth.public or mark is None or mark['tick'] >= tick
                    or wealth.value['mark_tx_id'] != mark['tx_id'] or wealth.fact_id not in decision.evidence_ids
                    or response is None or response['evidence_id'] != wealth.fact_id):
                raise ValueError('Household judgment lacks an authorized committed mark')
        elif response is not None:
            raise ValueError('Household judged a security mark outside its observation')
        purchases = [intent.model_dump(mode='json') for intent in decision.economic_intents
                     if intent.kind == 'purchase']
        households.append({
            'actor_id': actor, 'observation_id': obs.observation_id, 'decision_id': decision.decision_id,
            'wealth_fact': wealth.model_dump(mode='json') if wealth is not None else None,
            'judgment': response, 'choice': decision.belief_patch['closed_choice'],
            'purchase_intents': purchases,
            'evidence_ids': list(decision.evidence_ids),
            'delivered_consumption_message_ids': [m.message_id for m in obs.delivered_messages
                                                  if m.body.get('kind') == 'disclosed_consumption'],
        })
    exposed = {row['actor_id']: row['wealth_fact']['value']['units']
               for row in households if row['wealth_fact'] is not None}
    expected_exposure = {actor: units for actor, units in book['security_units'].items()
                         if units and actor.startswith('household:') and mark is not None and mark['tick'] < tick}
    if exposed != expected_exposure:
        raise ValueError('Authorized exposure does not match the committed household security positions')

    consume_receipts = {}
    for receipt in receipts:
        if receipt.outcome == 'consumed':
            tx_id, = receipt.ledger_tx_ids
            if tx_id in consume_receipts:
                raise ValueError('One consumption transaction has multiple successful receipts')
            consume_receipts[tx_id] = (receipt.actor_id, receipt.executed_quantity)
    consumption = {e['tx_id']: (e['payload']['actor_id'], e['payload']['quantity_units'])
                   for e in journal['consumption']}
    macro = world['macro_history'][tick - 1]
    consumed = sum(quantity for _, quantity in consumption.values())
    if len(households) != store.identity.config['households'] or macro['tick'] != tick:
        raise ValueError('Period household denominators or clock are incomplete')
    if consume_receipts != consumption or macro['consumed_units'] != consumed:
        raise ValueError('Consumption journal, receipts, and period aggregate disagree')
    firm_sales = {actor: {'units': 0, 'income_minor': 0, 'purchase_tx_ids': []}
                  for actor in macro['sold_units_by_seller']}
    purchases = {entry['tx_id']: entry for entry in journal['purchase']}
    purchased = {tx_id: receipt for receipt in receipts if receipt.outcome == 'purchased'
                 for tx_id in receipt.ledger_tx_ids}
    if set(purchased) != set(purchases):
        raise ValueError('Purchase journal and successful receipts disagree')
    for tx_id, entry in purchases.items():
        receipt = purchased[tx_id]
        buyer = entry['payload']['buyer_id']
        if (receipt.actor_id, receipt.decision_id, receipt.executed_quantity) != (
                buyer, decisions[buyer].decision_id, entry['payload']['quantity_units']):
            raise ValueError('Purchase settlement does not match the acting buyer decision')
        seller = firm_sales[entry['payload']['seller_id']]
        seller['units'] += entry['payload']['quantity_units']
        seller['income_minor'] += entry['details']['gross_minor']
        seller['purchase_tx_ids'].append(entry['tx_id'])
    for actor, sales in firm_sales.items():
        if (sales['units'], sales['income_minor']) != (
                macro['sold_units_by_seller'][actor], macro['sales_minor_by_seller'][actor]):
            raise ValueError('Firm sales differ from settled purchases')

    return {
        'tick': tick, 'commit_hash': marker.commit_hash, 'state_hash': marker.state_hash,
        'asset': {'mark': mark, 'session': session, 'realized_close_minor': close,
                  'previous_session_tx_ids': previous_session['journal_tx_ids'] if previous_session else [],
                  'realized_return': period_return, 'return_missing_reason': missing},
        'information_counts_by_actor': information,
        'observation_hash': content_hash([o.model_dump(mode='json') for o in observations]),
        'households': households, 'receipts': [r.model_dump(mode='json') for r in receipts],
        'firms': firm_sales, 'labor': [row for row in world['labor_events'] if row['tick'] == tick],
        'institutions': {kind: journal[kind] for kind in ('bank', 'credit_application', 'fiscal')},
        'consumption_tx_ids': sorted(consumption), 'consumed_units': consumed,
        'denominators': {'households': len(households), 'wealth_observations': sum(
            row['wealth_fact'] is not None for row in households), 'firms': len(firm_sales),
            'labor_events': sum(row['tick'] == tick for row in world['labor_events']),
            'banks': len(store.identity.config['banking']['initial_bank_cash']),
            'treasuries': 1, 'consumption_receipts': len(consume_receipts),
            'security_sessions': int(session is not None), 'adjacent_return_windows': int(tick > 1)},
    }


def trace_world(store):
    previous_session = None
    periods = {}
    markers = {}
    for marker, snapshot in store.states():
        markers[str(marker.tick)] = marker.state_hash
        if marker.tick:
            row = period_evidence(store, marker, snapshot, previous_session)
            for entry in row['institutions']['fiscal']:
                response = entry['details'].get('consumption_response')
                if response is not None:
                    prior = periods[str(marker.tick - 1)]
                    if (response['published_tick'] != prior['tick']
                            or sorted(response['source_tx_ids']) != prior['consumption_tx_ids']
                            or response['consumed_units'] != prior['consumed_units']
                            or response['triggered'] != (prior['consumed_units'] < response['floor_units'])):
                        raise ValueError('Fiscal response does not match prior settled consumption')
            periods[str(marker.tick)] = row
            previous_session = row['asset']['session']
    return {'config_hash': store.identity.config_hash, 'rng': store.identity.rng,
            'data_origin': store.identity.metadata['data_origin'],
            'state_hashes': markers, 'periods': periods, 'independent_ledger_audit': audit_institutions(snapshot)}


def evaluate_root(protocol, traces):
    if set(traces) != set(protocol['arm_settings']):
        return {'status': 'ineligible', 'reasons': ['incomplete_factorial']}
    clock = protocol['clock']
    primary_tick = str(clock['primary_tick'])
    primary = {arm: row['periods'][primary_tick] for arm, row in traces.items()}
    t2 = {arm: row['periods'][str(clock['mark_tick'])]['asset'] for arm, row in traces.items()}
    initial = [{key: row['state_hashes'][str(key)] for key in (0, *clock['warmup_ticks'])}
               for row in traces.values()]
    matching = {
        'same_initial_and_warmup': all(state == initial[0] for state in initial),
        'same_random_address_space': len({content_hash(row['rng']) for row in traces.values()}) == 1,
        'same_prefunded_session': len({content_hash(row['session']) for row in t2.values()}) == 1,
        'same_primary_information_counts': len({content_hash(row['information_counts_by_actor'])
                                                for row in primary.values()}) == 1,
        'exact_information_within_mark_pairs': all(primary[prefix + '_linear']['observation_hash']
                                                    == primary[prefix + '_weighted']['observation_hash']
                                                    for prefix in ('flat', 'shock')),
    }
    fields = protocol['frozen_variables']
    checks = {}
    for arm, settings in protocol['arm_settings'].items():
        loss = fields['shock_drop_minor'] if settings['shock_enabled'] else 0
        mark = t2[arm]['mark']
        valuations = [row for row in primary[arm]['households'] if row['wealth_fact'] is not None]
        checks[arm] = (
            mark is not None and not mark['postings'] and not mark['goods_movements']
            and mark['payload']['price_minor'] == fields['initial_price_minor'] - loss
            and bool(valuations)
            and all(row['wealth_fact']['value']['asset_id'] == fields['security_asset']
                    and row['judgment']['lambda_bps'] == settings['lambda_bps']
                    and row['judgment']['reference_value_minor'] ==
                    fields['initial_price_minor'] * row['wealth_fact']['value']['units']
                    and row['judgment']['market_value_minor'] == row['wealth_fact']['value']['market_value_minor']
                    and row['judgment']['loss_minor'] == loss * row['wealth_fact']['value']['units']
                    and row['judgment']['reserve_increase_minor'] ==
                    row['judgment']['loss_minor'] * settings['lambda_bps'] // 10000 for row in valuations))
    endpoint = {arm: row['consumed_units'] for arm, row in primary.items()}
    low, high = protocol['primary_endpoint']['range']
    matching['registered_endpoint_range'] = all(low <= n <= high for n in endpoint.values())
    reasons = [key for key, ok in matching.items() if not ok]
    reasons += [arm + '_manipulation_not_verified' for arm, ok in checks.items() if not ok]
    return {'status': 'ineligible' if reasons else 'eligible', 'reasons': reasons, 'matching': matching,
            'manipulation': checks, 'endpoint_by_arm': endpoint}


def paired_summary(roots, contrast):
    differences = [{'root': int(root), 'difference': sum(weight * row['endpoint_by_arm'][arm]
                                                          for arm, weight in contrast.items())}
                   for root, row in roots.items() if row['status'] == 'eligible']
    values = [row['difference'] for row in differences]
    n = len(values)
    mean = statistics.fmean(values) if n else None
    sd = statistics.stdev(values) if n > 1 else None
    mcse = sd / n**0.5 if sd is not None else None
    radius = float(student_t.ppf(0.975, n - 1)) * mcse if mcse else None
    return {'assigned_roots': len(roots), 'n_complete_roots': n, 'differences': differences,
            'mean': mean, 'sample_sd': sd, 'mcse': mcse,
            'ci95': [mean - radius, mean + radius] if radius is not None else None,
            'interval_reason': 'insufficient_independent_roots' if n < 2 else 'zero_observed_sd' if sd == 0 else None,
            'observed_range': [min(values), max(values)] if values else None}


async def run_pilot(protocol_path, destination):
    protocol, base = load_protocol(protocol_path)
    bound_paths = ['scripts/joint_shock_pilot.py', protocol['base_fixture'], 'examples/joint-shock-protocol.v1.json',
                   'reproduction/runtime/source/src/civitas', 'reproduction/runtime/source/core/experiments']
    if subprocess.check_output(['git', 'status', '--porcelain', '--', *bound_paths], cwd=ROOT, text=True):
        raise ValueError('Commit the registered runner and runtime inputs before running the pilot')
    git_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    implementation = economic_run.current_implementation()
    budget = protocol['budget']
    assignments = {(seed, arm): assigned_configuration(protocol, base, seed, arm)
                   for seed in protocol['roots'] for arm in protocol['arm_settings']}
    if (len(protocol['roots']) > budget['maximum_roots'] or len(assignments) > budget['maximum_worlds']
            or sum(cfg.ticks for cfg in assignments.values()) > budget['maximum_world_ticks']
            or sum(cfg.ticks for (seed, _), cfg in assignments.items() if seed == budget['verification_root'])
            > budget['maximum_replay_ticks']):
        raise ValueError('The assigned worlds exceed the frozen pilot budget')
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=False)
    plan = {'protocol': protocol, 'protocol_sha256': REGISTERED_HASH,
            'protocol_commit': REGISTRATION_COMMIT, 'git_commit': git_commit,
            'runner_sha256': file_hash(Path(__file__)), 'implementation': implementation,
            'python': platform.python_version(), 'scipy': version('scipy'),
            'assignments': [{'root': seed, 'arm': arm, 'config_hash': content_hash(config.model_dump(mode='json'))}
                            for (seed, arm), config in assignments.items()]}
    versions = sorted({f"{item.metadata['Name']}=={item.version}" for item in distributions()
                       if item.metadata['Name'].lower() != 'ecosim-research'}, key=str.lower)
    locked = root / 'requirements.lock'
    locked.write_text('\n'.join(versions) + '\n')
    plan['requirements_sha256'] = file_hash(locked)
    atomic_json(root / 'plan.json', {'plan': plan, 'plan_hash': content_hash(plan)}, replace=False)
    manifest = {f'{seed}/{arm}': {'root': seed, 'arm': arm, 'status': 'not_started'}
                for seed, arm in assignments}
    manifest_path = root / 'execution.json'
    atomic_json(manifest_path, manifest)
    traces = defaultdict(dict)
    start = time.perf_counter()
    for (seed, arm), config in assignments.items():
        case = manifest[f'{seed}/{arm}']
        path = root / 'worlds' / str(seed) / arm
        case.update(status='running', path=str(path.relative_to(root)))
        atomic_json(manifest_path, manifest)
        started = time.perf_counter()
        try:
            checkpoint = budget['checkpoint_tick'] if seed == budget['verification_root'] else None
            run = await economic_run.run_new(config, path, until=checkpoint, run_id=f'r{seed}-{arm}')
            if checkpoint is not None:
                if (run['status'], run['tick']) != ('checkpointed', checkpoint):
                    raise ValueError('Execution did not reach the assigned checkpoint')
                case['checkpoint'] = run
                run = await economic_run.resume_run(path)
            case['run'] = run
            if run['status'] != 'completed' or run['tick'] != config.ticks:
                case['status'] = run['status'] if run['status'] != 'completed' else 'incomplete'
                continue
            store = CommitStore(path)
            if run['external_api_calls'] or store.archived_records() or store.identity.metadata['provider_mode'] != 'rule':
                raise ValueError('Pilot detected a model invocation in the rule budget')
            if (store.identity.code_hash != implementation['source_hash']
                    or store.identity.config_hash != content_hash(config.model_dump(mode='json'))):
                raise ValueError('Runtime source or assigned configuration drifted during the pilot')
            trace = trace_world(store)
            atomic_json(path / 'pilot-trace.json', trace, replace=False)
            case.update(trace=str((path / 'pilot-trace.json').relative_to(root)),
                        trace_sha256=file_hash(path / 'pilot-trace.json'),
                        state_hash=run['state_hash'], external_api_calls=0, model_attempts=0, monetary_model_cost_minor=0)
            if checkpoint is not None:
                replay = await economic_run.replay_run(path, destination=path.parent / (arm + '-replay'))
                case['replay'] = replay
                if (replay['status'], replay['compared_ticks'], replay['external_api_calls'],
                        replay['applied_deltas_to_execution'], replay['state_hash']) != (
                        'matched', config.ticks, 0, False, run['state_hash']):
                    raise ValueError('Independent execution replay differs from the assigned world')
            traces[seed][arm] = trace
            case['status'] = 'completed'
        except Exception as exc:
            case.update(status='audit_failed' if case.get('run', {}).get('status') == 'completed' else 'failed',
                        error_type=type(exc).__name__, error=str(exc))
        finally:
            case['wall_seconds'] = time.perf_counter() - started
            atomic_json(manifest_path, manifest)
    roots = {str(seed): evaluate_root(protocol, traces[seed]) for seed in protocol['roots']}
    report = {'status': 'completed' if all(row['status'] == 'eligible' for row in roots.values()) else 'incomplete',
              'inference_status': protocol['analysis']['inference_status'],
              'primary_contrast': protocol['analysis']['primary_contrast'], 'plan_hash': content_hash(plan),
              'assigned_worlds': len(manifest), 'world_status_counts': dict(Counter(c['status'] for c in manifest.values())),
              'roots': roots, 'contrasts': {key: paired_summary(roots, weights)
                                           for key, weights in protocol['analysis']['contrast_weights'].items()},
              'replays_matched': sum(case.get('replay', {}).get('status') == 'matched' for case in manifest.values()),
              'market_denominators': dict(Counter(period['asset']['return_missing_reason'] or 'observed_return'
                    for by_arm in traces.values() for trace in by_arm.values() for period in trace['periods'].values())),
              'wall_seconds': time.perf_counter() - start}
    atomic_json(root / 'report.json', report, replace=False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, default=REGISTERED)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run_pilot(args.protocol, args.output))
    print(f"{report['status']} · {report['inference_status']} · {args.output / 'report.json'}")
    return int(report['status'] != 'completed')


if __name__ == '__main__':
    raise SystemExit(main())
