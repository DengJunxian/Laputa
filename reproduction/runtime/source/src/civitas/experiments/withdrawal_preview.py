"""A frozen single-seat comparison over the existing verified-notice queue."""
from collections import Counter

from civitas.application import economic_run
from civitas.application.inspection import _cursor
from civitas.contracts.base import content_hash
from civitas.contracts.economic import WithdrawDepositIntent
from civitas.experiments.choice_preview import (audit_choice_pair, choice_attempts, freeze_choice_pair,
                                                 open_choice_arm, run_choice_pair, start_registered_choice)
from civitas.experiments.institutions import audit_institutions
from civitas.experiments.registry import history
from civitas.scenarios.closed_economy_cognition import opportunity_observation

DESIGN = 'verified-bank-notice.withdrawal-preview.v1'


def _plan(raw, tape, implementation):
    config = economic_run.parse_config(raw)
    notice = config.banking.run_notice if config.banking else None
    if (config.cognition_mode != 'rule' or config.stopping_hazard_bps is not None
            or config.population_input is not None or config.fault_tick is not None
            or notice is None or len(notice.withdrawal_by_recipient) != 1):
        raise ValueError('Choose a synthetic fixed-horizon rule world with exactly one registered recipient')
    actor, = notice.withdrawal_by_recipient
    rule = config.model_dump(mode='json')
    archive = economic_run.parse_config({**rule, 'withdrawal_cognition': {
        'household_index': int(actor.rsplit(':',1)[1]), 'source': 'archived_response'}})
    if tape is not None:
        tape.validate_choice_slot('cd_withdrawal', actor, notice.tick)
    return {'design': DESIGN, 'endpoint': 'settled_cash_minor_for_registered_notice_by_final_tick',
            'root_seed': config.root_seed, 'notice_tick': notice.tick, 'final_tick': config.ticks,
            'archive_hash': content_hash(tape) if tape is not None else None,
            'implementation': {key: implementation[key] for key in ('source_hash', 'dependency_hash')},
            'arms': {'rule': rule, 'archive': archive.model_dump(mode='json')}}


async def run_withdrawal_preview(raw, destination, response_archive=None):
    return await run_choice_pair(destination, response_archive, lambda tape, identity: _plan(raw, tape, identity),
                                 _recompute, cognition_field='withdrawal_cognition', tick_field='notice_tick')


def withdrawal_template_plan(raw):
    return _plan(raw, None, economic_run.current_implementation())


async def freeze_withdrawal_preview(raw, destination):
    plan, _ = await freeze_choice_pair(destination, None, lambda tape, identity: _plan(raw, tape, identity),
                                       cognition_field='withdrawal_cognition', tick_field='notice_tick')
    return plan


def _reload_plan(plan, tape, identity):
    return _plan(plan['arms']['rule'], tape, identity)


async def start_withdrawal_preview(root, plan_hash, *, progress):
    return await start_registered_choice(root, plan_hash, _reload_plan, _recompute, progress=progress)


def _settlement(book, actor, bank, intent, receipt):
    request_id = f'notice:{actor}'
    queue = book['bank_positions']['withdrawals'].get(request_id)
    ledger = {entry['tx_id']: entry for entry in book['journal']}
    if receipt is not None and receipt.status != 'rejected':
        queue_entry, = (ledger[tx] for tx in receipt.ledger_tx_ids)
        if (queue_entry['kind'] != 'bank_withdrawal' or queue_entry['tick'] != receipt.tick
                or queue_entry['payload'] != {'actor_id': actor, 'intent': intent.model_dump(mode='json')}
                or queue_entry['details']['request_id'] != request_id or queue_entry['postings']
                or receipt.outcome != 'withdrawal_queued' or receipt.executed_quantity != 0
                or receipt.cash_delta_minor != 0 or receipt.accepted_quantity != intent.amount_minor or queue is None):
            raise ValueError('Admission must create a reservation without a cash payment')
    elif queue is not None or (receipt is not None and (
            receipt.accepted_quantity or receipt.executed_quantity or receipt.cash_delta_minor or receipt.ledger_tx_ids)):
        raise ValueError('Rejected or absent instructions cannot settle')
    timeline = []
    for entry in book['journal']:
        if entry['kind'] != 'bank' or entry['payload'] != {'action':'queue', 'reference':request_id}:
            continue
        details = entry['details']
        paid = details.get('paid_minor',0)
        legs = {(actor,'cash','debit'), (actor,'deposit_asset','credit'),
                (bank,'bank_reserve','credit'), (bank,'deposit_payable','debit')}
        observed = Counter()
        for row in entry['postings']:
            observed[row['entity_id'], row['account_code'], row['side']] += row['amount_minor']
        if (queue is None or receipt.tick >= entry['tick'] or any(observed[leg] != paid for leg in legs)):
            raise ValueError('Queue payment differs from the reserve, cash or deposit postings')
        timeline.append({'tick':entry['tick'], 'status':details['status'], 'paid_minor':paid,
                         'outstanding_minor':details['outstanding_minor'], 'ledger_hash':content_hash(entry)})
    paid = sum(row['paid_minor'] for row in timeline)
    admitted = receipt.accepted_quantity if receipt else 0
    remaining = queue['remaining_minor'] if queue else 0
    if paid + remaining != admitted or (queue is not None and queue['paid_minor'] != paid):
        raise ValueError('Admitted principal differs from settlement and outstanding amounts')
    return {'queued_minor': admitted, 'paid_minor': paid, 'outstanding_minor': remaining,
            'queue_status': queue['status'] if queue else None, 'settlements': timeline}


