"""Registered A–E experimental contracts and durable verification."""
import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from importlib.metadata import version
from multiprocessing import get_context
from pathlib import Path
from statistics import fmean, stdev
from typing import Literal

from pydantic import Field, model_validator

from civitas.application import economic_run
from civitas.contracts.base import NonNegative, Record, content_hash, require_unique
from civitas.experiments import registry
from civitas.experiments.institutions import _run, compare_bank_channels, compare_fiscal_policies
from civitas.experiments.labor_factorial import LaborPilotSpec, run_pilot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.storage.commit_store import atomic_json, read_json

CATALOG = {
    'A': {'title':'Securities and social information','loops':['L1','L3'], 'toggles':['selective_exposure','herding'],
          'endpoint':'drawdown on actual trading closes after the shock and from the last pre-shock close',
          'boundary':'unfunded bids, no trade, missing close','crisis':'drawdown at least 25%'},
    'B': {'title':'Financial and social contagion','loops':['L4'], 'toggles':['guarantee_interbank','react_to_verified_delay'],
          'endpoint':'terminal depositor loss / pre-shock deposits',
          'boundary':'insufficient guarantee leaves uncovered due payment','crisis':'resolution or deposit loss'},
    'C': {'title':'Consumption and employment','loops':['L1','L3'], 'toggles':['peer_uplift_units','transfer_minor','hiring_adapts'],
          'endpoint':'fixed-price consumption per settled transfer; delivered employment rate',
          'boundary':'frozen sales baseline can leave the job unavailable','crisis':'wage arrears'},
    'D': {'title':'Trust and cooperation','loops':['L2'], 'toggles':['cumulative_trust','selective_rewiring','same_count_random'],
          'endpoint':'cooperative choices / eligible offered seats',
          'boundary':'natural stop before late opportunity','crisis':'failed funding or isolation'},
    'E': {'title':'Fiscal disclosure and transfer','loops':['L5'], 'toggles':['disclosure_audience','transfer_minor'],
          'endpoint':'necessary consumption gap; realized gross policy outlay',
          'boundary':'unfunded appropriation cannot finance disclosure or transfers','crisis':'rationed transfer budget',
          'feedback':'prior published consumption against a fixed floor, with the same treasury and maximum dose'},
}
ANALYSIS_VERSION = 'wp10.five-scene.v3'
CONTRASTS = ('A1', 'B1', 'C1_liquid', 'C2_employment')


class Assignment(Record):
    seeds: list[NonNegative] = Field(min_length=1)
    config: ClosedEconomyConfig

    @model_validator(mode='after')
    def unique(self):
        require_unique(self.seeds, 'scenario root')
        return self


class ScenarioSuite(Record):
    schema_version: Literal[1] = 1
    scenarios: dict[Literal['A','B','C','D','E'], Assignment]

    @model_validator(mode='after')
    def validate_domains(self):
        if set(self.scenarios) != set(CATALOG):
            raise ValueError('A complete registered suite requires A–E')
        cfg = {scene: row.config for scene,row in self.scenarios.items()}
        if (not cfg['A'].securities or not cfg['B'].banking or not cfg['C'].demand or not cfg['C'].fiscal
                or any(c.security_feedback is not None for c in cfg.values())
                or not cfg['C'].labor_transfer or not cfg['E'].fiscal or any(c.cognition_mode!='rule' for c in cfg.values())
                or any(cfg[s].stopping_hazard_bps is not None for s in ('A','B','C','E'))):
            raise ValueError('Registered configurations do not select their declared domains')
        bank_notice = cfg['B'].banking.run_notice
        if bank_notice is None or bank_notice.response_mode != 'scripted':
            raise ValueError('Registered B uses the frozen finance-by-social design')
        if any(cfg[s].fiscal.consumption_floor_units is not None for s in ('C','E')):
            raise ValueError('The fixed-dose comparisons require an unconditional transfer')
        if not cfg['A'].archive_observations or not cfg['A'].securities.maker_cash_minor:
            raise ValueError('A needs funded makers and archived exposure observations')
        c = cfg['C']
        if (c.initial_price_minor != c.min_price_minor or c.initial_price_minor != c.max_price_minor
                or c.firm_price_gap_minor or not c.fiscal.transfer_minor or not c.fiscal.transfer_recipients
                or not c.fiscal.effective_tick <= c.labor_transfer.offer_tick < c.ticks
                or c.labor_transfer.worker_index not in c.demand.initially_unemployed_indices):
            raise ValueError('C requires a single fixed price, a transfer before the vacancy closes and an eligible job seeker')
        LaborPilotSpec(schema_version=14, seeds=self.scenarios['D'].seeds, base_config=cfg['D'])
        return self


