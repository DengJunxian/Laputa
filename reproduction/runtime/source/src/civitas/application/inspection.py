"""Read-only products for one committed prefix and one disclosure scope."""
from pathlib import Path

from civitas.application.economic_run import APPLICATION, PROJECTION_TABLES
from civitas.storage.commit_store import CommitStore, read_json
from civitas.storage.history import file_hash


def _projection(store, marker):
    root = store.root/'projections'/marker.commit_hash
    if not root.exists():
        return None
    manifest = read_json(root/'manifest.json')
    cursor = manifest.get('cursor', {})
    if (manifest.get('run_id') != store.identity.run_id or manifest.get('config_hash') != store.identity.config_hash
            or manifest.get('source_hash') != store.identity.code_hash or cursor != {
                'source_commit_hash':marker.commit_hash,'state_hash':marker.state_hash,'tick':marker.tick,
                'world_schema_version':4,'projection_version':'closed-economy.v1',
                'audience':'researcher','authority':False} or set(manifest.get('files',{})) != PROJECTION_TABLES):
        raise ValueError('Invalid fixed-cursor researcher projection manifest')
    tables = {}
    for name, digest in manifest['files'].items():
        path = root/name
        if path.is_symlink() or not path.is_file() or file_hash(path) != digest:
            raise ValueError('Damaged researcher projection')
        value = read_json(path)
        if value.get('cursor') != cursor:
            raise ValueError('Projection contains mixed table cursors')
        tables[name] = value
    return tables


def _section(key, label, columns, rows):
    return {'key':key, 'label':label, 'columns':columns, 'rows':rows}


def _research(store, marker, snapshot):
    tables = _projection(store, marker)
    economic = snapshot.data['economic_runtime']
    security = snapshot.data.get('security_market')
    scenario = snapshot.data.get('closed_economy', {})
    social = snapshot.data.get('closed_social', {})
    balances = economic['balances']
    macro = scenario.get('last_macro', {})
    receipts = (tables['receipts.json']['records'] if tables else
                [r.model_dump(mode='json') for r in snapshot.receipts if r.tick == marker.tick])
    decisions = (tables['decisions.json']['records'] if tables else
                 [d.model_dump(mode='json') for d in snapshot.decisions if d.tick == marker.tick])
    journal = [entry for entry in economic['journal'] if entry['tick'] == marker.tick]
    metrics = [
        ['现金总额',sum(b['cash'] for b in balances.values()),'最小货币单位'],
        ['产出',macro.get('produced_units',0),'商品单位'],
        ['消费',macro.get('consumed_units',0),'商品单位'],
        ['当期主体',len(snapshot.actors),'个'],
    ]
    if security is not None:
        last = security['history'][-1] if security['history'] else {}
        metrics[1:3] = [['当期成交价', last.get('close_minor'), '最小货币单位／份'],
                        ['当期成交量', last.get('volume_units', 0), '份']]
    receipts = [r for r in receipts if r['tick'] == marker.tick]
    decisions = [d for d in decisions if d['tick'] == marker.tick]
    models = sorted({(row['response'] or {}).get('model', row['request']['configuration']['model'])
                    + ' / ' + row['request']['configuration']['revision']
                    for row in store.model_records(marker)})
    accounts = [[actor,b['cash'],economic['goods'][actor],b.get('wage_receivable',0),
                 b.get('deposit_asset',0),b.get('bank_reserve',0)] for actor,b in sorted(balances.items())]
    comparison = []
    relations = [[r.source_id,r.target_id,r.layer,r.delta_id] for r in snapshot.relations.values()]
    relations.extend([[actor,neighbor,'communication','初始邻接']
                      for actor, neighbors in sorted((social.get('neighbors') or {}).items())
                      for neighbor in neighbors])
    network = social.get('network')
    if network is not None:
        relations.extend([[edge['source_id'],edge['target_id'],'communication',edge['delta_id']]
                          for edge in network.get('applied', []) if edge.get('effective_tick') == marker.tick])
    for decision in decisions:
        patch = decision['belief_patch']
        choice = patch.get('closed_choice',{})
        scores = patch.get('model_supplier',{}).get('applied_scores_bps',choice.get('effective_scores_bps',{}))
        appraisal = patch.get('security_appraisal', {})
        comparison.append([decision['actor_id'],decision['policy_id']+' / '+decision['policy_version'],
                           appraisal.get('exposed_source') if security is not None else choice.get('chosen_seller'),
                           dict(appraisal) if security is not None else
                           dict(scores),decision['evidence_ids']])
    status = 'committed'
    log = store.root/'attempts'
    if log.is_dir() and not log.is_symlink():
        from civitas.experiments import registry
        statuses = [entry['status'] for entry in registry.history(store.root)
                    if entry.get('tick') == marker.tick and entry.get('status') in (
                        'completed','checkpointed','failed','cancelled','projection_failed')]
        if statuses:
            status = statuses[-1]
    return {'metrics':metrics, 'data_origin':store.identity.metadata.get('data_origin','unknown'),
            'model_versions':models, 'status':status, 'source_kind':'verified_eight_tables' if tables else 'commit',
            'sections':[
                _section('accounts','账户',['主体','现金','证券份额' if security is not None else '库存',
                         '应收工资','存款债权','准备金'],accounts),
                _section('receipts','执行回执',['命令','主体','结果','数量','现金变化','关联分录'],
                         [[r[k] for k in ('command_id','actor_id','outcome','executed_quantity','cash_delta_minor','ledger_tx_ids')]
                          for r in receipts]),
                _section('evidence','选择与证据',['主体','策略',
                         *(['曝光来源','报价判断'] if security is not None else ['选中卖方','供应商分数（基点）']),
                         '证据 ID'],comparison),
                _section('journal','账本分录',['事务','类型','分录数','已记详情'],
                         [[e['tx_id'],e['kind'],len(e['postings']),e['details']] for e in journal]),
                _section('relations','关系变化',['来源','目标','类型','记录'],relations),
            ]}


