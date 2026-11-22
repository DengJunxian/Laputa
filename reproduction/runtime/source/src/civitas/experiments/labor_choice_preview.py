"""One registered applicant's choice and wages on the existing vacancy."""
from civitas.application import economic_run
from civitas.application.inspection import _cursor
from civitas.contracts.base import content_hash
from civitas.contracts.economic import LaborApplyIntent, LaborContract, LaborVacancyClosure
from civitas.experiments.choice_preview import (audit_choice_pair, choice_attempts, freeze_choice_pair,
                                                 open_choice_arm, run_choice_pair, start_registered_choice)
from civitas.experiments.closed_economy import audit_snapshot
from civitas.experiments.registry import history
from civitas.scenarios.closed_economy_cognition import opportunity_observation

DESIGN = 'directed-labor.vacancy-choice-preview.v1'
PREVIEW_SEAT = 3


def _plan(raw, seat, tape, implementation):
    config = economic_run.parse_config(raw)
    transfer = config.labor_transfer
    if (config.cognition_mode != 'rule' or config.population_input is not None or config.fault_tick is not None
            or config.stopping_hazard_bps is not None or config.demand is not None
            or transfer is None or not transfer.additional_candidate_indices or transfer.shared_payroll):
        raise ValueError('Choose a fixed, synthetic, single competitive vacancy with archived observations')
    rule = config.model_dump(mode='json')
    archive = economic_run.parse_config({**rule, 'labor_cognition': {'household_index': seat}}).model_dump(mode='json')
    if tape is not None:
        tape.validate_choice_slot('cd_labor', f'household:{seat:04d}', transfer.offer_tick)
    return {'design':DESIGN, 'endpoint':'paid_wages_minor_on_the_registered_new_contract',
            'root_seed':config.root_seed, 'offer_tick':transfer.offer_tick, 'final_tick':config.ticks,
            'archive_hash':content_hash(tape) if tape is not None else None,
            'implementation':{key:implementation[key] for key in ('source_hash','dependency_hash')},
            'arms':{'rule':rule,'archive':archive}}


async def run_labor_choice(raw, seat, destination, response_archive=None):
    return await run_choice_pair(destination, response_archive,
        lambda tape, identity: _plan(raw, seat, tape, identity), _recompute,
        cognition_field='labor_cognition', tick_field='offer_tick')


def labor_template_plan(raw):
    return _plan(raw, PREVIEW_SEAT, None, economic_run.current_implementation())


async def freeze_labor_preview(raw, destination):
    plan, _ = await freeze_choice_pair(destination, None, lambda tape, identity: _plan(raw, PREVIEW_SEAT, tape, identity),
                                       cognition_field='labor_cognition', tick_field='offer_tick')
    return plan


def _reload_plan(plan, tape, identity):
    seat = plan['arms']['archive']['labor_cognition']['household_index']
    return _plan(plan['arms']['rule'], seat, tape, identity)


async def start_labor_preview(root, plan_hash, *, progress):
    return await start_registered_choice(root, plan_hash, _reload_plan, _recompute, progress=progress)


def audit_labor_choice(root):
    return audit_choice_pair(root, _reload_plan, _recompute)


def _terms(config):
    transfer = config.labor_transfer
    employer = f'firm:{transfer.employer_index:02d}'
    tick = transfer.offer_tick
    return {'kind':'labor_vacancy','employer_id':employer,'offered_tick':tick,
            'start_tick':tick+1,'end_tick_exclusive':tick+1+transfer.duration_ticks,
            'wage_minor':config.wage_minor,'work_minutes':480}


def _read_offer(store, marker, state, config, actor, candidates, terms):
    tick = marker.tick
    offer_id = f'labor-vacancy:{tick}:{terms["employer_id"]}'
    scenario = state.data['closed_economy']
    closure = LaborVacancyClosure.model_validate(scenario['vacancy_closure'])
    prior = scenario['macro_history'][tick-2]
    opened = prior['sold_units_by_seller'][terms['employer_id']] >= config.labor_transfer.min_previous_sales_units
    if (closure.vacancy_id, closure.tick, closure.status != 'not_opened', prior['tick']) != (
            offer_id, tick, opened, tick-1):
        raise ValueError('Vacancy closure is inconsistent with the settled sales gate')
    observations = store.observation_rows(marker)
    delivered = {}
    for obs in observations:
        for fact in obs.market_view:
            if fact.value.get('kind') == 'labor_vacancy':
                if (fact.fact_id != offer_id or fact.owner_id != obs.actor_id or fact.public
                        or fact.visible_at.tick != tick or fact.value != terms or obs.actor_id in delivered):
                    raise ValueError('Vacancy was not delivered on its registered private terms')
                delivered[obs.actor_id] = (obs, fact)
    if set(delivered) != (candidates if opened else set()):
        raise ValueError('Vacancy recipients do not match its opened candidate pool')
    decision, = (d for d in state.decisions if d.tick == tick and d.actor_id == actor)
    intent = next((i for i in decision.economic_intents if isinstance(i, LaborApplyIntent)), None)
    observation, fact = delivered.get(actor, (None,None))
    receipt = None
    if intent is not None:
        if (fact is None or decision.observation_id != observation.observation_id or intent.vacancy_id != offer_id
                or fact.fact_id not in decision.evidence_ids or actor not in closure.applicant_ids):
            raise ValueError('Application does not bind the authorized eligible opportunity')
        receipt, = (r for r in state.receipts if r.command_id == f'command:{intent.intent_id}')
        if ((receipt.actor_id, receipt.tick, receipt.decision_id) != (actor,tick,decision.decision_id)
                or receipt.executed_quantity or receipt.cash_delta_minor
                or receipt.status not in ('accepted','rejected')
                or (receipt.status=='accepted' and (receipt.outcome!='labor_offer_accepted'
                                                     or receipt.counterparty_id != terms['employer_id']))):
            raise ValueError('Application receipt does not bind the registered no-cash decision')
    elif actor in closure.applicant_ids:
        raise ValueError('Vacancy applicants include an unsubmitted choice')
    return observation, fact, decision, receipt, closure