def _difference(a, b, c, d):
    return a - b - c + d if None not in (a,b,c,d) else None


def security_drawdown(history, shock):
    pre = [r['close_minor'] for r in history if r['tick'] < shock and r['close_minor'] is not None]
    later = [r['close_minor'] for r in history if r['tick'] >= shock and r['close_minor'] is not None]
    peak = pre[-1] if pre else None
    drawdown = None
    if peak is not None and later:
        drawdown = 0.
        for value in later:
            peak = max(peak,value)
            drawdown = max(drawdown,(peak-value)/peak)
    return drawdown


def _security_outcomes(world):
    snapshot = world.snapshot
    settings = world.scenario.settings
    history = snapshot.data['security_market']['history']
    shock = settings.shock_tick
    drawdown = security_drawdown(history, shock)
    appraisal = [dict(d.belief_patch['security_appraisal']) | {'tick':d.tick, 'actor_id':d.actor_id,
                 'observation_id':d.observation_id, 'decision_id':d.decision_id, 'evidence_ids':list(d.evidence_ids)}
                 for d in snapshot.decisions if d.tick >= shock and d.actor_id.startswith('investor:')]
    latest = settings.initial_price_minor
    prior_trade = {}
    for row in history:
        prior_trade[row['tick']] = latest
        if row['close_minor'] is not None:
            latest = row['close_minor']
    expected_count = world.scenario.config.households * (world.scenario.config.ticks-shock+1)
    valid = (len(appraisal)==expected_count and all(r['exposed_source'] and
        (not settings.selective_exposure or r['exposed_source'].endswith(':pessimistic')) and
        r['prior_trade_minor']==prior_trade[r['tick']] and len(r['evidence_ids'])==3 and
        r['applied_herding_minor'] == (r['raw_signal_minor'] if settings.herding else 0) for r in appraisal))
    return {'drawdown':drawdown,'crisis':drawdown is not None and drawdown>=.25,
            'history':history,'stock_and_cash_rejections':dict(Counter(reason for r in snapshot.receipts
                if r.status=='rejected' for reason in r.reason_codes)),
            'state_and_exposure_valid':valid,'appraisal':appraisal}


