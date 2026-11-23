"""Two run archives at explicitly selected ticks, with no write path to either."""
from pathlib import Path

from civitas.application.inspection import (
    _cursor, _frame, _inspection_document, _inspection_store, _inspection_ticks,
)
from civitas.contracts.base import content_hash
from civitas.economy.fiscal import TREASURY
from civitas.experiments.registry import history
from civitas.storage.commit_store import json_bytes, read_json
from civitas.storage.history import publish_directory


def _metrics(snapshot, tick):
    security = snapshot.data.get('security_market')
    if security is not None:
        period = security['history'][-1] if security['history'] else {}
        return {'security_close_minor': period.get('close_minor'),
                'security_volume_units': period.get('volume_units', 0),
                'security_fill_count': period.get('fill_count', 0)}
    economy = snapshot.data['economic_runtime']
    balances = economy['balances']
    journal = [e for e in economy['journal'] if e['tick'] == tick]
    bank = [e for e in journal if e['kind'] == 'bank']
    fiscal = [e['details'] for e in journal if e['kind'] == 'fiscal']
    disclosures = [e['details'] for e in bank if 'disclosure_by_recipient_minor' in e['details']]
    support = [e['details'] for e in bank if e['payload']['action'] in ('support_disburse', 'support_collect')]
    scenario = snapshot.data.get('closed_economy', {})
    macro = scenario.get('last_macro', {})
    has_bank = any(entity.startswith('bank:') for entity in balances)
    disbursed = sum(d.get('disbursed_minor', 0) for d in support)
    recovered = sum(d.get('bank_repaid_minor', 0) + d.get('guarantee_paid_minor', 0) for d in support)
    disclosure_cost = sum(d['disclosure_paid_minor'] for d in disclosures)
    program_cost = sum(d['transfers_paid_minor'] + d['disclosure_paid_minor'] for d in fiscal) + disclosure_cost
    return {
        'wages_paid_minor': macro.get('wage_paid_minor'),
        'produced_units': macro.get('produced_units'),
        'consumed_units': macro.get('consumed_units'),
        'deposit_claims_minor': sum(b.get('deposit_asset', 0) for b in balances.values()) if has_bank else None,
        'bank_reserves_minor': sum(b.get('bank_reserve', 0) for b in balances.values()) if has_bank else None,
        'disclosure_recipients': len({actor for d in disclosures for actor in d['reached']}) if has_bank else None,
        'disclosure_paid_minor': disclosure_cost if has_bank else None,
        'support_disbursed_minor': disbursed if has_bank else None,
        'support_recovered_minor': recovered if has_bank else None,
        'treasury_program_net_outlay_minor': (
            program_cost + disbursed - recovered
            if TREASURY in balances else None
        ),
        'treasury_cash_minor': balances.get(TREASURY, {}).get('cash'),
    }


def joint_timeline(store):
    if not store.identity.config.get('security_feedback'):
        raise ValueError('Select a joint security feedback world')
    periods = []
    for marker, snapshot in store.states():
        if marker.tick == 0:
            continue
        economy = snapshot.data['economic_runtime']
        scenario = snapshot.data['closed_economy']
        session = scenario['security_session'] or {}
        mark = next((entry for entry in economy['journal'] if entry['kind'] == 'security_mark'), None)
        sales = scenario['last_macro']['sales_minor_by_seller']
        macro = _metrics(snapshot, marker.tick)
        periods.append({'cursor': _cursor(store, marker), 'metrics': {
            'security_mark_minor': mark['payload']['price_minor'] if mark else None,
            'realized_close_minor': session.get('close_minor') if session.get('tick') == marker.tick else None,
            'consumed_units': macro['consumed_units'], 'firm_sales_minor': sum(sales.values()),
            'wages_paid_minor': macro['wages_paid_minor'], 'bank_reserves_minor': macro['bank_reserves_minor'],
            'treasury_cash_minor': macro['treasury_cash_minor'],
        }, 'sales_by_firm_minor': sales})
    return periods


# The existing pilot's canonical protocol content binds this one registered contrast.
JOINT_PILOT_PROTOCOL = '32ed1d383c8b42aa375152dd4b8ef908ccc471664909185dbf6c683b670a25b1'


