"""Append-only execution receipts; no successful or failed attempt is overwritten."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import uuid

from civitas.storage.commit_store import atomic_json, read_json


def record(root, value):
    folder = Path(root)/'attempts'
    folder.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    path = folder/(now.replace(':','-')+'-'+uuid.uuid4().hex+'.json')
    atomic_json(path, {'schema_version':1,'recorded_at':now,**value})
    return path


def history(root):
    return [read_json(p) for p in sorted((Path(root)/'attempts').glob('*.json'))]


def status(root):
    """Public control-plane summary: no accounts, messages or private memories."""
    from civitas.storage.commit_store import CommitStore
    store = CommitStore(root)
    marker = store.verified_markers()[-1]
    attempts = history(root)
    return {'run_id':store.identity.run_id,'tick':marker.tick,'commit_hash':marker.commit_hash,
            'state_hash':marker.state_hash,'last_execution_status':attempts[-1]['status'] if attempts else 'initialized'}


@contextmanager
def run_lock(root):
    with (Path(root)/'run.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