def _consumption_outcomes(world):
    snapshot, config = world.snapshot, world.scenario.config
    own = snapshot.data['closed_economy']
    fiscal_tick, offer_tick = config.fiscal.effective_tick, config.labor_transfer.offer_tick
    population = own['population']
    recipients = {f'household:{i:04d}' for i in config.fiscal.transfer_recipients}
    cohort = {tag: sorted(a for a in recipients if (population[a]['initial_cash_minor'] < 2*config.initial_price_minor)
                          == (tag=='tight')) for tag in ('tight','liquid')}
    consumed = {tag:sum(r.executed_quantity for r in snapshot.receipts if r.outcome=='consumed'
                       and fiscal_tick<=r.tick<=offer_tick and r.actor_id in members) for tag,members in cohort.items()}
    inputs = [e for e in snapshot.data['economic_runtime']['journal'] if e['kind']=='fiscal'
              and fiscal_tick<=e['tick']<=offer_tick]
    transfers = {tag:sum(sum(e['details']['transfers_minor'].get(a,0) for a in members)
                     for e in inputs) for tag,members in cohort.items()}
    decisions = [{'actor_id':d.actor_id, 'tick':d.tick, 'decision_id':d.decision_id,
                  'observation_id':d.observation_id, 'evidence_ids': list(d.evidence_ids),
                  **dict(d.belief_patch['closed_choice'])}
                 for d in snapshot.decisions if 'closed_choice' in d.belief_patch]
    from civitas.scenarios.closed_economy_demand import consumption_source

    messages = {message.message_id: message for message in snapshot.messages}
    receipts = {receipt.command_id: receipt for receipt in snapshot.receipts}
    downstream = {}
    for receipt in snapshot.receipts:
        downstream.setdefault(receipt.decision_id, []).append(receipt.command_id)
    for decision in decisions:
        decision['linked_delivery_receipts'] = {}
        decision['decision_receipts'] = downstream.get(decision['decision_id'], [])
        for reference in decision['consumption_message_ids']:
            message = messages[reference]
            source = consumption_source(message.body, receipts, message.sender_id, message.created_at.tick)
            if (source is None or message.recipient_id != decision['actor_id']
                    or message.created_at.tick != decision['tick']-1
                    or (decision['peer_uplift_units'] and reference not in decision['evidence_ids'])):
                raise ValueError('Consumption appraisal lacks an authorized, prior delivery receipt')
            decision['linked_delivery_receipts'][reference] = source.command_id
    jobs = [event for event in own['labor_events'] if event['tick']>offer_tick]
    denominator = len(population)*(config.ticks-offer_tick)
    return {'population_hash':own['population_hash'],'cohort':cohort,'consumed_units':consumed,
            'transfers_paid_minor':transfers,'employment_rate':len(jobs)/denominator,
            'employment_worker_periods':len(jobs),'employment_denominator':denominator,
            'vacancy_offer_tick':offer_tick,'candidate_contract':own['labor_contract'],
            'contract_work_events':[e for e in jobs if 'contract_id' in e],
            'sales_at_hiring_gate':own['macro_history'][offer_tick-2]['sold_units_by_seller'],
            'positive_peer_decisions':sum(r['peer_uplift_units']>0 for r in decisions),
            'peer_exposure_decisions':sum(bool(r['consumption_message_ids']) for r in decisions),
            'consumption_trace':decisions,
            'crisis_wage_arrears_minor':sum(w['outstanding_minor'] for w in
                                          snapshot.data['economic_runtime']['wage_obligations'])}


async def _factorial(scene, config, root, boundary, *, run_world=_run):
    base = config.model_dump(mode='json')
    rows = {}
    if scene == 'A':
        variants = [(f'S{s}H{h}', {'securities':{**base['securities'], 'selective_exposure':bool(s),
                     'herding':bool(h), **({'maker_cash_minor':0} if boundary else {})}})
                    for s in (0,1) for h in (0,1)] if not boundary else [
            ('unfunded',{'securities':{**base['securities'],'maker_cash_minor':0}})]
        measure = _security_outcomes
    else:
        variants = [(f'P{p}I{i}A{a}', {'demand':{**base['demand'],'peer_uplift_units':p,'hiring_adapts':bool(a)},
                      'fiscal':{**base['fiscal'],'transfer_minor':base['fiscal']['transfer_minor'] if i else 0}})
                    for p,i,a in ((0,0,1),(1,0,1),(0,1,1),(1,1,1),(0,1,0),(1,1,0))]
        measure = _consumption_outcomes
    for arm, update in variants:
        world, row = await run_world({**base, **update},root/arm,checkpoint_tick=1)
        if world is not None:
            try:
                row.update(measure(world))
            except Exception as exc:
                row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        rows[arm] = row
    completed = len([r for r in rows.values() if r['status']=='completed'])
    gate = completed==len(rows)
    outcomes = {}
    if gate and scene=='A':
        gate = all(r['state_and_exposure_valid'] for r in rows.values())
        if boundary:
            unfunded = rows['unfunded']
            gate &= (unfunded['drawdown'] is None
                     and all(not row['fill_count'] and not row['volume_units'] and row['close_minor'] is None
                             for row in unfunded['history'])
                     and unfunded['stock_and_cash_rejections'] == {
                         'insufficient_security_cash': config.firms * config.ticks})
        else:
            y = [rows[a]['drawdown'] for a in ('S1H1','S1H0','S0H1','S0H0')]
            outcomes['A1'] = _difference(*y)
    if gate and scene=='C':
        gate = (len({r['population_hash'] for r in rows.values()})==1
                and all(not row['positive_peer_decisions'] for arm,row in rows.items() if arm.startswith('P0'))
                and all(not rows[key]['contract_work_events'] for key in ('P0I1A0','P1I1A0'))
                and rows['P0I1A1']['transfers_paid_minor']==rows['P1I1A1']['transfers_paid_minor'])
        for cohort in ('liquid','tight'):
            y = [rows[a]['consumed_units'][cohort] for a in ('P1I1A1','P1I0A1','P0I1A1','P0I0A1')]
            denominator = rows['P0I1A1']['transfers_paid_minor'][cohort]
            effect = _difference(*y) * config.initial_price_minor / denominator if denominator else None
            outcomes[f'C1_{cohort}'] = effect
        outcomes['C2_employment'] = _difference(*(rows[a]['employment_rate']
                                          for a in ('P1I1A1','P1I1A0','P0I1A1','P0I1A0')))
    return {'rows':rows, 'gate':gate, 'assigned_worlds':len(rows), 'completed_worlds':completed, 'outcomes':outcomes}