def _wages(state, actor, terms, receipt):
    scenario = state.data['closed_economy']
    raw = scenario['labor_contract']
    contract = LaborContract.model_validate(raw) if raw is not None else None
    seat_contract = contract if contract and contract.offer.worker_id == actor else None
    closure = scenario['vacancy_closure']
    if bool(contract) != bool(closure and closure['status'] == 'filled'):
        raise ValueError('Vacancy closure and contract award disagree')
    awarded = receipt is not None and receipt.status == 'accepted'
    if awarded != bool(seat_contract) or (awarded and receipt.command_id != seat_contract.contract_id):
        raise ValueError('Recorded outcome disagrees with the committed contract')
    ledger = {entry['tx_id']:entry for entry in state.data['economic_runtime']['journal']}
    periods = []
    if seat_contract is not None:
        offer = seat_contract.offer
        if ({'kind':'labor_vacancy', **offer.model_dump(exclude={'offer_id','worker_id'})} != terms
                or seat_contract.early_end_tick is not None):
            raise ValueError('Contract does not preserve the authorized vacancy terms')
        for event in scenario['labor_events']:
            if event.get('contract_id') != seat_contract.contract_id:
                continue
            tick = event['tick']
            accrual, payment = ledger[event['accrual_tx_id']], ledger[event['payment_tx_id']]
            earned, paid = event['accrued_minor'], event['paid_minor']
            expected = {'worker_id': actor, 'employer_id':offer.employer_id,'amount_minor':earned}
            body = {key:payment['payload'][key] for key in expected}
            cash = {(p['entity_id'],p['side']):p['amount_minor'] for p in payment['postings']
                    if p['account_code']=='cash'}
            if (seat_contract.activation_failed or not offer.start_tick <= tick < offer.end_tick_exclusive
                    or (event['worker_id'],event['employer_id'],event['delivered_minutes'],earned) != (
                        actor,offer.employer_id,offer.work_minutes,offer.wage_minor)
                    or (accrual['kind'],payment['kind'],accrual['tick'],payment['tick']) != ('wage_accrual','wage_payment',tick,tick)
                    or accrual['payload'] != expected or body != expected or accrual['details']['accrued_minor'] != earned
                    or payment['details']['paid_minor'] != paid or payment['details']['outstanding_minor'] != event['outstanding_minor']
                    or cash != ({(actor,'debit'):paid,(offer.employer_id,'credit'):paid} if paid else {})):
                raise ValueError('Work, payable or cash entry differs from the signed labor contract')
            periods.append({'tick':tick,'minutes':event['delivered_minutes'],'earned_minor':earned,'paid_minor':paid,
                            'payment_hash':content_hash(payment)})
        expected_ticks = ([] if seat_contract.activation_failed else
                          list(range(offer.start_tick, min(offer.end_tick_exclusive,state.tick+1))))
        if [row['tick'] for row in periods] != expected_ticks:
            raise ValueError('Completed work does not cover the effective contract window')
    total = 0
    for entry in ledger.values():
        if entry['kind']=='wage_payment' and entry['tick'] >= terms['start_tick'] and entry['payload']['worker_id']==actor:
            total += sum(p['amount_minor'] for p in entry['postings']
                         if (p['entity_id'],p['account_code'],p['side']) == (actor,'cash','debit'))
    return {'hired':int(awarded),'activation_failed':int(bool(seat_contract and seat_contract.activation_failed)),
            'filled_vacancy':int(bool(contract)), 'worked_periods':len(periods),
            'minutes':sum(row['minutes'] for row in periods),'earned_minor':sum(row['earned_minor'] for row in periods),
            'paid_contract_wages_minor':sum(row['paid_minor'] for row in periods),
            'total_seat_wages_after_offer_minor':total,'work_and_wages':periods}


