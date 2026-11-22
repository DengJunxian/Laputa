"""Freeze or independently audit the joint-world paired labor shift experiment."""
import argparse
from collections import Counter
import json
from pathlib import Path

from civitas.application.economic_run import NO_INTERVENTION, parse_config, verify_implementation
from civitas.contracts.base import content_hash
from civitas.experiments.institutions import audit_institutions
from civitas.experiments.registry import history
from civitas.scenarios.closed_economy_config import LaborShiftIntervention
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from civitas.storage.history import file_hash, parent_reference


def freeze(parent, intervention):
    config = parse_config(parent.identity.config)
    if (parent.identity.metadata.get('parent') or config.labor_income_shock is not None
            or not config.archive_observations or config.banking is None or config.fiscal is None):
        raise ValueError('The experiment requires a pristine joint world with archived observations')
    terminal = parent.identity.metadata.get('terminal_tick', config.ticks)
    if intervention.shock.tick > terminal:
        raise ValueError('The shift lies outside the frozen horizon')
    marker, _ = parent.load(intervention.shock.tick - 1)
    return {'schema_version': 1, 'design': 'joint_world.paired_labor_shift.v1',
            'parent': parent_reference(parent, marker), 'baseline_state_hash': marker.state_hash,
            'source_hash': parent.identity.code_hash, 'config': parent.identity.config,
            'rng': parent.identity.rng, 'terminal_tick': terminal,
            'arms': {'control': NO_INTERVENTION, 'short_shift': intervention.model_dump(mode='json')},
            'primary': 'impact_period_targeted_wage_paid_minor',
            'contrast': 'short_shift_minus_control', 'independent_unit': 'parent_random_root'}


def period(store, marker, snapshot, intended_workers, expected_workers, work_minutes):
    tick = marker.tick
    state = snapshot.data['economic_runtime']
    entries = [row for row in state['journal'] if row['tick'] == tick]
    wages = Counter()
    payments = {}
    for row in entries:
        if row['kind'] == 'wage_payment':
            worker = row['payload']['worker_id']
            wages[worker] += sum(p['amount_minor'] for p in row['postings']
                                 if (p['entity_id'], p['account_code'], p['side']) == (worker, 'cash', 'debit'))
            payments[row['tx_id']] = row
    flows = Counter()
    for row in entries:
        for movement in row['goods_movements']:
            flows[movement['kind']] += movement['quantity_units']
    labor = [row for row in snapshot.data['closed_economy']['labor_events'] if row['tick'] == tick]
    affected = {row['worker_id']: row for row in labor if row.get('income_shock')}
    if set(affected) != expected_workers or any(row['delivered_minutes'] != work_minutes for row in affected.values()):
        raise ValueError('The actual shortened shifts differ from the frozen assignment')

    reached = []
    observed_bank_notices = []
    observed_fiscal_notices = []
    for observation in store.observation_rows(marker):
        for fact in observation.market_view:
            if fact.visible_at.tick != tick:
                continue
            kind = fact.value.get('kind')
            actor = observation.actor_id
            if kind == 'labor_income_shock':
                row = affected.get(actor)
                if (row is None or fact.public or fact.owner_id != actor or
                        fact.value['payment_tx_id'] != row['payment_tx_id'] or
                        fact.value['payment_tx_id'] not in payments or
                        fact.value['paid_minor'] != wages[actor] or fact.value['delivered_minutes'] != work_minutes):
                    raise ValueError('Workshift observation is not backed by an authorized actual payment')
                reached.append(actor)
            elif kind in ('verified_withdrawal_delay', 'withdrawal_window_notice'):
                observed_bank_notices.append(actor)
            elif kind == 'fiscal_announcement':
                observed_fiscal_notices.append(actor)
    if sorted(reached) != sorted(expected_workers):
        raise ValueError('The authorized workshift observations do not match the actual recipients')

    bank = [row for row in entries if row['kind'] == 'bank']
    noticed = sorted(actor for row in bank if row['payload']['action'] == 'notice' for actor in row['details']['reached'])
    if noticed != sorted(observed_bank_notices):
        raise ValueError('The realized bank disclosure does not match the authorized observations')
    fiscal = [row['details'] for row in entries if row['kind'] == 'fiscal']
    fiscal_recipients = sorted(actor for row in fiscal for actor in row['reached'])
    if fiscal_recipients != sorted(observed_fiscal_notices):
        raise ValueError('The realized fiscal announcement does not match the authorized observations')
    audited = audit_institutions(snapshot)
    withdrawals = state['bank_positions']['withdrawals'].values()
    measures = {'worked_minutes': sum(row['delivered_minutes'] for row in labor),
                'wage_paid_minor': wages.total(), 'targeted_wage_paid_minor': sum(wages[a] for a in intended_workers),
                'produced_units': flows['production'], 'consumed_units': flows['consumption'],
                'labor_notice_reached': len(reached), 'bank_notice_reached': len(noticed),
                'bank_disclosure_paid_minor': sum(row['details'].get('disclosure_paid_minor', 0) for row in bank),
                'withdrawal_paid_minor': sum(row['details'].get('paid_minor', 0) for row in bank
                                             if row['payload']['action'] == 'queue'),
                'withdrawal_queued_minor': sum(req['remaining_minor'] for req in withdrawals if req['status'] == 'queued'),
                'tax_collected_minor': sum(row['tax_collected_minor'] for row in fiscal),
                'fiscal_notice_reached': len(fiscal_recipients),
                'fiscal_disclosure_paid_minor': sum(row['disclosure_paid_minor'] for row in fiscal),
                'fiscal_transfers_paid_minor': sum(row['transfers_paid_minor'] for row in fiscal),
                'treasury_cash_minor': audited['fiscal']['cash_remaining_minor'],
                'bank_reserves_minor': audited['banking']['reserves_minor'], 'bank_deposits_minor': audited['banking']['deposits_minor']}
    return {'tick': tick, 'commit_hash': marker.commit_hash, 'state_hash': marker.state_hash,
            'shift_recipients': sorted(reached), 'bank_notice_recipients': noticed,
            'fiscal_notice_recipients': fiscal_recipients, 'measures': measures}