async def _replay_d_world(world):
    try:
        replay = await economic_run.replay_run(world,destination=world.parent/(world.name+'-replay'))
        return {'replay':replay, 'status':'completed' if replay['status']=='matched' else 'failed'}
    except Exception as exc:
        return {'status':'failed', 'error':f'{type(exc).__name__}: {exc}'}


def _replay_d_process(world):
    return asyncio.run(_replay_d_world(world))


async def _validate_d(config, seeds, root, workers):
    report = await run_pilot(root,LaborPilotSpec(schema_version=14,base_config=config,seeds=seeds),workers=workers)
    worlds = [root/r['relative_path'] for r in report['runs'] if r['status']=='completed']
    if workers==1:
        replays = {path:await _replay_d_world(path) for path in worlds}
    else:
        with ProcessPoolExecutor(max_workers=workers,mp_context=get_context('spawn')) as pool:
            replays = dict(zip(worlds,pool.map(_replay_d_process,worlds),strict=True))
    return _d_case(report, {str(path.relative_to(root)): result for path, result in replays.items()})


def _d_case(report, replays):
    rows = {}
    for r in report['runs']:
        row = {k:r[k] for k in ('seed','arm','status','stopping','config_hash','relative_path')}
        if r['status']=='completed':
            row.update(replays[r['relative_path']], cooperation=r['cooperation'], partner_selection=r['partner_selection'])
        rows[f"{r['seed']}:{r['arm']}"] = row
    ready = sum(r['status']=='completed' for r in rows.values())
    study = report['d1']
    return {'assigned_worlds':len(rows),'completed_worlds':ready,'rows':rows,'d1':study,
            'gate':(ready==len(rows) and report['status']=='completed' and bool(study['eligible']['roots'])
                    and bool(study['pre_offer_stops']))}


