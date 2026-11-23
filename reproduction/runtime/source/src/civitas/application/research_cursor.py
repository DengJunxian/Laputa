"""Rebuildable researcher pages for a single verified economic commit."""
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
import re

from core.experiments.strict_types import strict_json_loads
from civitas.application.inspection import _cursor, _inspection_store
from civitas.contracts.base import content_hash
from civitas.storage.checkpoint import validate_versions
from civitas.storage.commit_store import atomic_json, json_bytes, read_json
from civitas.storage.history import publish_directory


PAGE_SIZE = 64


def _source(root, tick, commit):
    if type(tick) is not int or tick < 0 or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{64}', commit):
        raise ValueError('Select a period and its full commit hash')
    store = _inspection_store(root)
    validate_versions(store.identity.participants)
    prefix = [marker for marker in store.markers() if marker.tick <= tick]
    if not prefix or prefix[-1].tick != tick:
        raise FileNotFoundError('No committed period for this research cursor')
    if prefix[-1].commit_hash != commit:
        raise ValueError('Commit does not match the selected period')
    return store, prefix


def _provenance(prefix):
    return {'state_segment_sha256': prefix[-1].segment.sha256,
            'state_prefix_sha256': content_hash([marker.segment.sha256 for marker in prefix]),
            'state_segments': len(prefix)}


def _reconcile(snapshot):
    if snapshot.schema_version != 4 or 'security_market' in snapshot.data:
        raise ValueError('Research accounts and goods aggregates require a v4 goods economy')
    state = snapshot.data['economic_runtime']
    balances = state['balances']
    cash = dict.fromkeys(balances, 0)
    consumed = defaultdict(int)
    produced = 0
    for entry in state['journal']:
        for posting in entry['postings']:
            if posting['account_code'] == 'cash':
                sign = 1 if posting['side'] == 'debit' else -1
                cash[posting['entity_id']] += sign * posting['amount_minor']
        if entry['tick'] == snapshot.tick:
            for movement in entry['goods_movements']:
                units = movement['quantity_units']
                if movement['kind'] == 'production':
                    produced += units
                elif movement['kind'] == 'consumption':
                    consumed[movement['from_owner'], entry['tx_id']] += units
    receipts = [row for row in snapshot.receipts if row.tick == snapshot.tick]
    settled = {}
    for row in receipts:
        if row.outcome == 'consumed':
            if len(row.ledger_tx_ids) != 1 or (row.actor_id, row.ledger_tx_ids[0]) in settled:
                raise ValueError('Consumption receipt has a missing or duplicate ledger reference')
            settled[row.actor_id, row.ledger_tx_ids[0]] = row.executed_quantity
    decisions = [row.actor_id for row in snapshot.decisions if row.tick == snapshot.tick]
    if cash != {actor: row['cash'] for actor, row in balances.items()} or consumed != settled:
        raise ValueError('Research metrics disagree with the committed cash or consumption records')
    if snapshot.tick and sorted(decisions) != sorted(snapshot.actors):
        raise ValueError('The committed decision count does not cover the actor roster')
    metrics = {'cash_minor': sum(cash.values()), 'produced_units': produced,
               'consumed_units': sum(settled.values()), 'actor_count': len(snapshot.actors)}
    macro = snapshot.data['closed_economy']['last_macro']
    if any(metrics[key] != macro.get(key, 0) for key in ('produced_units', 'consumed_units')):
        raise ValueError('Journal goods movements disagree with the committed macro totals')
    return metrics, {'decisions': len(decisions), 'receipts': len(receipts), 'accounts': len(balances)}


