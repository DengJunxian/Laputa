"""Rebuildable researcher tables; never used to recover or decide world state."""
import hashlib
import os
from pathlib import Path

from .commit_store import atomic_json, json_bytes, sync_directory


def export_parquet(store, destination):
    import pyarrow as pa
    import pyarrow.parquet as pq
    head, snapshot = store.load()
    destination = Path(destination)
    destination.mkdir(parents=True,exist_ok=False)
    markers = store.markers()
    tables = {
        'commits':pa.table({'tick':pa.array([r.tick for r in markers],type=pa.int64()),
            'commit_hash':pa.array([r.commit_hash for r in markers],type=pa.string()),
            'state_hash':pa.array([r.state_hash for r in markers],type=pa.string())}),
        'receipts':pa.table({'command_id':pa.array([r.command_id for r in snapshot.receipts],type=pa.string()),
            'actor_id':pa.array([r.actor_id for r in snapshot.receipts],type=pa.string()),
            'execution_seq':pa.array([r.execution_seq for r in snapshot.receipts],type=pa.int64()),
            'body_json':pa.array([json_bytes(r.model_dump(mode='json')).decode() for r in snapshot.receipts],type=pa.string())}),
    }
    artifacts = {}
    for name, table in tables.items():
        path = destination/(name+'.parquet')
        pq.write_table(table,path,compression='zstd')
        with path.open('rb') as handle:
            os.fsync(handle.fileno())
        artifacts[path.name] = {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size,
                                'rows':table.num_rows,'arrow_schema':str(table.schema)}
    sync_directory(destination)
    receipt = {'schema_version':1,'authority':False,'source_commit':head.commit_hash,'state_hash':head.state_hash,
        'pyarrow_version':pa.__version__,'audience':'researcher','files':artifacts}
    atomic_json(destination/'manifest.json',receipt)
    return receipt


# Keep v1 export_parquet for callers of the financial/archive adapter above.
def export_economic_tables(root, destination, *, tick=None, format='parquet', batch_rows=256):
    from itertools import islice
    from .commit_store import CommitStore
    from .history import file_hash, publish_directory

    if type(batch_rows) is not int or not 1 <= batch_rows <= 65536 or format not in ('parquet', 'jsonl'):
        raise ValueError('Choose parquet or jsonl and a bounded positive batch size')
    if format == 'parquet':
        import pyarrow as pa
        import pyarrow.parquet as pq
    store = CommitStore(root)
    marker, snapshot = store.load(tick)
    if snapshot.schema_version != 4:
        raise ValueError('Economic table format v2 needs a v4 source commit')
    ledger = snapshot.data['economic_runtime']['journal']
    schema = {
        'commits': {'tick':'int', 'commit_hash':'str', 'state_hash':'str'},
        'receipts': {'tick':'int', 'command_id':'str', 'actor_id':'str', 'execution_seq':'int', 'outcome':'str',
                     'status':'str', 'unit':'str', 'executed_quantity':'int', 'cash_delta_minor':'int',
                     'counterparty_id':'str', 'ledger_tx_ids':'strings'},
        'decisions': {'tick':'int', 'actor_id':'str', 'decision_id':'str', 'observation_id':'str',
                      'policy_id':'str', 'policy_version':'str', 'evidence_ids':'strings'},
        'journal': {'tick':'int', 'tx_id':'str', 'event_id':'str', 'kind':'str', 'payload_hash':'str'},
        'postings': {'tick':'int', 'tx_id':'str', 'posting_id':'str', 'entity_id':'str', 'currency_id':'str',
                     'account_code':'str', 'side':'str', 'amount_minor':'int'},
    }

    def attributes(rows, table):
        return ({k:getattr(row,k) for k in schema[table]} for row in rows)

    def postings():
        for entry in ledger:
            for posting in entry['postings']:
                yield {'tick':entry['tick'], **dict(posting)}

    tables = {'commits':attributes((r for r in store.markers() if r.tick <= marker.tick), 'commits'),
              'receipts':attributes(snapshot.receipts, 'receipts'),
              'decisions':attributes(snapshot.decisions, 'decisions'),
              'journal':({k:e[k] for k in schema['journal']} for e in ledger),
              'postings':postings()}
    files = {}
    with publish_directory(destination) as staging:
        for name, rows in tables.items():
            target = staging/f'{name}.{format}'
            count = 0
            if format == 'parquet':
                types = {'int':pa.int64(), 'str':pa.string(), 'strings':pa.list_(pa.string())}
                contract = pa.schema([(k,types[v]) for k,v in schema[name].items()])
                with pq.ParquetWriter(target, contract, compression='zstd') as writer:
                    while batch := list(islice(rows, batch_rows)):
                        writer.write_table(pa.Table.from_pylist(batch, schema=contract), row_group_size=batch_rows)
                        count += len(batch)
                with target.open('rb') as handle:
                    os.fsync(handle.fileno())
            else:
                with target.open('xb') as handle:
                    for row in rows:
                        handle.write(json_bytes(row))
                        count += 1
                    handle.flush()
                    os.fsync(handle.fileno())
            files[target.name] = {'sha256':file_hash(target), 'bytes':target.stat().st_size,
                                  'rows':count, 'columns':schema[name]}
        manifest = {'schema_version':2, 'audience':'researcher', 'authority':False, 'format':format,
                    'run_id':store.identity.run_id, 'source_hash':store.identity.code_hash,
                    'config_hash':store.identity.config_hash, 'tick':marker.tick,
                    'source_commit_hash':marker.commit_hash, 'state_hash':marker.state_hash,
                    'batch_rows':batch_rows, 'pyarrow_version':pa.__version__ if format=='parquet' else None,
                    'files':files}
        atomic_json(staging/'manifest.json', manifest)
    return manifest
