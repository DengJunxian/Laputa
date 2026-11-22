"""Frozen two-arm interventions and read-only evidence from the authoritative journal."""
from civitas.application.comparison_inspection import _metrics
from civitas.application.inspection import _cursor
from civitas.contracts.base import content_hash
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_relations import SOCIAL_FIELD, materialize_social
from civitas.storage.commit_store import CommitStore, read_json

KINDS = {
    'income': ('收入收缩', 2, 2, ('labor_income_shock',)),
    'liquidity': ('银行流动性冲击', 2, 4, ('verified_withdrawal_delay', 'withdrawal_window_notice')),
    'revaluation': ('证券重估', 2, 2, ('security_market', 'security_signal')),
    'fiscal': ('财政调整', 2, 3, ('fiscal_announcement', 'fiscal_settlement')),
}
ARMS = {'control': '对照', 'treatment': '干预'}


def recipe(kind, side):
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(side, str) or side not in ARMS:
        raise ValueError('Choose a registered intervention and arm')
    active = side == 'treatment'
    config = dict(ticks=5, households=4, firms=2, root_seed=7,
                  fault_tick=None, archive_observations=True)
    if kind == 'revaluation':
        config['securities'] = {'shock_tick': 2, 'fundamental_drop_minor': 18 if active else 5,
                                'binding_maker_bid': 'fundamental'}
    else:
        config.update(initial_household_cash_minor=4, cash_jitter_minor=0,
                      initial_price_minor=6, min_price_minor=6, max_price_minor=6, firm_price_gap_minor=0)
    if kind == 'income' and active:
        config['labor_income_shock'] = {'tick': 2, 'household_indices': [0, 1], 'work_minutes': 240}
    if kind == 'liquidity':
        config['initial_household_cash_minor'] = 38
        config['banking'] = {
            'credit_creation_mode': 'bank_credit_creation',
            'initial_bank_cash': {'bank:a': 2, 'bank:b': 3},
            'operations': [
                {'tick': 1, 'operation_id': 'opening-deposit', 'kind': 'deposit',
                 'bank': 'bank:a', 'actor': 'household:0001', 'amount_minor': 9},
                {'tick': 1, 'operation_id': 'opening-credit', 'kind': 'approve',
                 'bank': 'bank:a', 'actor': 'household:0000', 'amount_minor': 20,
                 'installments': 1, 'payment_interval': 5},
                {'tick': 2, 'operation_id': 'first-delay', 'kind': 'withdraw',
                 'bank': 'bank:a', 'actor': 'household:0000', 'amount_minor': 15 if active else 1,
                 'expires_tick': 5},
                {'tick': 4, 'operation_id': 'reserve-inflow', 'kind': 'deposit',
                 'bank': 'bank:a', 'actor': 'household:0001', 'amount_minor': 12},
            ],
            'run_notice': {'tick': 3, 'source_request': 'first-delay',
                           'withdrawal_by_recipient': {'household:0001': 6},
                           'react_to_verified_delay': True, 'response_mode': 'actor'},
        }
    if kind == 'fiscal':
        config['fiscal'] = {'initial_cash_minor': 3, 'announced_tick': 2, 'effective_tick': 3,
                            'audience': [0, 1], 'disclosure_cost_minor': 2,
                            'brackets': [{'start_minor': 0, 'rate_bps': 2500 if active else 0}],
                            'transfer_recipients': [0, 1], 'transfer_minor': 3 if active else 0,
                            'precautionary_bps': 10000}
    return ClosedEconomyConfig.model_validate(config).model_dump(mode='json')


def registration(kind, side):
    config = recipe(kind, side)
    label, start, settlement, _ = KINDS[kind]
    if kind == 'income':
        dose = {'household_indices': [0, 1],
                'work_minutes': config.get('labor_income_shock', {}).get('work_minutes', 480)}
    elif kind == 'liquidity':
        amount = next(row['amount_minor'] for row in config['banking']['operations']
                      if row['operation_id'] == 'first-delay')
        dose = {'bank': 'bank:a', 'withdrawal_minor': amount}
    elif kind == 'revaluation':
        dose = {'fundamental_drop_minor': config['securities']['fundamental_drop_minor']}
    else:
        dose = {'rate_bps': config['fiscal']['brackets'][0]['rate_bps'],
                'transfer_per_household_minor': config['fiscal']['transfer_minor']}
    return {'kind': kind, 'arm': side, 'label': label + ' · ' + ARMS[side], 'change_tick': start,
            'settlement_tick': settlement, 'feedback_tick': settlement + 1, 'dose': dose,
            'config_hash': content_hash(config)}