def joint_pilot_comparison(root):
    root = Path(root).resolve(strict=True)

    def local(relative):
        path = root / relative
        if path.resolve(strict=True) != path:
            raise ValueError('Pilot archives must use their registered local paths')
        return path

    frozen = read_json(local('plan.json'))
    plan = frozen['plan']
    if (content_hash(plan) != frozen['plan_hash']
            or content_hash(plan['protocol']) != JOINT_PILOT_PROTOCOL):
        raise ValueError('Pilot plan does not match the frozen joint shock protocol')
    execution = read_json(local('execution.json'))
    seed = plan['protocol']['budget']['verification_root']
    arms, opening = [], []
    for arm in ('flat_weighted', 'shock_weighted'):
        key = f'{seed}/{arm}'
        path = local('worlds/' + key)
        store = _inspection_store(path)
        identity = store.identity
        expected, = [row['config_hash'] for row in plan['assignments']
                     if (row['root'], row['arm']) == (seed, arm)]
        status = execution[key]
        setting = plan['protocol']['arm_settings'][arm]
        if (status['status'] != 'completed' or status['path'] != 'worlds/' + key
                or (status['root'], status['arm']) != (seed, arm)
                or identity.config_hash != expected or identity.config['root_seed'] != seed
                or identity.config['security_feedback']['shock_enabled'] != setting['shock_enabled']
                or identity.config['reference_loss']['lambda_bps'] != setting['lambda_bps']
                or identity.code_hash != plan['implementation']['source_hash']
                or identity.metadata['implementation']['code_commit'] not in (None, plan['git_commit'])
                or identity.metadata['provider_mode'] != 'rule' or history(path)[-1]['status'] != 'completed'):
            raise ValueError('Pilot arm does not match its registered completed execution')
        periods = joint_timeline(store)
        terminal = periods[-1]['cursor'] if periods else {}
        if (terminal.get('tick') != plan['protocol']['clock']['terminal_tick']
                or terminal.get('state_hash') != status['state_hash']):
            raise ValueError('Pilot arm terminal commit differs from its execution receipt')
        opening.append([m.state_hash for m in store.markers() if m.tick <= 1])
        arms.append({'arm': arm, 'seed': seed, 'data_origin': identity.metadata['data_origin'],
                     'commit': plan['git_commit'], 'source_hash': identity.code_hash, 'run_status': status['status'],
                     'periods': periods})
    if opening[0] != opening[1]:
        raise ValueError('Pilot arm initial and warmup states diverge')
    differences = []
    for left, right in zip(*(arm['periods'] for arm in arms), strict=True):
        tick = left['cursor']['tick']
        if tick != right['cursor']['tick']:
            raise ValueError('Pilot arm clocks differ')
        metric_delta = {key: right['metrics'][key] - value
                        if value is not None and right['metrics'][key] is not None else None
                        for key, value in left['metrics'].items()}
        differences.append({'tick': tick, 'metrics': metric_delta})
    return {'audience': 'researcher', 'schema_version': 1, 'authority': False,
            'protocol_id': plan['protocol']['protocol_id'], 'plan_hash': frozen['plan_hash'],
            'arms': arms, 'difference': 'shock_weighted - flat_weighted', 'differences': differences}


def _read_arm(store, ticks, audience, actor, baseline_tick):
    frames = {tick: {'status': 'uncommitted'} for tick in ticks}
    baseline_hash = None
    actor_registered = False
    for marker, snapshot in store.states():
        tick = marker.tick
        if tick == baseline_tick:
            state = snapshot.model_dump(mode='json')
            state['data']['economic_runtime'].pop('institution_config_hash', None)
            baseline_hash = content_hash(state)
        if tick in frames:
            actor_registered |= actor in snapshot.actors
            if audience == 'agent' and (actor not in snapshot.actors or marker.observations is None):
                reason = 'actor_unregistered' if actor not in snapshot.actors else 'unrecorded_observe'
                frames[tick] = {'status': reason}
            else:
                frame = _frame(store, marker, snapshot, audience, actor)
                if audience == 'researcher':
                    frame['comparison_metrics'] = _metrics(snapshot, tick)
                frames[tick] = {'status': 'available', 'view': frame}
        if tick == ticks[-1]:
            break
    return frames, baseline_hash, actor_registered


def write_comparison(left, right, destination, ticks, *, audience, actor=None, baseline_tick=None, labels=None):
    ticks = _inspection_ticks(ticks, audience, actor)
    if baseline_tick is not None and (audience != 'researcher' or type(baseline_tick) is not int
                                     or baseline_tick not in ticks):
        raise ValueError('Choose a requested tick as the researcher baseline')
    roots = [Path(root).resolve() for root in (left, right)]
    destination = Path(destination).resolve()
    if roots[0] == roots[1] or any(destination.is_relative_to(root) for root in roots):
        raise ValueError('Comparison requires two source runs and an output outside both')
    if destination.exists():
        raise FileExistsError(destination)
    stores = [_inspection_store(root) for root in roots]
    names = [store.identity.run_id for store in stores]
    labels = names if labels is None else labels
    if len(labels) != 2 or any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ValueError('Choose two nonempty run labels')
    frames, hashes, registrations = zip(*(
        _read_arm(store, ticks, audience, actor, baseline_tick) for store in stores
    ))
    if audience == 'agent' and not any(registrations) and any(
        frame['status'] != 'uncommitted' for arm in frames for frame in arm.values()
    ):
        raise ValueError('Actor is not registered at any requested commit')
    data = {'schema_version': 1, 'authority': False, 'audience': audience, 'actor_id': actor,
            'arms': [{'id': name, 'label': label} for name, label in zip(names, labels)],
            'periods': [{'tick': tick, 'arms': [arm[tick] for arm in frames]} for tick in ticks]}
    if audience == 'researcher':
        data['root_seeds'] = [store.identity.config.get('root_seed') for store in stores]
        if baseline_tick is not None:
            status = 'unavailable' if None in hashes else ('matched' if hashes[0] == hashes[1] else 'different')
            data['baseline'] = {'tick': baseline_tick, 'status': status, 'hashes': hashes,
                                'excluded_field': 'data.economic_runtime.institution_config_hash'}
    from .inspection_html import _browser_integers, render
    with publish_directory(destination) as staging:
        for period in data['periods']:
            for side, store, frame in zip(('left', 'right'), stores, period['arms']):
                if frame['status'] == 'available':
                    name = f"{side}-{period['tick']}.json"
                    report = _inspection_document(store, audience, actor, [frame['view']])
                    payload = json_bytes(report)
                    (staging/name).write_bytes(payload)
                    frame['export'] = name
                    if _browser_integers(report) != report:
                        frame['exact_export'] = payload.decode()
        (staging/'index.html').write_text(render(data), encoding='utf-8')
    return {'path': str(destination/'index.html'), 'audience': audience, 'actor_id': actor, 'ticks': ticks,
            'available_by_arm': [sum(f['status'] == 'available' for f in arm.values()) for arm in frames]}