def audit_arm(root, name, plan, intended_workers, intervention):
    path = root / name
    if not path.exists():
        return {'status': 'not_started', 'failed_attempts': 0, 'periods': []}
    store = CommitStore(path)
    initial = store.markers()[0]
    metadata = store.identity.metadata
    if (metadata.get('parent') != plan['parent'] or metadata.get('intervention') != plan['arms'][name]
            or initial.state_hash != plan['baseline_state_hash'] or initial.tick != plan['parent']['tick']
            or metadata.get('terminal_tick', store.identity.config['ticks']) != plan['terminal_tick']
            or store.identity.config != plan['config'] or store.identity.rng != plan['rng']
            or store.identity.code_hash != plan['source_hash']):
        raise ValueError(f'{name} does not match the frozen assignment')
    periods = []
    for marker, snapshot in store.states():
        if marker.tick > plan['terminal_tick']:
            raise ValueError(f'{name} passed the frozen terminal period')
        if marker.kind == 'tick':
            expected = intended_workers if name == 'short_shift' and marker.tick == intervention.shock.tick else set()
            periods.append(period(store, marker, snapshot, intended_workers, expected, intervention.shock.work_minutes))
    attempts = history(path)
    return {'status': 'completed' if marker.tick == plan['terminal_tick'] else 'incomplete',
            'run_id': store.identity.run_id, 'head_tick': marker.tick,
            'failed_attempts': sum(row['status'] in ('failed', 'cancelled') for row in attempts),
            'periods': periods}


def audit(root):
    frozen = read_json(root / 'plan.json')
    plan = frozen['plan']
    parent = CommitStore(root / 'parent')
    intervention = LaborShiftIntervention.model_validate(plan['arms']['short_shift'])
    if content_hash(plan) != frozen['plan_hash'] or plan != freeze(parent, intervention):
        raise ValueError('Frozen plan or parent commit differs')
    workers = {f'household:{index:04d}' for index in intervention.shock.household_indices}
    arms = {name: audit_arm(root, name, plan, workers, intervention) for name in plan['arms']}
    completed = sum(row['status'] == 'completed' for row in arms.values())
    effects = []
    summary = None
    if completed == 2:
        for control, treatment in zip(arms['control']['periods'], arms['short_shift']['periods'], strict=True):
            if control['tick'] != treatment['tick']:
                raise ValueError('The assigned comparison periods do not align')
            effects.append({'tick': control['tick'], 'delta': {key: treatment['measures'][key] - value
                                                              for key, value in control['measures'].items()}})
        flow_keys = ('worked_minutes', 'wage_paid_minor', 'targeted_wage_paid_minor', 'produced_units',
                     'consumed_units', 'withdrawal_paid_minor', 'tax_collected_minor', 'fiscal_transfers_paid_minor')
        summary = {'primary_effect_minor': effects[0]['delta']['targeted_wage_paid_minor'],
                   'impact_period': effects[0], 'last_period': effects[-1],
                   'cumulative_flow_differences': {key: sum(row['delta'][key] for row in effects) for key in flow_keys}}
    return {'status': 'complete' if completed == 2 else 'incomplete', 'plan_hash': frozen['plan_hash'],
            'assigned_arms': 2, 'completed_arms': completed, 'assigned_pairs': 1, 'complete_pairs': int(completed == 2),
            'arms': arms, 'paired_period_effects': effects, 'summary': summary,
            'plan_file_sha256': file_hash(root / 'plan.json')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--freeze', type=Path, help='create a fresh plan using this intervention JSON')
    args = parser.parse_args()
    if args.freeze is not None:
        target = args.root / 'plan.json'
        if target.exists():
            raise FileExistsError(target)
        parent = CommitStore(args.root / 'parent')
        verify_implementation(parent)
        plan = freeze(parent, LaborShiftIntervention.model_validate_json(args.freeze.read_bytes()))
        report = {'plan': plan, 'plan_hash': content_hash(plan)}
        atomic_json(target, report)
    else:
        report = audit(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