async def _fiscal_feedback(config, root, *, run_world=_run):
    base = config.model_dump(mode='json')
    dose = len(config.fiscal.transfer_recipients)*config.fiscal.transfer_minor
    rows = {}
    for name, floor in (('fixed', None), ('published_floor', 2*config.households), ('floor_met', 1)):
        settings = {**base['fiscal'], 'consumption_floor_units': floor}
        world, row = await run_world({**base, 'fiscal':settings},root/name,checkpoint_tick=1)
        rows[name] = row
        if world is None:
            continue
        macro = {r['tick']:r['consumed_units'] for r in world.snapshot.data['closed_economy']['macro_history']}
        journal = world.snapshot.data['economic_runtime']['journal']
        consumption = {e['tx_id']: e for e in journal if e['kind']=='consumption'}
        fiscal = [e for e in journal if e['kind']=='fiscal' and e['details']['effective']]
        response = []
        for entry in fiscal:
            details = entry['details']
            rule = details.get('consumption_response')
            previous = entry['tick']-1
            expected = dose if floor is None or macro[previous]<floor else 0
            if rule is not None:
                refs = [e['tx_id'] for e in consumption.values() if e['tick']==previous]
                if (rule['published_tick']!=previous or rule['consumed_units']!=macro[previous]
                        or rule['source_tx_ids']!=refs or rule['triggered']!=(macro[previous]<floor)):
                    row.update(status='failed',error='Fiscal response disagrees with prior published consumption')
            if ((rule is None)!=(floor is None) or details['requested_transfer_minor']!=expected
                    or not 0<=details['transfers_paid_minor']<=expected):
                row.update(status='failed',error='Fiscal request or payment is outside its rule')
            response.append({'tick':entry['tick'],'tx_id':entry['tx_id'],'rule':rule,
                             'requested_minor':details['requested_transfer_minor'],
                             'paid_minor':details['transfers_paid_minor']})
        row['responses'] = response
    completed = sum(v['status']=='completed' for v in rows.values())
    responses = [r for k in ('published_floor','floor_met') for r in rows[k].get('responses',[]) if r['rule']]
    gate = completed==3 and all(len(row['responses'])==config.ticks-config.fiscal.effective_tick+1
                               for row in rows.values())
    return {'assigned_worlds':3,'completed_worlds':completed,'rows':rows,'gate':gate,
            'triggered_payments':sum(r['rule']['triggered'] and r['paid_minor']>0 for r in responses),
            'floor_satisfied_periods':sum(not r['rule']['triggered'] for r in responses)}


async def _evaluate_case(scene, config, root, boundary, *, run_world=_run):
    if scene in ('A','C'):
        return await _factorial(scene,config,root,boundary,run_world=run_world)
    if scene=='B':
        report = await compare_bank_channels(config,root,checkpoint_tick=1,run_world=run_world)
        pairs = [r for arm,r in report['rows'].items() if arm.startswith('F0')]
        report['gate'] = (all(r.get('financial_realized') in ('attenuated','uncovered')
                              and r['actual_interbank_due_gap_minor']>0 for r in pairs)
                          if boundary else report['manipulation_passed'])
        if report['gate'] and not boundary:
            y = [report['rows'][arm]['deposit_loss_rate'] for arm in ('F1S1','F1S0','F0S1','F0S0')]
            report['outcomes'] = {'B1':_difference(*y)}
        return report
    report = await compare_fiscal_policies(config,root,checkpoint_tick=1,run_world=run_world)
    report['gate'] = (not report['fixed_dose']['valid'] and all(r.get('actual_program_cost_minor')==0
                       and r['fiscal']['reached'] == []
                       for r in report['rows'].values()) if boundary else
                       report['fixed_dose']['valid'] and report['same_gross_budget']['valid'])
    if not boundary:
        feedback = await _fiscal_feedback(config,root/'feedback',run_world=run_world)
        report['fiscal_feedback'] = feedback
        report['gate'] &= feedback['gate']
        report['assigned_worlds'] += feedback['assigned_worlds']
        report['completed_worlds'] += feedback['completed_worlds']
    return report


def _run_job(args):
    scene, raw, seeds, path, boundary, d_workers = args
    registry.record(path.parent,{'job':path.name,'status':'running'})
    try:
        config = ClosedEconomyConfig.model_validate(raw)
        if scene == 'D':
            result = asyncio.run(_validate_d(config,seeds,path,d_workers))
        else:
            path.mkdir(parents=True)
            result = asyncio.run(_evaluate_case(scene,config,path,boundary))
    except Exception as exc:
        result = {'status':'failed','error':f'{type(exc).__name__}: {exc}',
                  **_empty_case(scene,boundary,seeds)}
    path.mkdir(parents=True, exist_ok=True)
    receipt = path/'case.json'
    atomic_json(receipt,result)
    return receipt


