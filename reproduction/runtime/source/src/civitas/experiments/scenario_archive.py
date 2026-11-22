"""Recompute an offline scenario suite from its assigned worlds and commit chains."""
import asyncio
from importlib.metadata import version
from pathlib import Path

from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.experiments.institutions import audit_institutions
from civitas.experiments.labor_factorial import LaborPilotSpec, _report, _stopping_schedule
from civitas.experiments.scenario_catalog import (
    ANALYSIS_VERSION, CATALOG, CONTRASTS, ScenarioSuite, _d_case, _evaluate_case,
    _suite_configurations, _suite_report,
)
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.storage.commit_store import CommitStore, read_json

SUPPORTED_ANALYSES = ('wp10.five-scene.v1', 'wp10.five-scene.v2', ANALYSIS_VERSION)
ARCHIVE_ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError, AssertionError)


def _archive_path(root, relative):
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError('Archive paths must stay inside the frozen run without symbolic links')
    return path


def _open_world(root, path, config, end, implementation):
    relative = str(path.relative_to(root))
    store = CommitStore(_archive_path(root, relative))
    if (store.identity.code_hash != implementation['source_hash']
            or store.identity.metadata['implementation']['dependency_hash'] != implementation['dependency_hash']
            or store.identity.config_hash != content_hash(config)
            or store.identity.metadata.get('terminal_tick', config.ticks) != end):
        raise ValueError('World does not match the frozen treatment, implementation or terminal tick')
    return store


def _replay_receipt(root, path, store, head, end):
    if head.tick != end:
        raise ValueError('Commit chain has not reached its assigned terminal tick')
    relative = str(path.relative_to(root))
    report = read_json(_archive_path(root, relative + '-replay/report.json'))
    required = {'status': 'matched', 'compared_ticks': head.tick, 'initial_tick': 0,
                'final_tick': head.tick, 'state_hash': head.state_hash,
                'source_commit_hash': head.commit_hash, 'code_hash': store.identity.code_hash,
                'applied_deltas_to_execution': False, 'external_api_calls': 0}
    if any(report.get(key) != value for key, value in required.items()):
        raise ValueError('Independent replay receipt does not match the final verified marker')
    return report


def _recompute_d(root, case_path, config, seeds, implementation, failures, verified):
    spec = LaborPilotSpec(schema_version=14,base_config=config,seeds=seeds)
    stopping = _stopping_schedule(spec)
    replays = {}

    def assigned_store(path, arm_config, end):
        relative = str(path.relative_to(root))
        try:
            store = _open_world(root,path,arm_config,end,implementation)
            head = store.markers()[-1]
            replay = _replay_receipt(root,path,store,head,end)
            replays[str(path.relative_to(case_path))] = {'status':'completed','replay':replay}
            verified[relative] = head.tick
            return store
        except ARCHIVE_ERRORS as exc:
            failures[relative] = str(exc)
            raise

    report = _report(case_path,None,spec,stopping,open_store=assigned_store)
    for row in report['runs']:
        relative = str((case_path/row['relative_path']).relative_to(root))
        if row['status'] != 'completed':
            verified.pop(relative,None)
            failures.setdefault(relative,row.get('error',f"World execution status is {row['status']}"))
    return _d_case(report,replays)


def _historic_case(result, scene, analysis):
    if analysis != ANALYSIS_VERSION:
        if scene == 'B':
            for row in result['rows'].values():
                for field in ('notice_tx_id','social_response_mode','reaction_receipts','reaction_payments'):
                    row.pop(field,None)
        elif scene == 'C':
            for row in result['rows'].values():
                for decision in row.get('consumption_trace',[]):
                    for field in ('evidence_ids','linked_delivery_receipts','decision_receipts'):
                        decision.pop(field,None)
    if analysis == 'wp10.five-scene.v1' and scene == 'E' and 'fiscal_feedback' in result:
        feedback = result['fiscal_feedback']
        rows = feedback['rows']
        feedback['gate'] = feedback['completed_worlds']==3 and (
            all(r['rule'] is None and r['paid_minor']>0 for r in rows['fixed']['responses'])
            and any(r['rule']['triggered'] and r['paid_minor']>0 for r in rows['published_floor']['responses'])
            and all(not r['rule']['triggered'] and r['paid_minor']==0 for r in rows['floor_met']['responses']))
        for field in ('triggered_payments','floor_satisfied_periods'):
            feedback.pop(field)
        result['gate'] = result['fixed_dose']['valid'] and result['same_gross_budget']['valid'] and feedback['gate']
    return result