def _arm(root, plan, tape, side):
    store = open_choice_arm(root, plan, side)
    config = economic_run.parse_config(plan['arms'][side])
    spec = config.banking.run_notice
    actor, = spec.withdrawal_by_recipient
    source, = (op for op in config.banking.operations if op.operation_id == spec.source_request)
    prefix, cursors = [], []
    prior_balances = None
    exposure = observation = decision = intent = receipt = None
    delivered = False
    committed_model = []
    for marker, state in store.states():
        if marker.tick:
            audit_institutions(state)
        cursors.append(_cursor(store, marker))
        rows = store.observation_rows(marker)
        if marker.tick < spec.tick:
            book = state.data['economic_runtime']
            prefix.append((content_hash(book),content_hash(list(rows))))
            prior_balances = {'bank_reserve_minor': book['balances'][source.bank].get('bank_reserve',0),
                              'bank_deposits_minor': -book['balances'][source.bank].get('deposit_payable',0)}
        model_rows = store.model_records(marker)
        if marker.tick != spec.tick:
            if any(row not in committed_model for row in model_rows):
                raise ValueError('Unexpected model invocation outside the registered withdrawal period')
            continue
        book = state.data['economic_runtime']
        journal = {entry['tx_id']: entry for entry in book['journal']}
        notice, = [entry for entry in journal.values() if entry['event_id'] == f'bank:{spec.tick}:notice:{spec.source_request}'] or [None]
        delivered = bool(notice and actor in notice['details']['reached'])
        verified = []
        for obs in rows:
            verified.extend((obs, fact) for fact in obs.market_view
                            if fact.value.get('kind') == 'verified_withdrawal_delay' and fact.visible_at.tick == marker.tick)
        if verified:
            (observation, exposure), = verified
            root_entry = journal[exposure.value['source_tx_id']]
            if (not delivered or observation.actor_id != actor or exposure.owner_id != actor
                    or exposure.value['bank'] != source.bank or exposure.value['source_tx_id'] != notice['details']['source_tx_id']
                    or root_entry['payload'] != {'action':'queue', 'reference':spec.source_request}
                    or root_entry['details']['status'] != 'queued' or not root_entry['details']['outstanding_minor']):
                raise ValueError('Verified notice is not tied to the same authorized delay and recipient')
        elif delivered and notice['details']['content'] == 'verified_withdrawal_delay':
            raise ValueError('A verified delivered notice is missing from the archived observation')
        decision, = (d for d in state.decisions if d.tick == marker.tick and d.actor_id == actor)
        intents = [i for i in decision.economic_intents if isinstance(i, WithdrawDepositIntent)]
        if intents:
            intent, = intents
            if (exposure is None or decision.observation_id != observation.observation_id
                    or intent.notice_fact_id != exposure.fact_id or intent.notice_fact_id not in decision.evidence_ids
                    or intent.bank_id != source.bank or intent.amount_minor > exposure.value['withdrawal_limit_minor']):
                raise ValueError('The withdrawal does not cite the authorized opportunity')
            receipt, = (r for r in state.receipts if r.command_id == 'command:' + intent.intent_id)
            if (receipt.actor_id, receipt.tick, receipt.decision_id) != (actor, marker.tick, decision.decision_id):
                raise ValueError('Withdrawal receipt does not identify its decision')
        committed_model = model_rows
    if cursors[0]['tick'] != 0 or marker.tick > config.ticks:
        raise ValueError('The withdrawal history does not match the assigned horizon')
    projected = opportunity_observation(observation, exposure) if exposure is not None else None
    attempts = choice_attempts(store, tape, projected) if side == 'archive' else []
    trace = decision.belief_patch.get('model_withdrawal') if decision is not None else None
    if committed_model:
        fact, = committed_model
        if fact not in attempts or not trace or trace['attempt_id'] != fact['attempt_id']:
            raise ValueError('Committed withdrawal does not bind its invocation')
    elif trace or (side == 'archive' and exposure is not None):
        raise ValueError('Committed archived withdrawal opportunity is missing its invocation')
    if side == 'archive' and decision is not None and exposure is None and attempts:
        raise ValueError('Unreached seat invoked the withdrawal policy')
    settlement = _settlement(state.data['economic_runtime'], actor, source.bank, intent, receipt)
    reasons = list(receipt.reason_codes) if receipt and receipt.status == 'rejected' else []
    failures = sum(entry['status'] in ('failed','projection_failed') for entry in history(store.root))
    return {'commits':cursors, 'exposed':int(exposure is not None), 'notice_delivered':int(delivered),
            'observation_hash':observation.payload_hash if observation else None,
            'before_notice':prior_balances, 'attempted_intents':int(intent is not None),
            'requested_minor':intent.amount_minor if intent else 0, 'rejected_intents':int(bool(reasons)),
            'insufficient_deposit':int('insufficient_unreserved_deposit' in reasons or 'insufficient_deposit' in reasons),
            'rejection_codes':reasons, 'model_attempts':len(attempts),
            'successful_model_attempts':sum(row['state']=='success' for row in attempts),
            'failed_model_attempts':sum(row['state']=='failed' for row in attempts),
            'rule_fallbacks':int(bool(trace and trace['status']=='rule_fallback')),
            'failed_execution_attempts':failures, 'request_hashes':[content_hash(row['request']) for row in attempts],
            **settlement}, prefix