def _empty_case(scene, boundary, seeds):
    assigned = {'A':1 if boundary else 4,'B':4,'C':6,'D':5*len(seeds),'E':5 if boundary else 8}[scene]
    return {'assigned_worlds':assigned,'completed_worlds':0,'gate':False,'rows':{}}


def inference(rows):
    from scipy.stats import ttest_1samp
    from statsmodels.stats.multitest import multipletests

    result = {}
    for name in CONTRASTS:
        scene = name[0]
        source = [r for r in rows if r['scenario']==scene and not r['boundary']]
        if not source:
            continue
        blocks = [{'seed':r['seed'],'value':r.get('outcomes',{}).get(name) if r['gate'] else None} for r in source]
        values = [b['value'] for b in blocks if b['value'] is not None]
        out = {'assigned_roots':len(blocks),'observed_roots':len(values),'blocks':blocks,
               'mean':fmean(values) if values else None, 'mcse':stdev(values)/len(values)**.5 if len(values)>1 else None,
               'p_raw':None,'ci95':None}
        if len(values)==len(blocks) and len(values)>1 and max(values)!=min(values):
            test = ttest_1samp(values,0,alternative='greater')
            ci = ttest_1samp(values,0).confidence_interval()
            out.update(p_raw=float(test.pvalue),ci95=[float(ci.low),float(ci.high)])
        result[name] = out
    if result:
        values = [result.get(name,{}).get('p_raw') for name in CONTRASTS]
        p = multipletests([value if value is not None else 1. for value in values],method='holm')[1]
        for name, value in result.items():
            value['p_holm'] = float(p[CONTRASTS.index(name)]) if value['p_raw'] is not None else None
    return result


def _suite_configurations(spec, scenarios):
    for scene in scenarios:
        group = spec.scenarios[scene]
        for seeds in ([list(group.seeds)] if scene=='D' else [[seed] for seed in group.seeds]):
            config = group.config.model_copy(update={'root_seed':seeds[0]})
            yield {'scenario':scene,'seed':seeds[0],'seeds':seeds,'boundary':False,
                   'path':f'{scene}-{seeds[0]}'}, config.model_dump(mode='json')
        if scene in ('A','B','E'):
            config = group.config.model_dump(mode='json')
            if scene=='B':
                config['banking']['guarantee_budget_minor'] = 3
            if scene=='E':
                config['fiscal'].update(initial_cash_minor=0,brackets=[{'start_minor':0,'rate_bps':0}])
            yield {'scenario':scene,'seed':config['root_seed'],'boundary':True,
                   'path':f'{scene}-boundary'}, config


def _suite_report(rows, scenarios, plan_hash, *, analysis_version=ANALYSIS_VERSION):
    demand = [r for r in rows if r['scenario']=='C' and r['gate']]
    peer_exposure = [[r['rows'][arm]['peer_exposure_decisions'] for r in demand]
                     for arm in ('P0I1A1','P1I1A1')]
    covered = all if analysis_version==ANALYSIS_VERSION else any
    c_gates = {
        'peer_exposure_in_both_arms': bool(demand) and all(covered(counts) for counts in peer_exposure),
        'positive_peer_response': any(r['rows']['P1I1A1']['positive_peer_decisions'] for r in demand),
        'actual_job_delivery': any(r['rows']['P1I1A1']['contract_work_events'] for r in demand),
    }
    tight = [r['outcomes']['C1_tight'] for r in demand if r['outcomes']['C1_tight'] is not None]
    gates_passed = ('C' not in scenarios or all(c_gates.values()))
    fiscal_gates = {}
    if 'E' in scenarios and analysis_version!='wp10.five-scene.v1':
        fiscal = [r['fiscal_feedback'] for r in rows if r['scenario']=='E' and r['gate'] and not r['boundary']]
        fiscal_gates = {name:sum(r[name] for r in fiscal) for name in ('triggered_payments','floor_satisfied_periods')}
        gates_passed &= all(fiscal_gates.values())
    report = {'status':'completed' if gates_passed and all(
                  r['gate'] and r['completed_worlds']==r['assigned_worlds'] for r in rows) else 'failed',
        'plan_hash':plan_hash,'external_api_calls':0,'scenario_ids':list(scenarios),'cases':rows,
        'inference':inference(rows),'assigned_worlds':sum(r['assigned_worlds'] for r in rows),
        'completed_worlds':sum(r['completed_worlds'] for r in rows)}
    if 'C' in scenarios:
        report['demand_manipulation_gates'] = c_gates
        report['tight_budget_c1_descriptive'] = {'observed_roots':len(tight),'mean':fmean(tight) if tight else None}
    if fiscal_gates:
        report['fiscal_feedback_gates'] = fiscal_gates
    return report


