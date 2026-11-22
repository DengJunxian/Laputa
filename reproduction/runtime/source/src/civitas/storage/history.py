"""Immutable, portable copies of a verified commit prefix."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from civitas.contracts.base import content_hash
from .commit_store import CommitStore, atomic_json, sync_directory


def file_hash(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


@contextmanager
def publish_directory(destination, *, create_staging=True):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.projection-', dir=destination.parent) as temp:
        pending = Path(temp)/'publish'
        if create_staging:
            pending.mkdir()
        yield pending
        sync_directory(pending)
        if destination.exists():
            raise FileExistsError(destination)
        pending.rename(destination)
        sync_directory(destination.parent)


def parent_reference(store, marker):
    return {'path': str(store.root), 'identity_hash': content_hash(store.identity),
            'run_id': store.identity.run_id, 'tick': marker.tick, 'commit_hash': marker.commit_hash}


def validate_parent(store):
    lineage = [store]
    seen = {store.root}
    while 'parent' in store.identity.metadata:
        parent = store.identity.metadata['parent']
        source = CommitStore(parent['path'])
        if source.root in seen:
            raise ValueError('Cyclic branch lineage')
        initial = store.markers()[0]
        selected = next((r for r in source.verified_markers() if r.tick == initial.tick), None)
        if (selected is None or parent != parent_reference(source, selected)
                or (selected.tick, selected.world_version, selected.state_hash) !=
                   (initial.tick, initial.world_version, initial.state_hash)
                or any(getattr(store.identity, key) != getattr(source.identity, key)
                       for key in ('lineage_id', 'config_hash', 'code_hash', 'participants', 'rng'))):
            raise ValueError('Parent reference or branch prefix mismatch')
        seen.add(source.root)
        lineage.append(source)
        store = source
    return lineage


def retain_history(root, destination, *, tick=None):
    store = CommitStore(root)
    selected, _ = store.load(tick)
    records = [record for record in store.markers() if record.tick <= selected.tick]
    paths = {'identity.json'}
    for record in records:
        paths.add(str(store.marker_path(record.tick).relative_to(store.root)))
        for reference in (record.segment, record.model_facts, record.observations):
            if reference is not None:
                paths.add(str(store.segment_path(reference).relative_to(store.root)))
    with publish_directory(destination) as staging:
        for folder in ('commits', 'segments', 'checkpoints', 'diagnostics'):
            (staging/folder).mkdir()
        files = {}
        for name in sorted(paths):
            source, target = store.root/name, staging/name
            if source.is_symlink():
                raise ValueError('Unsafe retained history reference')
            with source.open('rb') as reader, target.open('xb') as writer:
                shutil.copyfileobj(reader, writer, length=1024*1024)
                writer.flush()
                os.fsync(writer.fileno())
            files[name] = file_hash(target)
        for folder in ('commits', 'segments', 'checkpoints', 'diagnostics'):
            sync_directory(staging/folder)
        recovered, _ = CommitStore(staging).load()
        if recovered != selected:
            raise ValueError('Retained prefix did not verify at the selected commit')
        manifest = {'schema_version':1, 'history_policy':'retain_full_prefix', 'run_id':store.identity.run_id,
                    'first_tick':records[0].tick,'tick':selected.tick,'source_commit_hash':selected.commit_hash,
                    'state_hash':selected.state_hash,'files':files}
        atomic_json(staging/'retention.json', manifest)
    return manifest
