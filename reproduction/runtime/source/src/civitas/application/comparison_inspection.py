"""Two run archives at explicitly selected ticks, with no write path to either."""
from pathlib import Path

from civitas.application.inspection import (
    _frame, _inspection_document, _inspection_store, _inspection_ticks,
)
from civitas.contracts.base import content_hash
from civitas.economy.fiscal import TREASURY
from civitas.storage.commit_store import json_bytes
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
    macro = snapshot.data.get('closed_economy', {}).get('last_macro', {})
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