def catalog():
    return [{'kind': kind, 'label': values[0], 'arms': [
        registration(kind, side) for side in ARMS]} for kind, values in KINDS.items()]


def _focus(kind, state):
    if kind == 'revaluation':
        return set(state.actors)
    if kind == 'fiscal':
        return {actor for actor in state.actors if actor.startswith('household:')}
    return {'household:0000', 'household:0001'}


def _flow(kind, tick, state, owners):
    journal = [row for row in state.data['economic_runtime']['journal'] if row['tick'] == tick]
    if kind == 'income':
        return [row for row in journal if row['kind'] == 'wage_payment'
                and row['payload']['worker_id'] in owners]
    if kind == 'fiscal':
        return [row for row in journal if row['kind'] == 'fiscal']
    if kind == 'revaluation':
        return [row for row in journal if row['kind'] == 'security_trade']
    return [row for row in journal
            if (row['kind'] == 'bank_withdrawal'
                or (row['kind'] == 'bank' and (
                    row['payload']['reference'] in ('first-delay', 'notice:household:0001')
                    or row['payload']['action'] == 'notice')))]


def _series(kind, tick, state, flow):
    series = _metrics(state, tick)
    if kind == 'income':
        series['affected_wage_paid_minor'] = sum(r['details']['paid_minor'] for r in flow)
    elif kind == 'liquidity':
        series['disclosure_recipients'] = sum(len(r['details']['reached']) for r in flow
                                             if r['payload'].get('action') == 'notice')
        series['queued_minor'] = sum(r['payload']['intent']['amount_minor'] for r in flow
                                     if r['kind'] == 'bank_withdrawal')
        series['withdrawals_paid_minor'] = sum(r['details'].get('paid_minor', 0) for r in flow
                                                if r['payload'].get('action') == 'queue')
        positions = state.data['economic_runtime'].get('bank_positions', {})
        series['outstanding_minor'] = sum(x['remaining_minor'] for name, x in positions.get('withdrawals', {}).items()
                                          if name in ('first-delay', 'notice:household:0001'))
    elif kind == 'revaluation':
        market = state.data['security_market']
        series['fundamental_minor'] = market['fundamental_price_minor']
        series['security_paid_minor'] = sum(r['payload']['price_ticks'] * r['payload']['quantity_units']
                                            for r in flow)
    else:
        for key in ('tax_collected_minor', 'transfers_paid_minor', 'disclosure_paid_minor'):
            series[key] = sum(r['details'][key] for r in flow)
    return series


def _network(kind, tick, state, flow, observations):
    edges = []
    if SOCIAL_FIELD in state.data:
        social = materialize_social(state.data[SOCIAL_FIELD], tick)
        edges = [[actor, peer, 'communication', None] for actor, peers in social.outgoing_contacts().items()
                 for peer in peers]
    if kind == 'liquidity':
        positions = state.data['economic_runtime']['bank_positions']
        edges.extend([actor, bank, 'deposit', amount] for bank, claims in positions['deposits'].items()
                     for actor, amount in claims.items() if amount)
        edges.extend([loan['terms']['bank'], loan['terms']['actor'], 'credit', loan['principal_minor']]
                     for loan in positions['loans'].values() if loan.get('principal_minor'))
    elif kind == 'revaluation':
        edges.extend([r['payload']['seller_id'], r['payload']['buyer_id'], 'fill', r['payload']['quantity_units']]
                     for r in flow)
    edges.extend([message.sender_id, obs.actor_id, 'delivered_message', message.message_id]
                 for obs in observations for message in obs.delivered_messages)
    return edges


def _individual(kind, state, marker, observations, owners):
    tick = marker.tick
    decisions = {row.actor_id: row for row in state.decisions if row.tick == tick}
    receipts = {}
    for item in state.receipts:
        if item.tick == tick and item.actor_id in owners:
            receipts.setdefault(item.actor_id, []).append(item)
    facts = KINDS[kind][3]
    rows = []
    for obs in observations:
        if obs.actor_id not in owners:
            continue
        decision = decisions[obs.actor_id]
        rows.append({
            'actor': obs.actor_id, 'observation_id': obs.observation_id,
            'facts': [{'fact_id': f.fact_id, 'visible_tick': f.visible_at.tick, 'value': f.value}
                      for f in obs.market_view if f.value.get('kind') in facts],
            'feedback_facts': [f.model_dump(mode='json') for f in obs.market_view
                               if kind == 'liquidity' and f.value.get('kind') == 'bank_statement'],
            'choice': decision.belief_patch.get('security_appraisal' if kind == 'revaluation' else 'closed_choice', {}),
            'fiscal_precaution': decision.belief_patch.get('fiscal_precaution'),
            'evidence_ids': list(decision.evidence_ids),
            'intents': [row.model_dump(mode='json') for row in decision.economic_intents if row.kind in
                        ('purchase', 'withdraw_deposit', 'security_order')],
            'receipts': [row.model_dump(mode='json') for row in receipts.get(obs.actor_id, ())],
        })
    return rows