def build_research_index(root, destination, *, tick, commit, state_only=False):
    store, prefix = _source(root, tick, commit)
    destination = Path(destination)
    if destination.is_symlink() or destination.resolve().is_relative_to(store.root):
        raise ValueError('Research index must be outside the source archive')
    if destination.exists():
        raise FileExistsError('The research index already exists')
    marker, snapshot = store.load_state_chain(tick) if state_only else store.load(tick)
    if marker != prefix[-1]:
        raise ValueError('Commit changed while building the research index')
    metrics, denominators = _reconcile(snapshot)
    state = snapshot.data['economic_runtime']
    rows = [{'account_id': actor, 'cash_minor': balance['cash'], 'goods_units': state['goods'][actor],
             'wage_receivable_minor': balance.get('wage_receivable', 0),
             'deposit_asset_minor': balance.get('deposit_asset', 0), 'bank_reserve_minor': balance.get('bank_reserve', 0),
             **({'security_units': state['security_units'].get(actor, 0),
                 'loan_payable_minor': -balance.get('loan_payable', 0)}
                if store.identity.config.get('security_feedback') else {})}
            for actor, balance in sorted(state['balances'].items())]
    with publish_directory(destination) as staging:
        staging.chmod(0o700)
        hashes = []
        for page, offset in enumerate(range(0, len(rows), PAGE_SIZE), start=1):
            block = rows[offset:offset+PAGE_SIZE]
            atomic_json(staging/f'accounts-{page}.json', block)
            hashes.append(sha256(json_bytes(block)).hexdigest())
        index = {'schema_version': 1, 'audience': 'researcher', 'authority': False,
                 'run_id': store.identity.run_id, 'config_hash': store.identity.config_hash,
                 'data_origin': store.identity.metadata.get('data_origin', 'unknown'),
                 'cursor': _cursor(store, marker), 'provenance': _provenance(prefix),
                 'metrics': metrics, 'denominators': denominators, 'page_sha256': hashes}
        digest = content_hash(index)
        atomic_json(staging/'manifest.json', {**index, 'index_sha256': digest})
    return {'cursor': index['cursor'], 'path': str(destination.resolve()), 'index_sha256': digest,
            'verification_scope': 'verified_commit_state', 'account_pages': len(hashes)}


def _verified_index(root, index, *, tick, commit):
    store, prefix = _source(root, tick, commit)
    index = Path(index)
    if index.is_symlink() or not index.is_dir():
        raise FileNotFoundError('Research index is unavailable')
    manifest = read_json(index/'manifest.json')
    if not isinstance(manifest, dict):
        raise ValueError('Invalid research index manifest')
    digest = manifest.pop('index_sha256', None)
    if (digest != content_hash(manifest) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('audience') != 'researcher' or manifest.get('authority') is not False
            or manifest.get('run_id') != store.identity.run_id
            or manifest.get('config_hash') != store.identity.config_hash
            or manifest.get('data_origin') != store.identity.metadata.get('data_origin', 'unknown')
            or manifest.get('cursor') != _cursor(store, prefix[-1]) or manifest.get('provenance') != _provenance(prefix)):
        raise ValueError('Research index digest or source cursor mismatch')
    hashes = manifest.pop('page_sha256', None)
    denominators = manifest.get('denominators')
    total = denominators.get('accounts') if isinstance(denominators, dict) else None
    if type(total) is not int or total < 1 or not isinstance(hashes, list) or len(hashes) != (total+PAGE_SIZE-1)//PAGE_SIZE:
        raise ValueError('Invalid research account index')
    for marker in prefix:
        store._segment_bytes(marker.segment)
    summary = {**manifest, 'index_sha256': digest, 'verification_scope': 'indexed_research_cursor'}
    return index, summary, hashes, total


def research_summary(root, index, *, tick, commit):
    _, summary, _, _ = _verified_index(root, index, tick=tick, commit=commit)
    return summary


def research_page(root, index, *, tick, commit, page=1):
    index, summary, hashes, total = _verified_index(root, index, tick=tick, commit=commit)
    if type(page) is not int or not 1 <= page <= len(hashes):
        raise ValueError('No such research account page')
    path = index/f'accounts-{page}.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing indexed account page')
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != hashes[page-1]:
        raise ValueError('Indexed account page digest mismatch')
    rows = strict_json_loads(raw)
    if not isinstance(rows, list) or len(rows) != min(PAGE_SIZE, total-(page-1)*PAGE_SIZE):
        raise ValueError('Indexed account page count mismatch')
    return {**summary, 'accounts': {'page': page, 'page_size': PAGE_SIZE, 'total': total, 'rows': rows,
                         'next_page': page+1 if page < len(hashes) else None, 'page_sha256': hashes[page-1]}}
