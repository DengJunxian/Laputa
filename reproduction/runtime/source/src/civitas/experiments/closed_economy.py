"""Paired, synthetic mechanism checks using the existing v4 world unchanged.

The independent unit is a complete seeded world. Agents and ticks are not
replicates. Monte Carlo standard errors describe these finite synthetic runs;
they are not confidence intervals for real populations or causal estimates.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import statistics
import time

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import current_implementation
from civitas.contracts.base import content_hash
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.storage.commit_store import atomic_json


ARMS = {
    'adaptive_fault': {'social_mode':'adaptive', 'fault_tick':3},
    'static_fault': {'social_mode':'static', 'fault_tick':3},
    'no_peer_fault': {'social_mode':'none', 'fault_tick':3},
    'adaptive_no_fault': {'social_mode':'adaptive', 'fault_tick':None},
}
CONTRASTS = (
    ('adaptive_fault', 'static_fault'),
    ('adaptive_fault', 'no_peer_fault'),
    ('adaptive_fault', 'adaptive_no_fault'),
)
OUTCOMES = (
    'consumed_units_total', 'goods_total_units', 'scrapped_units_total',
    'wage_outstanding_minor', 'mean_active_fulfillment_bps',
    'fault_seller_sales_after_tick3_minor', 'fault_seller_choices_after_tick3',
    'peer_consumptions_total',
)


def audit_snapshot(snapshot):
    """Independent aggregate oracle over recorded postings and resource flows."""
    state = snapshot.data['economic_runtime']
    cash_initial = sum(state['initial_cash'].values())
    cash_final = sum(b['cash'] for b in state['balances'].values())
    if cash_initial != cash_final:
        raise AssertionError('Closed settlement-token total changed')
    goods = dict(state['initial_goods'])
    flows = defaultdict(int)
    account_totals = defaultdict(int)
    transaction_groups = 0
    for entry in state['journal']:
        balance = defaultdict(int)
        for posting in entry['postings']:
            sign = 1 if posting['side'] == 'debit' else -1
            balance[posting['entity_id'], posting['currency_id']] += sign*posting['amount_minor']
            account_totals[posting['entity_id'], posting['account_code']] += sign*posting['amount_minor']
        if any(balance.values()):
            raise AssertionError('A journal transaction does not balance by owner/currency')
        transaction_groups += len(balance)
        for movement in entry['goods_movements']:
            if movement['kind'] == 'opening':
                continue  # The initial inventory is already in the oracle.
            quantity = movement['quantity_units']
            source, target = movement['from_owner'], movement['to_owner']
            if source is not None:
                goods[source] -= quantity
            if target is not None:
                goods[target] += quantity
            flows[movement['kind']] += quantity
    if goods != dict(state['goods']) or any(value < 0 for value in goods.values()):
        raise AssertionError('Journal goods flows and recorded stock disagree')
    for actor, balance in state['balances'].items():
        if sum(balance.values()) != 0 or balance['cash'] < 0:
            raise AssertionError('Owner trial balance or liquidity constraint failed')
        if any(value != account_totals[actor, code] for code, value in balance.items()):
            raise AssertionError('An owner account differs from summed journal postings')
    if 'security_market' in snapshot.data:
        history = snapshot.data['security_market']['history']
        if set(flows) - {'transfer'} or flows['transfer'] != sum(row['volume_units'] for row in history):
            raise AssertionError('Security session volumes differ from journal movements')
        if sum(goods.values()) != sum(state['initial_goods'].values()):
            raise AssertionError('Listed security units changed')
        from civitas.scenarios.security_market import validate_security_receipts

        validate_security_receipts(snapshot)
        return {'cash_initial_minor':cash_initial, 'cash_final_minor':cash_final,
                'security_final_units':sum(goods.values()), 'security_volume_units':flows['transfer'],
                'balanced_entity_currency_transactions':transaction_groups, 'status':'passed'}
    macro = snapshot.data['closed_economy']['macro_history']
    for kind, metric in (('production','produced_units'), ('consumption','consumed_units'),
                         ('scrap','scrapped_units')):
        if flows[kind] != sum(row[metric] for row in macro):
            raise AssertionError('Macro resource totals differ from journal movements')
    return {'cash_initial_minor':cash_initial, 'cash_final_minor':cash_final,
            'goods_final_units':sum(goods.values()), 'flows_units':dict(flows),
            'balanced_entity_currency_transactions':transaction_groups,
            'status':'passed'}


def summarize_world(world):
    metrics = world.scenario.metrics(world._context(world.snapshot))
    scenario = world.snapshot.data['closed_economy']
    social = world.snapshot.data['closed_social']
    seller = world.scenario.firms[0]
    metrics['fault_seller_sales_after_tick3_minor'] = sum(
        row['sales_minor_by_seller'].get(seller, 0)
        for row in scenario['macro_history'] if row['tick'] > 3)
    metrics['fault_seller_choices_after_tick3'] = sum(
        row.get('chosen_seller') == seller
        for row in social['trace'] if row['kind'] == 'decision' and row['tick'] > 3)
    metrics['realized_fault_commands'] = [command for row in scenario['fault_trace']
                                        for command in row['realized_commands']]
    return metrics


def paired_summary(rows, seeds):
    by_key = {(row['seed'], row['arm']):row for row in rows}
    contrasts = []
    for left, right in CONTRASTS:
        eligible, excluded = [], []
        for seed in seeds:
            pair = [by_key.get((seed, arm)) for arm in (left, right)]
            if any(row is None or row['status'] != 'completed' for row in pair):
                excluded.append({'seed':seed,'reason':'incomplete_pair'})
            elif pair[0]['population_hash'] != pair[1]['population_hash']:
                excluded.append({'seed':seed,'reason':'population_mismatch'})
            elif any(row['arm'].endswith('_fault') and row['arm'] != 'adaptive_no_fault'
                     and len(row['metrics']['realized_fault_commands']) != 1 for row in pair):
                excluded.append({'seed':seed,'reason':'scheduled_fault_not_realized_once'})
            else:
                eligible.append((seed, *pair))
        outcomes = {}
        for key in OUTCOMES:
            values = [a['metrics'][key]-b['metrics'][key] for _, a, b in eligible]
            n = len(values)
            sd = statistics.stdev(values) if n > 1 else None
            outcomes[key] = {'n_world_pairs':n,
                'differences':[{'seed':s,'left_minus_right':a['metrics'][key]-b['metrics'][key]}
                               for s,a,b in eligible],
                'mean':statistics.fmean(values) if n else None,
                'sample_sd':sd, 'monte_carlo_se':sd/n**0.5 if sd is not None else None,
                'minimum':min(values) if n else None, 'maximum':max(values) if n else None}
        contrasts.append({'left':left,'right':right,'expected_pairs':len(seeds),
                          'excluded_pairs':excluded,'outcomes':outcomes})
    return contrasts


async def compare_conditions(destination, *, seeds, ticks=8, households=8):
    if (not seeds or any(type(seed) is not int or seed < 0 for seed in seeds)
            or len(set(seeds)) != len(seeds)):
        raise ValueError('Seeds must be distinct nonnegative integers')
    if type(ticks) is not int or ticks < 6:
        raise ValueError('At least six ticks are required to observe the two-step disclosure delay')
    # Validate all dimensions before creating an output directory.
    base = ClosedEconomyConfig(ticks=ticks, households=households)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    implementation = current_implementation()
    plan = {'schema_version':1,'experiment_id':'closed-economy-mechanism-check-v1',
        'data_origin':'synthetic_engineering_parameters','independent_unit':'seeded_world',
        'base_config':base.model_dump(mode='json'),'seeds':list(seeds),'arms':ARMS,
        'contrasts':[list(pair) for pair in CONTRASTS],'outcomes':list(OUTCOMES),
        'implementation':implementation,'external_api_calls':0,
        'interpretation':{
            'static_fault':'Frozen supplier fulfillment weights; fixed contacts and information delivery retained',
            'no_peer_fault':'No peer disclosure; individual receipt learning retained',
            'adaptive_no_fault':'Adaptive learning with no scheduled quality failure',
            'uncertainty':'Descriptive Monte Carlo variation across synthetic worlds, no empirical p-values',
            'persistence':'Per-tick hashes and final states; these experiment outputs are not commit stores'}}
    atomic_json(destination/'plan.json', plan)
    rows = []
    for seed in seeds:
        for arm, settings in ARMS.items():
            folder = destination/f'seed-{seed}'/arm
            folder.mkdir(parents=True)
            config = base.model_copy(update={'root_seed':seed, **settings})
            atomic_json(folder/'config.json', config.model_dump(mode='json'))
            started = time.perf_counter()
            world, history = None, []
            row = {'seed':seed,'arm':arm,'status':'running','external_api_calls':0,
                   'config_hash':content_hash(config),'relative_path':folder.relative_to(destination).as_posix()}
            try:
                world = build_closed_economy(config)
                row['population_hash'] = world.snapshot.data['closed_economy']['population_hash']
                for _ in range(config.ticks):
                    await world.step()
                    invariants = audit_snapshot(world.snapshot)
                    history.append({'tick':world.snapshot.tick,
                        'state_hash':content_hash(world.snapshot),'invariants':invariants,
                        'metrics':summarize_world(world)})
                row.update(status='completed', state_hash=content_hash(world.snapshot),
                           metrics=summarize_world(world), invariant_checks=len(history))
            except Exception as exc:
                row.update(status='failed', error=type(exc).__name__+': '+str(exc),
                           completed_ticks=0 if world is None else world.snapshot.tick)
            row['elapsed_seconds'] = time.perf_counter()-started
            if world is not None:
                atomic_json(folder/'final_state.json', world.snapshot.model_dump(mode='json'))
            atomic_json(folder/'trace.json', history)
            atomic_json(folder/'result.json', row)
            rows.append(row)
            # Preserve the denominator and all completed/failed arms throughout.
            atomic_json(destination/'progress.json', {'expected_worlds':len(seeds)*len(ARMS),'runs':rows})
    after = current_implementation()
    stable = (after['source_hash'],after['dependency_hash']) == (
        implementation['source_hash'],implementation['dependency_hash'])
    contrasts = paired_summary(rows, seeds)
    all_complete = all(row['status']=='completed' for row in rows)
    complete_pairs = all(not pair['excluded_pairs'] for pair in contrasts)
    report = {'schema_version':1,'experiment_id':plan['experiment_id'],
        'status':'completed' if all_complete and complete_pairs and stable else 'failed',
        'expected_worlds':len(seeds)*len(ARMS),'completed_worlds':sum(r['status']=='completed' for r in rows),
        'source_identity_stable':stable,'source_hash':implementation['source_hash'],
        'dependency_hash':implementation['dependency_hash'],'external_api_calls':0,
        'runs':rows,'paired_contrasts':contrasts,'interpretation':plan['interpretation']}
    atomic_json(destination/'report.json', report)
    return report