def evolution(store, kind, side, tick):
    plan = registration(kind, side)
    if plan['config_hash'] != store.identity.config_hash or store.identity.metadata.get('parent'):
        raise ValueError('Run does not match the registered intervention configuration')
    chosen = None
    series, prior_steps = [], []
    for marker, state in store.states():
        period = marker.tick
        owners = _focus(kind, state)
        flow = _flow(kind, period, state, owners)
        point = {'cursor': _cursor(store, marker), 'metrics': _series(kind, period, state, flow)}
        series.append(point)
        if period == tick or period >= plan['change_tick']:
            observations = store.observation_rows(marker)
            individuals = _individual(kind, state, marker, observations, owners)
            reached = [row['actor'] for row in individuals if row['facts']]
            step = {**point, 'individuals': individuals, 'visible_to': reached,
                    'ledger': [{'tx_id': row['tx_id'], 'kind': row['kind'], 'event': row['payload'],
                                'details': row['details'], 'postings': row['postings']} for row in flow]}
            if period == tick:
                chosen = {'plan': plan, **step, 'timeline': series, 'prior_steps': prior_steps,
                          'network': _network(kind, period, state, flow, observations)}
                break
            prior_steps.append(step)
    if chosen is None:
        raise ValueError('Evidence requires a committed period')
    return chosen


def _prior_hashes(store, kind, side, before):
    hashes = []
    drop = recipe(kind, side)['securities']['fundamental_drop_minor'] if kind == 'revaluation' else None
    for marker, state in store.states():
        if marker.tick >= before:
            break
        wire = state.model_dump(mode='json')
        # Bank/fiscal settings are part of ledger identity. Compare the same
        # substantive prefix while retaining both original hashes in the reports.
        if kind in ('liquidity', 'fiscal'):
            wire['data']['economic_runtime'].pop('institution_config_hash')
        if kind == 'revaluation':
            shock, = (row for row in wire['pending_events'] if row['kind'] == 'fundamental_shock')
            if (shock['tick'] != before
                    or shock['payload'].pop('fundamental_drop_minor') != drop):
                raise ValueError('Pending security shock differs from the registered treatment dose')
        hashes.append(content_hash([wire, [row.model_dump(mode='json') for row in store.observation_rows(marker)]]))
    return hashes


def audit_pair(kind, control, treatment, *, tick=None, download=None):
    from civitas.application.economic_run import open_run, verify_implementation
    from civitas.experiments.closed_economy import audit_snapshot

    stores = [CommitStore(path) for path in (control, treatment)]
    for store in stores:
        verify_implementation(store)
    tick = stores[0].markers()[-1].tick if tick is None else tick
    reports = [evolution(store, kind, side, tick) for store, side in zip(stores, ARMS, strict=True)]
    start = reports[0]['plan']['change_tick']
    left, right = (_prior_hashes(store, kind, side, start) for store, side in zip(stores, ARMS, strict=True))
    if left != right or len(left) != start:
        raise ValueError('Registered two-arm pre-intervention prefix does not match')
    if download is not None:
        pair = read_json(download)
        try:
            views = [arm['report']['views'][0] for arm in pair['arms']]
            recorded = {v['intervention_evolution']['plan']['arm']: v for v in views}
            bound = (pair['tick'] == tick and len(views) == len(reports)
                     and all(recorded[report['plan']['arm']]['cursor'] == report['cursor']
                             and content_hash(recorded[report['plan']['arm']]['intervention_evolution'])
                             == content_hash(report) for report in reports))
        except (KeyError, TypeError, IndexError):
            bound = False
        if not bound:
            raise ValueError('Downloaded intervention evidence differs from the requested original commits')
    return {'status': 'matched', 'kind': kind, 'tick': tick, 'prefix_states_matched': len(left),
            'feedback_reached': tick >= reports[0]['plan']['feedback_tick'],
            'download_verified': download is not None, 'arms': reports,
            'ledger_oracles': [audit_snapshot(open_run(store.root, tick=tick)[0].snapshot) for store in stores]}