def _case_hash(case):
    # Persisted absolute paths describe where execution ran before an archive was moved.
    portable = {**case,'rows':{arm:{k:v for k,v in row.items() if k!='path'}
                               for arm,row in case['rows'].items()}}
    if 'fiscal_feedback' in portable:
        portable['fiscal_feedback'] = {**case['fiscal_feedback'],'rows':{
            arm:{k:v for k,v in row.items() if k!='path'} for arm,row in case['fiscal_feedback']['rows'].items()}}
    return content_hash(portable)


async def _audit_cases(root, planned, plan, failures, verified):
    implementation = plan['implementation']

    async def read_world(raw, path, *, checkpoint_tick):
        relative = str(path.relative_to(root))
        try:
            config = ClosedEconomyConfig.model_validate(raw)
            store = _open_world(root,path,config,config.ticks,implementation)
            periods = []
            for head, snapshot in store.states():
                if head.tick:
                    periods.append({'tick':head.tick,'state_hash':head.state_hash,**audit_institutions(snapshot)})
            replay = _replay_receipt(root,path,store,head,config.ticks)
            world = build_closed_economy(config)
            world.restore_snapshot(snapshot)
            verified[relative] = head.tick
            return world, {'status':'completed','path':str(path),'config_hash':store.identity.config_hash,
                           'periods':periods,'replay':replay}
        except ARCHIVE_ERRORS as exc:
            failures[relative] = str(exc)
            return None, {'status':'failed','path':str(path),'error':str(exc)}

    recomputed, archived = [], []
    for assignment, raw in planned:
        scene, name = assignment['scenario'], assignment['path']
        case_path = _archive_path(root,name)
        config = ClosedEconomyConfig.model_validate(raw)
        if scene == 'D':
            computed = _recompute_d(root,case_path,config,assignment['seeds'],implementation,failures,verified)
        else:
            computed = await _evaluate_case(scene,config,case_path,assignment['boundary'],run_world=read_world)
        _historic_case(computed,scene,plan['version'])
        recomputed.append(assignment | computed)
        try:
            case = read_json(_archive_path(root,f'{name}/case.json'))
            archived.append(assignment | case)
            if _case_hash(computed) != _case_hash(case):
                failures[name] = 'Case result differs from independent computation from committed states'
        except ARCHIVE_ERRORS as exc:
            failures[name] = str(exc)
    return recomputed,archived


def audit_suite(destination):
    """Validate declared treatments, replay receipts and statistics without running any worlds."""
    root = Path(destination).resolve()
    sealed = read_json(_archive_path(root,'plan.json'))
    plan = sealed['plan']
    plan_hash = content_hash(plan)
    if (sealed['plan_hash'] != plan_hash or plan['version'] not in SUPPORTED_ANALYSES
            or plan['external_api_calls'] != 0 or tuple(plan['contrast_family']) != CONTRASTS):
        raise ValueError('Invalid or unsupported scenario plan')
    scenarios = list(dict.fromkeys(row['scenario'] for row in plan['assignments']))
    if not scenarios or not set(scenarios)<=set(CATALOG) or set(scenarios)!=set(plan['catalog']):
        raise ValueError('Invalid frozen scenario selection')
    planned = list(_suite_configurations(ScenarioSuite.model_validate(plan['spec']),scenarios))
    if [r for r,_ in planned] != plan['assignments']:
        raise ValueError('Frozen assignments do not match the suite specification')
    failures, verified = {}, {}
    try:
        report = read_json(_archive_path(root,'report.json'))
        if report['plan_hash']!=plan_hash or report['scenario_ids']!=scenarios or report['external_api_calls']!=0:
            raise ValueError('Report refers to a different plan')
    except ARCHIVE_ERRORS as exc:
        failures['report.json'] = str(exc)
        report = {}

    computed, archived = asyncio.run(_audit_cases(root,planned,plan,failures,verified))
    summary = _suite_report(computed,scenarios,plan_hash,analysis_version=plan['version'])
    if report:
        if content_hash(archived) != content_hash(report.get('cases')):
            failures['report.json'] = 'Summary cases differ from retained case reports'
        dependencies = plan['research_dependencies']
        if any(version(name)!=dependencies.get(name) for name in ('scipy','statsmodels')):
            failures['inference'] = 'Installed analysis dependencies differ from the frozen plan'
        elif content_hash(summary['inference']) != content_hash(report.get('inference')):
            failures['inference'] = 'Reported contrasts differ from independent ledger computation'
        fields = summary.keys() - {'cases','inference'}
        if {k:summary[k] for k in fields}!={k:report.get(k) for k in fields}:
            failures['report.json'] = 'Summary gates or denominators differ from independent computation'
    return {'status':'failed' if failures else 'completed','archive_status':report.get('status'),
            'plan_hash':plan_hash,'assigned_worlds':summary['assigned_worlds'],'verified_worlds':len(verified),
            'verified_ticks':sum(verified.values()),'recomputed_inference':summary['inference'],
            'failures':failures}