def _recompute(root, plan, tape):
    rule, rule_prefix = _arm(root, plan, tape, 'rule')
    archive, archive_prefix = _arm(root, plan, tape, 'archive')
    if any(a != b for a,b in zip(rule_prefix, archive_prefix)):
        raise ValueError('Opening state or pre-notice observations differ between arms')
    reached = rule['exposed'] + archive['exposed']
    completed = sum(arm['commits'][-1]['tick'] == plan['final_tick'] for arm in (rule,archive))
    paired = reached == 2 and rule['observation_hash'] == archive['observation_hash']
    if reached == 2 and not paired:
        raise ValueError('Archived arm received a different authorized opportunity')
    arms = {'rule':rule, 'archive':archive}
    return {'status':'completed' if completed == 2 and paired else 'failed', 'design':DESIGN,
            'plan_hash':content_hash(plan), 'root_seed':plan['root_seed'],
            'endpoint':plan['endpoint'], 'notice_tick':plan['notice_tick'], 'final_tick':plan['final_tick'],
            'response_origin':'fixed_synthetic_archive', 'archive_hash':plan['archive_hash'], 'external_api_calls':0,
            'denominators':{'assigned_arms':2, 'completed_arms':completed, 'planned_seats':2, 'reached_seats':reached,
                            'delivered_notice_seats':sum(arm['notice_delivered'] for arm in arms.values()),
                            'admitted_intents':sum(arm['queued_minor'] > 0 for arm in arms.values()),
                            'seats_with_payment':sum(arm['paid_minor'] > 0 for arm in arms.values()),
                            'unreached_seats':sum(arm['commits'][-1]['tick'] >= plan['notice_tick'] and not arm['exposed']
                                                  for arm in arms.values()),
                            'unobserved_at_notice':sum(arm['commits'][-1]['tick'] < plan['notice_tick'] for arm in arms.values()),
                            'paired_opportunities':int(paired), 'natural_stops':0,
                            **{key:sum(arm[key] for arm in arms.values()) for key in (
                                'attempted_intents','rejected_intents','insufficient_deposit','model_attempts',
                                'successful_model_attempts','failed_model_attempts','rule_fallbacks','failed_execution_attempts')}},
            'difference':{key:archive[key]-rule[key] for key in (
                'requested_minor','queued_minor','paid_minor','outstanding_minor')} if paired and completed == 2 else None,
            'arms':arms}


def audit_withdrawal_preview(root):
    return audit_choice_pair(root, _reload_plan, _recompute)