def run_suite(raw, destination, *, scenarios=tuple(CATALOG), workers=1, resume=False):
    spec = ScenarioSuite.model_validate(raw)
    require_unique(scenarios,'scenario')
    if not scenarios or not set(scenarios)<=set(CATALOG) or type(workers) is not int or not 1<=workers<=8:
        raise ValueError('Choose A–E and between 1 and 8 workers')
    root = Path(destination).resolve()
    d_workers = max(1,workers//2) if 'D' in scenarios else 1
    planned = list(_suite_configurations(spec, scenarios))
    manifest = [record for record, _ in planned]
    jobs = [(r['scenario'],raw,r.get('seeds',[r['seed']]),root/r['path'],r['boundary'],
             d_workers if r['scenario']=='D' else 1) for r,raw in planned]
    plan = {'version':ANALYSIS_VERSION,'spec':spec.model_dump(mode='json'),'assignments':manifest,
            'implementation':economic_run.current_implementation(),'catalog':{s:CATALOG[s] for s in scenarios},
            'research_dependencies':{name:version(name) for name in ('scipy','statsmodels')},
            'contrast_family':list(CONTRASTS),'analysis':'world-root one-sided paired contrasts, Holm; retain incomplete roots',
            'external_api_calls':0}
    if not resume:
        root.mkdir(parents=True)
    with registry.run_lock(root):
        if resume:
            sealed = read_json(root/'plan.json')
            frozen = sealed['plan']
            plan_hash = content_hash(frozen)
            identity = ('source_hash','dependency_hash')
            if (sealed['plan_hash']!=plan_hash or
                    any(frozen.get(k)!=v for k,v in plan.items() if k!='implementation') or
                    any(frozen['implementation'][k]!=plan['implementation'][k] for k in identity)):
                raise ValueError('Resume requires the original suite plan, source and dependencies')
        else:
            plan_hash = content_hash(plan)
            atomic_json(root/'plan.json',{'plan':plan,'plan_hash':plan_hash})
        final = root/'report.json'
        if final.exists():
            report = read_json(final)
            if report['plan_hash']!=plan_hash:
                raise ValueError('Existing suite report does not match the frozen plan')
            return report
        attempted = {r['job'] for r in registry.history(root)}
        pending, cases = [], {}
        for job in jobs:
            scene, _, seeds, path, boundary, _ = job
            receipt = path/'case.json'
            if path.is_symlink() or receipt.is_symlink():
                raise ValueError('Suite case paths cannot be symbolic links')
            if receipt.exists():
                cases[path.name] = read_json(receipt)
            elif path.exists() or path.name in attempted:
                cases[path.name] = {'status':'interrupted',**_empty_case(scene,boundary,seeds)}
            else:
                pending.append(job)
        if workers==1:
            measured = list(map(_run_job,pending))
        else:
            with ProcessPoolExecutor(max_workers=workers-d_workers+1,mp_context=get_context('spawn')) as worker_pool:
                measured = list(worker_pool.map(_run_job,pending))
        cases.update({receipt.parent.name:read_json(receipt) for receipt in measured})
        rows = [record | cases[record['path']] for record in manifest]
        report = _suite_report(rows,scenarios,plan_hash)
        atomic_json(final,report)
    return report
