"""Registered, shareable summary of one offline supplier decision."""
from collections import Counter

from civitas.application import economic_run
from civitas.application.inspection import _cursor
from civitas.cognition.cache import semantic_key
from civitas.cognition.invocations import ModelInvocationStore
from civitas.cognition.supplier_archive import SupplierResponseArchive
from civitas.contracts.base import content_hash
from civitas.contracts.economic import PurchaseIntent
from civitas.contracts.observations import Observation
from civitas.experiments.registry import history
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from civitas.storage.history import publish_directory

ANALYSIS = 'joint-world.supplier-preview.v1'
ACTOR = 'household:0000'
PERIOD = 3
SETTINGS = {'mode': 'L', 'household_indices': [0], 'ticks': [PERIOD],
            'reflect': False, 'source': 'archived_response'}


def _plan(raw, tape, implementation):
    rule = economic_run.parse_config(raw)
    if (rule.cognition_mode != 'rule' or (rule.households, rule.firms, rule.ticks) != (32, 4, 5)
            or not rule.banking or not rule.fiscal or not rule.archive_observations
            or rule.population_input is not None or rule.fault_tick is not None
            or rule.stopping_hazard_bps is not None):
        raise ValueError('Use the synthetic fixed-horizon joint-world rule configuration')
    configs = {'rule': rule.model_dump(mode='json')}
    archive = economic_run.parse_config({**configs['rule'], 'supplier_cognition': SETTINGS})
    configs['archive'] = archive.model_dump(mode='json')
    tape.validate_slots(archive.supplier_cognition)
    if len(tape.records) != 1 or tape.records[0].request['task'] != 'cd_supplier':
        raise ValueError('Register exactly one synthetic supplier response')
    return {'design': ANALYSIS, 'period': PERIOD, 'root_seed': rule.root_seed,
            'archive_hash': content_hash(tape), 'configs': configs,
            'implementation': {key: implementation[key] for key in ('source_hash', 'dependency_hash')}}


def freeze_supplier_preview(config, archive, destination):
    tape = SupplierResponseArchive.model_validate(archive)
    plan = _plan(config, tape, economic_run.current_implementation())
    with publish_directory(destination) as staging:
        staging.chmod(0o700)
        atomic_json(staging/'plan.json', {'plan': plan, 'plan_hash': content_hash(plan)})
        atomic_json(staging/'response-archive.json', tape.model_dump(mode='json'))
        for side, raw in plan['configs'].items():
            atomic_json(staging/f'{side}.json', raw)
    return {'status': 'frozen', 'plan_hash': content_hash(plan)}


def load_supplier_preview(root):
    manifest = read_json(root/'plan.json')
    tape = SupplierResponseArchive.model_validate(read_json(root/'response-archive.json'))
    plan = _plan(manifest['plan']['configs']['rule'], tape, economic_run.current_implementation())
    if manifest != {'plan': plan, 'plan_hash': content_hash(plan)}:
        raise ValueError('Frozen preview, response archive or runtime changed')
    return plan, tape


def supplier_side(plan, config):
    for side, frozen in plan['configs'].items():
        if config == frozen:
            return side
    raise ValueError('Select one of the two registered supplier configurations')