def _arm(root, plan, tape, side):
    store = open_choice_arm(root,plan,side)
    config = economic_run.parse_config(plan['arms'][side])
    transfer = config.labor_transfer
    seat = plan['arms']['archive']['labor_cognition']['household_index']
    actor = f'household:{seat:04d}'
    candidates = {f'household:{i:04d}' for i in [transfer.worker_index,*transfer.additional_candidate_indices]}
    terms = _terms(config)
    cursors, prefix, model = [],[],[]
    observation = fact = decision = receipt = closure = None
    for marker,state in store.states():
        cursors.append(_cursor(store,marker))
        if marker.tick < transfer.offer_tick:
            prefix.append((marker.state_hash,content_hash(list(store.observation_rows(marker)))))
        if marker.tick == transfer.offer_tick:
            observation,fact,decision,receipt,closure = _read_offer(store,marker,state,config,actor,candidates,terms)
            model = store.model_records(marker)
        elif any(r not in model for r in store.model_records(marker)):
            raise ValueError('Model invocation outside the registered labor opportunity')
    if cursors[0]['tick'] != 0 or marker.tick > config.ticks:
        raise ValueError('Labor history does not cover its assigned initial state and horizon')
    audited = audit_snapshot(state)
    if audited['status'] != 'passed':
        raise ValueError('Economic state did not pass independent audit')
    projected = opportunity_observation(observation,fact) if fact is not None else None
    attempts = choice_attempts(store,tape,projected) if side=='archive' else []
    trace = decision.belief_patch.get('model_labor') if decision else None
    fallback = bool(trace and trace['status']=='rule_fallback')
    if trace:
        row, = model
        action = 'apply' if receipt else 'wait'
        if (row not in attempts or trace['attempt_id'] != row['attempt_id']
                or row['state'] != ('failed' if fallback else 'success')
                or (not fallback and trace['action'] != action)):
            raise ValueError('Labor choice does not match its frozen invocation and receipt')
    elif model or (side=='archive' and fact is not None) or (decision is not None and attempts):
        raise ValueError('Unexpected or missing invocation for the committed labor opportunity')
    attempted = int(receipt is not None)
    return {'commits':cursors,'eligible':int(actor in candidates),'reached':int(fact is not None),
            'observation_hash':observation.payload_hash if observation else None,
            'closure_status':closure.status if closure else None,'valid_applicants':len(closure.applicant_ids) if closure else 0,
            'attempted_applications':attempted,'normal_waits':int(fact is not None and not attempted and not fallback),
            'rejected_applications':int(bool(receipt and receipt.status=='rejected')),
            'rejection_codes':list(receipt.reason_codes) if receipt else [],
            'model_attempts':len(attempts),'successful_model_attempts':sum(r['state']=='success' for r in attempts),
            'failed_model_attempts':sum(r['state']=='failed' for r in attempts),'rule_fallbacks':int(fallback),
            'failed_execution_attempts':sum(r['status'] in ('failed','projection_failed') for r in history(store.root)),
            'request_hashes':[content_hash(r['request']) for r in attempts],
            **_wages(state,actor,terms,receipt)}, prefix


def _recompute(root,plan,tape):
    rule, rp = _arm(root,plan,tape,'rule')
    archive, ap = _arm(root,plan,tape,'archive')
    if any(r != a for r,a in zip(rp,ap)):
        raise ValueError('Pre-offer state or observations differ')
    reached = sum(a['reached'] for a in (rule,archive))
    paired = reached == 2
    if paired and rule['observation_hash'] != archive['observation_hash']:
        raise ValueError('Arms did not receive the same authorized vacancy')
    complete = sum(a['commits'][-1]['tick'] == plan['final_tick'] for a in (rule,archive))
    totals = {key:sum(a[key] for a in (rule,archive)) for key in (
        'attempted_applications','normal_waits','rejected_applications','hired','model_attempts',
        'successful_model_attempts','failed_model_attempts','rule_fallbacks','failed_execution_attempts')}
    terms = _terms(economic_run.parse_config(plan['arms']['rule']))
    return {'status':'completed' if paired and complete==2 else 'failed','design':DESIGN,'plan_hash':content_hash(plan),
            'root_seed':plan['root_seed'],'endpoint':plan['endpoint'],'archive_hash':plan['archive_hash'],
            'response_origin':'fixed_synthetic_archive','external_api_calls':0,'terms':{
                k:terms[k] for k in ('offered_tick','start_tick','end_tick_exclusive','work_minutes','wage_minor')},
            'denominators':{'assigned_arms':2,'completed_arms':complete,'planned_seats':2,'reached_seats':reached,
                            'ineligible_seats':sum(not a['eligible'] for a in (rule,archive)),
                            'eligible_unreached_seats':sum(a['eligible'] and not a['reached'] and a['closure_status'] is not None
                                                          for a in (rule,archive)),
                            'unobserved_at_offer':sum(a['closure_status'] is None for a in (rule,archive)),
                            'paired_opportunities':int(paired),'natural_stops':0,**totals},
            'difference':{key:archive[key]-rule[key] for key in (
                'attempted_applications','hired','worked_periods','minutes','paid_contract_wages_minor',
                'total_seat_wages_after_offer_minor')} if paired and complete==2 else None,
            'arms':{'rule':rule,'archive':archive}}
