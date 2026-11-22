"""Independently verify the browser export or short thousand-household replay."""
import argparse
from pathlib import Path

from civitas.application.economic_run import verify_implementation
from civitas.application.workbench import job_key
from civitas.contracts.base import content_hash
from civitas.contracts.observations import Observation
from civitas.experiments.institutions import audit_institutions
from civitas.scenarios.closed_economy_config import LaborShiftIntervention
from civitas.storage.commit_store import CommitStore, json_bytes, read_json
from civitas.storage.history import file_hash

from verify_joint_shift import freeze, period


def workbench(workspace, exported, intervention, receipt=None):
    pair = read_json(exported)
    shift = LaborShiftIntervention.model_validate(read_json(intervention))
    runs = workspace.resolve()/'runs'

    def open_job(key):
        path = runs/job_key(key)
        if path.resolve() != path:
            raise ValueError('Run archives must stay in the supplied workspace')
        store = CommitStore(path)
        verify_implementation(store)
        return store

    if pair.get('audience') != 'researcher' or len(pair['arms']) != 2:
        raise ValueError('Download does not identify two researcher arms')
    left, right = pair['arms']
    ancestor = left['plan']['parent']
    if ancestor is None or right['plan']['parent'] != ancestor:
        raise ValueError('Both browser arms need the same parent')
    parent = open_job(ancestor['run_id'])
    plan = freeze(parent, shift)
    tick = shift.shock.tick
    if (pair.get('tick') != tick or pair.get('shared_parent_commit') != plan['parent']['commit_hash']
            or ancestor != {k:v for k,v in plan['parent'].items() if k != 'path'}):
        raise ValueError('Downloaded ancestry or period differs from the frozen parent')
    downloads = {}
    for arm in pair['arms']:
        side, = [name for name, terms in plan['arms'].items() if arm['plan']['intervention'] == terms]
        downloads[side] = arm
    if len(downloads) != 2 or left['job'] == right['job']:
        raise ValueError('Download requires the registered control and treatment')
    if receipt is not None:
        browser = read_json(receipt)
        if (browser['parent'] != ancestor['run_id'] or browser['parent_commit'] != ancestor['commit_hash']
                or browser['arms'] != {side:arm['job'] for side,arm in downloads.items()}
                or browser['export_sha256'] != file_hash(exported)):
            raise ValueError('Download does not identify the declared browser session')
    workers = {f'household:{i:04d}' for i in shift.shock.household_indices}
    results = {}
    for side in ('control','short_shift'):
        download = downloads[side]
        store = open_job(download['job'])
        frozen = download['plan']
        expected_config = {'config': store.identity.config, 'config_hash': store.identity.config_hash,
                           'source_hash': store.identity.code_hash, 'terminal_tick': plan['terminal_tick'],
                           'dependency_hash': store.identity.metadata['implementation']['dependency_hash']}
        if any(frozen[name] != value for name,value in expected_config.items()):
            raise ValueError('Downloaded plan differs from the frozen arm identity')
        history = store.markers()
        metadata = store.identity.metadata
        if (metadata.get('parent') != plan['parent'] or metadata.get('intervention') != plan['arms'][side]
                or history[0].state_hash != plan['baseline_state_hash']
                or history[-1].tick != plan['terminal_tick'] or store.identity.config != plan['config']
                or store.identity.rng != plan['rng']):
            raise ValueError('Arm differs from the assigned common parent, horizon or treatment')
        marker, state = store.load(tick)
        expected = workers if side == 'short_shift' else set()
        actual = period(store, marker, state, workers, expected, shift.shock.work_minutes)
        report = download['report']
        view, = report['views']
        cursor = view['cursor']
        if (report['run_id'] != store.identity.run_id or cursor['tick'] != tick
                or cursor['source_commit_hash'] != marker.commit_hash or cursor['state_hash'] != marker.state_hash
                or cursor['source_hash'] != store.identity.code_hash):
            raise ValueError('Downloaded cursor differs from the committed arm')
        mapping = {'wages_paid_minor':'wage_paid_minor','produced_units':'produced_units','consumed_units':'consumed_units'}
        if any(view['comparison_metrics'][key] != actual['measures'][field] for key, field in mapping.items()):
            raise ValueError('Downloaded period metrics differ from paid journal flows')
        displayed, = (s['rows'] for s in view['sections'] if s['key'] == 'journal')
        journal = [[r['tx_id'],r['kind'],len(r['postings']),r['details']]
                   for r in state.data['economic_runtime']['journal'] if r['tick'] == tick]
        if content_hash(displayed) != content_hash(journal):
            raise ValueError('Downloaded journal rows differ from the committed journal')
        results[side] = actual
    start, end = (results[side]['measures']['targeted_wage_paid_minor'] for side in results)
    return {'status':'matched','plan_hash':content_hash(plan),'source_hash':parent.identity.code_hash,
            'parent_commit':plan['parent']['commit_hash'],'paired_tick':tick,'arms':results,
            'export_sha256':file_hash(exported),'targeted_wage_effect_minor':end-start}