def _purchase(state, observation):
    decision, = (d for d in state.decisions if d.tick == PERIOD and d.actor_id == ACTOR)
    quotes = {f.value['seller_id']: f for f in observation.market_view if f.value.get('kind') == 'goods_quote'}
    if (not quotes or decision.observation_id != observation.observation_id
            or not set(decision.evidence_ids) <= observation.evidence_ids):
        raise ValueError('The committed purchase does not identify the authorized offer')
    intents = [intent for intent in decision.economic_intents if isinstance(intent, PurchaseIntent)]
    if not intents:
        return {'visible_quotes': len(quotes), 'legal_choice': False, 'supplier': None, 'settled_units': 0,
                'buyer_debit_minor': 0, 'supplier_credit_minor': 0}, decision
    intent, = intents
    if intent.seller_id not in quotes or intent.quote_id != quotes[intent.seller_id].value['quote_id']:
        raise ValueError('The purchase references a supplier outside the authorized quotes')
    receipt, = (r for r in state.receipts if r.command_id == f'command:{intent.intent_id}')
    ledger = {entry['tx_id']: entry for entry in state.data['economic_runtime']['journal']}
    transactions = [ledger[tx] for tx in receipt.ledger_tx_ids]
    bought = spent = received = 0
    for entry in transactions:
        if (entry['tick'] != PERIOD or entry['kind'] != 'purchase'
                or entry['payload']['buyer_id'] != ACTOR or entry['payload']['seller_id'] != intent.seller_id
                or entry['payload']['quote_id'] != intent.quote_id):
            raise ValueError('The receipt references another economic transaction')
        bought += sum(m['quantity_units'] for m in entry['goods_movements']
                      if m['from_owner'] == intent.seller_id and m['to_owner'] == ACTOR)
        for posting in entry['postings']:
            if posting['account_code'] == 'cash':
                if posting['entity_id'] == ACTOR and posting['side'] == 'credit':
                    spent += posting['amount_minor']
                elif posting['entity_id'] == intent.seller_id and posting['side'] == 'debit':
                    received += posting['amount_minor']
    if (receipt.decision_id != decision.decision_id or receipt.tick != PERIOD or receipt.actor_id != ACTOR
            or bought != receipt.executed_quantity or spent != -receipt.cash_delta_minor or spent != received):
        raise ValueError('Goods delivery or cash legs differ from the purchase receipt')
    return {'visible_quotes': len(quotes), 'legal_choice': True,
            'supplier': f'S{sorted(quotes).index(intent.seller_id)+1}', 'settled_units': bought,
            'buyer_debit_minor': spent, 'supplier_credit_minor': received}, decision


def _requests(tape, attempts, observation, decision, committed):
    by_id = {row['attempt_id']: row for row in attempts}
    if any(by_id.get(row['attempt_id']) != row for row in committed):
        raise ValueError('Committed model facts differ from the invocation ledger')
    request_hashes = []
    frozen = tape.records[0]
    for row in attempts:
        request = row['request']
        projected = Observation.model_validate(request['observation'])
        if (semantic_key(request) != semantic_key(frozen.request)
                or projected.actor_id != ACTOR or projected.visible_until.tick != PERIOD):
            raise ValueError('Model request differs from the registered input')
        if observation is not None:
            for field in ('account_view', 'market_view', 'private_memory_refs', 'delivered_messages'):
                if any(item not in getattr(observation, field) for item in getattr(projected, field)):
                    raise ValueError('Model input contains evidence outside the authorized observation')
        if row['accounted'] or (row['response'] is not None and row['response'] != frozen.response.model_dump(mode='json')):
            raise ValueError('Invocation differs from the registered zero-price response')
        request_hashes.append(content_hash(request))
    trace = decision.belief_patch.get('model_supplier') if decision is not None else None
    fallback = bool(trace and trace.get('status') == 'rule_fallback')
    if decision is not None and (not trace or len(committed) != 1
            or trace['attempt_id'] != committed[0]['attempt_id']
            or committed[0]['state'] != ('failed' if fallback else 'success')):
        raise ValueError('The settled choice does not identify its authorized model result')
    return request_hashes, fallback


def summarize_supplier_arm(store, plan, tape):
    identity = store.identity
    side = supplier_side(plan, identity.config)
    expected_archive = plan['archive_hash'] if side == 'archive' else None
    if (identity.code_hash != plan['implementation']['source_hash']
            or identity.metadata['implementation']['dependency_hash'] != plan['implementation']['dependency_hash']
            or identity.metadata.get('response_archive_hash') != expected_archive
            or identity.metadata.get('parent') is not None):
        raise ValueError('Run identity differs from the frozen supplier comparison')
    cursor, prefix = [], []
    choice = decision = observation = None
    committed = []
    for marker, state in store.states():
        cursor.append(_cursor(store, marker))
        if marker.tick < PERIOD:
            prefix.append((content_hash(state.data['economic_runtime']), content_hash(list(store.observation_rows(marker)))))
        if marker.tick == PERIOD:
            observation, = (row for row in store.observation_rows(marker) if row.actor_id == ACTOR)
            choice, decision = _purchase(state, observation)
            committed = store.model_records(marker)
    if cursor[0]['tick'] != 0 or cursor[-1]['tick'] > plan['configs'][side]['ticks']:
        raise ValueError('The registered preview requires histories from the initial state')
    attempts, requests, fallback = [], [], False
    if side == 'archive':
        local_tape = SupplierResponseArchive.model_validate(read_json(store.root/'response-archive.json'))
        if content_hash(local_tape) != expected_archive:
            raise ValueError('Run response archive differs from the frozen input')
        attempts = ModelInvocationStore(store.root/'model-control.sqlite', read_only=True).records(run_id=identity.run_id)
        requests, fallback = _requests(tape, attempts, observation, decision, committed)
    elif committed:
        raise ValueError('Unexpected model response in the rule arm')
    counts = Counter(row['state'] for row in attempts)
    failed_runs = sum(row['status'] in ('failed', 'projection_failed') for row in history(store.root))
    public = {'side': side, 'config_hash': identity.config_hash, 'commits': cursor, 'choice': choice,
              'authorized_observation_hash': observation.payload_hash if observation is not None else None,
              'request_hashes': requests, 'archive_hash': expected_archive,
              'response_origin': 'synthetic_fixture' if side == 'archive' else None,
              'assigned_slots': int(side == 'archive'), 'attempts': len(attempts), 'successful_attempts': counts['success'],
              'failed_attempts': counts['failed'], 'fallbacks': int(fallback), 'failed_execution_attempts': failed_runs,
              'recorded_fixture_token_counts': {key: sum(row[field] or 0 for row in attempts)
                                                 for key, field in (('input', 'incoming'), ('output', 'outgoing'))},
              'external_api_calls': 0}
    return public, prefix