def _agent(store, marker, snapshot, actor):
    if actor not in snapshot.actors:
        raise ValueError('Actor is not registered at this commit')
    rows = store.observation_rows(marker)
    if not rows:
        raise ValueError('This commit did not retain its original OBSERVE inputs')
    observations = {row.actor_id:row for row in rows}
    decisions = {d.actor_id:d.observation_id for d in snapshot.decisions if d.tick == marker.tick}
    if (set(observations) != set(snapshot.actors)
            or {a:o.observation_id for a,o in observations.items()} != decisions):
        raise ValueError('Observation archive does not match committed decision inputs')
    return _agent_payload(observations[actor])


def _agent_payload(observation):
    payload = observation.model_dump(mode='json')

    def facts(collection):
        return [[f['fact_id'],f['visible_at']['tick'],f['value']] for f in payload[collection]]

    return {'source_kind':'committed_observe','observation_id':observation.observation_id,
            'observation_hash':observation.payload_hash,
            'sections':[
                _section('account','账户快照',['证据 ID','可见期','事实'],facts('account_view')),
                _section('market','市场与制度',['证据 ID','可见期','事实'],facts('market_view')),
                _section('messages','收到的消息',['消息 ID','发送者','可见期','内容'],
                    [[m['message_id'],m['sender_id'],m['visible_at']['tick'],m['body']]
                     for m in payload['delivered_messages']]),
                _section('memory','可见回执与记忆',['证据 ID','可见期','事实'],facts('private_memory_refs')),
            ]}


def _inspection_ticks(ticks, audience, actor):
    if audience not in ('researcher','agent') or (audience == 'agent') != (actor is not None):
        raise ValueError('Choose researcher or an explicit single-agent scope')
    ticks = list(ticks)
    if not ticks or len(ticks) > 100 or len(set(ticks)) != len(ticks) or any(type(t) is not int or t < 0 for t in ticks):
        raise ValueError('Choose 1–100 distinct committed ticks')
    return sorted(ticks)


def _inspection_store(root):
    store = CommitStore(root)
    if store.identity.metadata.get('application') != APPLICATION:
        raise ValueError('Inspection currently supports the C/D v4 runner')
    return store


def _cursor(store, marker):
    return {'tick':marker.tick,'source_commit_hash':marker.commit_hash,'state_hash':marker.state_hash,
            'source_hash':store.identity.code_hash}


def _frame(store, marker, snapshot, audience, actor):
    if snapshot.schema_version != 4:
        raise ValueError('Unsupported inspection snapshot schema')
    view = _research(store,marker,snapshot) if audience=='researcher' else _agent(store,marker,snapshot,actor)
    return {'cursor':_cursor(store, marker), **view}


def _indexed_agent_frame(store, records, marker, actor):
    observation = store.indexed_observation_for(records, marker, actor)
    return {'cursor': _cursor(store, marker), 'verification_scope': 'indexed_observation_cursor',
            **_agent_payload(observation)}


def _inspection_document(store, audience, actor, views):
    return {'schema_version':1,'audience':audience, 'actor_id':actor,
            'run_id':store.identity.run_id, 'authority':False, 'views':views}


def inspect_run(root, ticks, *, audience, actor=None):
    ticks = _inspection_ticks(ticks, audience, actor)
    store = _inspection_store(root)
    if audience == 'agent' and store.observation_archive == 'observe.v2':
        records = store.markers()
        markers = {record.tick: record for record in records}
        if any(tick not in markers for tick in ticks):
            raise ValueError('No committed observation period for the selected cursor')
        views = [_indexed_agent_frame(store, records, markers[tick], actor) if markers[tick].kind == 'tick'
                 else _frame(store, *store.load(tick), audience, actor) for tick in ticks]
    else:
        views = [_frame(store, *store.load(tick), audience, actor) for tick in ticks]
    return _inspection_document(store, audience, actor, views)


def write_inspection(root, destination, ticks, *, audience, actor=None, format='html'):
    if format not in ('html','json'):
        raise ValueError('Expected html or json inspection format')
    destination = Path(destination).resolve()
    if destination.is_relative_to(Path(root).resolve()):
        raise ValueError('Inspection output must be outside its source run')
    data = inspect_run(root,ticks,audience=audience,actor=actor)
    if format == 'html':
        from .inspection_html import render
        body = render(data)
    else:
        from civitas.storage.commit_store import json_bytes
        body = json_bytes(data).decode()
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('x',encoding='utf-8') as handle:
        handle.write(body)
    return {'path':str(destination),'format':format,'audience':audience,
            'actor_id':actor,'ticks':[v['cursor']['tick'] for v in data['views']]}