def mixed(root, replay_path):
    store = CommitStore(root)
    verify_implementation(store)
    records = store.markers()
    replay = read_json(replay_path)
    config, meta = store.identity.config, store.identity.metadata
    if (config['households'] != 1000 or config['firms'] != 40 or config['ticks'] != 4
            or meta['provider_mode'] != 'archived_response' or meta['external_api_calls'] != 0
            or [m.tick for m in records] != list(range(5))):
        raise ValueError('Run does not cover the four-period thousand-household archive assignment')
    final, state = store.load(4)
    if (replay['status'] != 'matched' or replay['initial_tick'] != 0 or replay['compared_ticks'] != 4
            or replay['source_commit_hash'] != final.commit_hash or replay['state_hash'] != final.state_hash
            or replay['code_hash'] != store.identity.code_hash or replay['applied_deltas_to_execution']
            or replay['external_api_calls'] != 0):
        raise ValueError('Independent replay does not match the frozen run')
    calls = store.archived_records()
    limits = meta['budget_limits']
    seats = {f'household:{i:04d}' for i in config['supplier_cognition']['household_indices']}
    marker, impact = store.load(2)
    observations = {o.actor_id:o for o in store.observation_rows(marker) if o.actor_id in seats}
    choices = {d.actor_id:d for d in impact.decisions if d.tick == 2 and d.actor_id in seats}
    attached = [d for d in state.decisions if 'model_supplier' in d.belief_patch]
    if (len(calls) != len(attached) or len(calls) != len(seats) or limits['max_attempts'] != len(seats)
            or limits['usd_nano'] or limits['cny_nano']):
        raise ValueError('Model attempts differ from the frozen zero-fee seats')
    selected = {}
    for call in calls:
        request = call['request']
        projected = Observation.model_validate(request['observation'])
        actor = projected.actor_id
        full, decision = observations[actor], choices[actor]
        trace = decision.belief_patch['model_supplier']
        if (actor in selected or projected.visible_until.tick != 2 or request['task'] != 'cd_supplier'
                or call['state'] != 'success' or call['accounted'] or trace['attempt_id'] != call['attempt_id']
                or decision.observation_id != full.observation_id or not projected.evidence_ids <= full.evidence_ids):
            raise ValueError('Archived response is not bound to one authorized purchase observation')
        payments = [r for r in impact.receipts if r.tick == 2 and r.actor_id == actor and r.outcome == 'purchased']
        selected[actor] = {'attempt_id':call['attempt_id'],'observation_id':full.observation_id,
                           'seller':decision.belief_patch['closed_choice']['chosen_seller'],
                           'paid_minor':-sum(r.cash_delta_minor for r in payments),
                           'quantity':sum(r.executed_quantity for r in payments),
                           'ledger_tx_ids':[key for r in payments for key in r.ledger_tx_ids]}
    if not any(row['quantity'] and row['ledger_tx_ids'] for row in selected.values()):
        raise ValueError('The archived policy did not drive a purchase on any registered seat')
    observations_by_period = [{k:getattr(mark,k) for k in ('tick','commit_hash','state_hash')} for mark in records]
    return {'status':'matched','source_hash':store.identity.code_hash,'config_hash':store.identity.config_hash,
            'archive_hash':meta['response_archive_hash'],'budgets':limits,'states':observations_by_period,
            'decisions':len(state.decisions),'execution_receipts':len(state.receipts),
            'banks':len(config['banking']['initial_bank_cash']),'government':int(config['fiscal'] is not None),
            'model_attempts':selected,'external_api_calls':0,'economic_audit':audit_institutions(state)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    browser = modes.add_parser('workbench')
    for name in ('workspace','download','intervention'):
        browser.add_argument('--'+name, type=Path, required=True)
    browser.add_argument('--receipt',type=Path,help='optional browser automation receipt')
    scale = modes.add_parser('mixed')
    scale.add_argument('run', type=Path)
    scale.add_argument('--replay', type=Path, required=True)
    args = parser.parse_args()
    result = (workbench(args.workspace,args.download,args.intervention,args.receipt) if args.mode=='workbench'
              else mixed(args.run,args.replay))
    print(json_bytes(result).decode(),end='')


if __name__ == '__main__':
    main()