def supplier_comparison(plan, tape, stores):
    arms, prefixes = {}, {}
    for run in stores:
        arm, prefix = summarize_supplier_arm(run, plan, tape)
        if arm['side'] in arms:
            raise ValueError('Choose one rule run and one archived-response run')
        arms[arm['side']] = {'run_ref': run.identity.run_id, **arm}
        prefixes[arm['side']] = prefix
    if set(arms) != {'rule', 'archive'}:
        raise ValueError('Choose both registered supplier treatments')
    rule, archive = arms['rule'], arms['archive']
    if any(a != b for a, b in zip(prefixes['rule'], prefixes['archive'])):
        raise ValueError('The arms do not share the pre-decision economic state and observations')
    paired = rule['choice'] is not None and archive['choice'] is not None
    difference = None
    if paired:
        if rule['authorized_observation_hash'] != archive['authorized_observation_hash']:
            raise ValueError('The arms do not share the same authorized procurement opportunity')
        a, b = archive['choice'], rule['choice']
        redistribution = Counter()
        for sign, choice in ((1, a), (-1, b)):
            if choice['supplier']:
                redistribution[choice['supplier']] += sign * choice['supplier_credit_minor']
        difference = {'supplier_credit_change_minor': dict(redistribution), 'supplier_changed': a['supplier'] != b['supplier'],
                      'settled_units': a['settled_units'] - b['settled_units'],
                      'buyer_debit_minor': a['buyer_debit_minor'] - b['buyer_debit_minor'],
                      'supplier_credit_minor': a['supplier_credit_minor'] - b['supplier_credit_minor']}
    complete = all(arm['commits'][-1]['tick'] == plan['configs']['rule']['ticks'] for arm in arms.values())
    return {'schema_version': 1, 'audience': 'researcher', 'authority': False, 'design': ANALYSIS,
            'status': 'completed' if paired and complete else 'incomplete',
            'plan_hash': content_hash(plan), 'tick': PERIOD, 'arms': arms, 'difference': difference,
            'denominators': {'assigned_arms': 2, 'completed_arms': sum(
                arm['commits'][-1]['tick'] == plan['configs']['rule']['ticks'] for arm in arms.values()),
                'paired_opportunities': int(paired), 'model_slots': archive['assigned_slots'],
                'model_attempts': archive['attempts'], 'successful_model_attempts': archive['successful_attempts'],
                'failed_model_attempts': archive['failed_attempts'], 'rule_fallbacks': archive['fallbacks'],
                'failed_execution_attempts': sum(arm['failed_execution_attempts'] for arm in arms.values()),
                'natural_stops': 0}, 'external_api_calls': 0}


def audit_supplier_preview(registration, exported, workspace):
    plan, tape = load_supplier_preview(registration)
    doc = read_json(exported)
    runs, selected = (workspace/'runs').resolve(), []
    for side in ('rule', 'archive'):
        ref = doc['arms'][side]['run_ref']
        if not isinstance(ref, str) or len(ref) != 32 or any(ch not in '0123456789abcdef' for ch in ref):
            raise ValueError('Invalid preview run reference')
        path = runs/ref
        if path.is_symlink():
            raise ValueError('Unsafe preview archive path')
        selected.append(CommitStore(path))
    result = supplier_comparison(plan, tape, selected)
    if content_hash(doc) != content_hash(result):
        raise ValueError('Shareable export differs from the independent purchase audit')
    return result
