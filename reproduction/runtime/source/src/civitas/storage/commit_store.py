"""A fsynced marker is the sole authority; indexes never decide commitment."""
from contextlib import closing, contextmanager
from copy import deepcopy
import fcntl
import gzip
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from pydantic import Field, TypeAdapter
from core.experiments.identity import CanonicalListPrefix, SEALED_SNAPSHOT_LISTS
from core.experiments.strict_types import SealedJSONRowsV1, SealedRecordRows, strict_json_loads
from civitas.contracts.base import Digest, Id, NonNegative, Record, content_hash
from civitas.contracts.codecs import decode_snapshot
from civitas.contracts.observations import Observation
from civitas.contracts.state import WorldSnapshot, FinancialWorldSnapshot, PortfolioWorldSnapshot, EconomicWorldSnapshot

# Kept for callers using the historical public adapter. Authoritative reads below
# use explicit, closed dispatch rather than trying arbitrary record families.
SNAPSHOT = TypeAdapter(WorldSnapshot | FinancialWorldSnapshot | PortfolioWorldSnapshot | EconomicWorldSnapshot)
OBSERVATION_BLOCK_SIZE = 32
_JSON_WIRE = TypeAdapter(Any)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')


def _canonical_json(raw):
    value = strict_json_loads(raw.decode('utf-8'))
    if json_bytes(value) != raw:
        raise ValueError('Noncanonical segment JSON')
    return value


def _observation_member(actor):
    return hashlib.sha256(actor.encode('utf-8')).hexdigest() + '.json'


def _observation_block(number):
    return f'rows-{number:04d}.json'


def stored_json_bytes(value, *, compressed=False):
    raw = json_bytes(value)
    return gzip.compress(raw,compresslevel=1,mtime=0) if compressed else raw


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value, *, replace=True):
    path = Path(path)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(stored_json_bytes(value,compressed=path.name.endswith('.gz')))
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    sync_directory(path.parent)


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Missing or unsafe artifact: {path.name}')
    raw = path.read_bytes()
    if path.name.endswith('.gz'):
        try:
            raw = gzip.decompress(raw)
        except (OSError,EOFError) as exc:
            raise ValueError('Invalid compressed JSON artifact') from exc
    return strict_json_loads(raw.decode('utf-8'))


class StateChange(Record):
    op: Literal['set', 'delete', 'append']
    path: list[str | NonNegative] = Field(min_length=1)
    value: Any = None
    offset: NonNegative | None = None


class StateDelta(Record):
    schema_version: Literal[1] = 1
    base_hash: Digest
    state_hash: Digest
    changes: list[StateChange]


def differences(before, after, path=()):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        rows = [StateChange(op='delete', path=[*path, key]) for key in sorted(before.keys()-after.keys())]
        for key in sorted(after):
            rows.extend(differences(before[key], after[key], (*path, key)) if key in before else
                        [StateChange(op='set', path=[*path, key], value=after[key])])
        return rows
    if isinstance(before, list) and isinstance(after, list) and len(after) >= len(before):
        if len(after) > len(before) and before == after[:len(before)]:
            return [StateChange(op='append', path=list(path), value=after[len(before):], offset=len(before))]
        rows = []
        for index, value in enumerate(before):
            rows.extend(differences(value, after[index], (*path, index)))
        if len(after) > len(before):
            rows.append(StateChange(op='append', path=list(path), value=after[len(before):], offset=len(before)))
        return rows
    if type(after) is StateChange:
        return [after]
    return [StateChange(op='set', path=list(path), value=after)]


def _snapshot_changes(base, snapshot):
    if type(base) is not EconomicWorldSnapshot or type(snapshot) is not EconomicWorldSnapshot:
        return differences(base.model_dump(mode='json'), snapshot.model_dump(mode='json'))

    exclude = {}
    appends = []
    for field in ('events', 'decisions', 'receipts', 'messages', 'memories'):
        before, after = getattr(base, field), getattr(snapshot, field)
        offset = len(before)
        shared = (after.retains(before) if type(before) is type(after) is SealedRecordRows
                  else len(after) >= offset and all(left is right for left, right in zip(before, after)))
        if shared:
            exclude[field] = True
            if len(after) > offset:
                appends.append(([field], offset, [row.model_dump(mode='json') for row in after[offset:]]))

    for domain, field in SEALED_SNAPSHOT_LISTS:
        old_group, new_group = base.data.get(domain), snapshot.data.get(domain)
        before = old_group.get(field) if isinstance(old_group, dict) else None
        after = new_group.get(field) if isinstance(new_group, dict) else None
        if type(before) is type(after) is SealedJSONRowsV1 and after.retains(before):
            exclude.setdefault('data', {}).setdefault(domain, set()).add(field)
            offset = len(before)
            if len(after) > offset:
                appends.append((['data', domain, field], offset, _JSON_WIRE.dump_python(after[offset:], mode='json')))

    old = base.model_dump(mode='json', exclude=exclude)
    new = snapshot.model_dump(mode='json', exclude=exclude)
    for path, offset, rows in appends:
        old_parent, new_parent = old, new
        for key in path[:-1]:
            old_parent, new_parent = old_parent[key], new_parent[key]
        # Leave the append at its original position in the wire diff order.
        old_parent[path[-1]] = None
        new_parent[path[-1]] = StateChange(op='append', path=path, value=rows, offset=offset)
    return differences(old, new)


def apply_delta(before, delta):
    value = deepcopy(before)
    _advance_state(value, delta, base_hash=content_hash(before))
    return value


def _advance_state(value, delta, *, base_hash, journal_prefix=None, commit_state_hash=None):
    """Advance a private recovery buffer with a hash verified at its last cursor."""
    delta = StateDelta.model_validate(delta)
    if base_hash != delta.base_hash:
        raise ValueError('Delta base hash mismatch')
    for change in delta.changes:
        if journal_prefix is not None:
            journal_prefix.invalidate(change.path, change.op)
        payload = change.model_dump(mode='json')['value']
        target = value
        for key in change.path[:-1]:
            target = target[key]
        key = change.path[-1]
        if change.op == 'set':
            target[key] = payload
        elif change.op == 'delete':
            del target[key]
        else:
            array = target[key]
            if not isinstance(array, list) or len(array) != change.offset or not isinstance(change.value, list):
                raise ValueError('Delta append offset/type mismatch')
            array.extend(payload)
    if commit_state_hash is not None:
        digest = commit_state_hash
    else:
        digest = journal_prefix.digest(value) if journal_prefix is not None else content_hash(value)
    if digest != delta.state_hash:
        raise ValueError('Delta result hash mismatch')
    return delta.state_hash


class Segment(Record):
    sha256: Digest
    bytes: NonNegative
    encoding: Literal['json', 'json+gzip', 'observe+zip'] = 'json'


class StoreIdentity(Record):
    schema_version: Literal[1] = 1
    run_id: Id
    lineage_id: Id
    branch_id: Id
    code_hash: Digest
    config_hash: Digest
    config: dict[str, Any]
    participants: dict[str, int]
    rng: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class Commit(Record):
    schema_version: Literal[1] = 1
    kind: Literal['initial', 'tick']
    tick: NonNegative
    world_version: NonNegative
    parent_commit_hash: Digest | None
    identity_hash: Digest
    state_hash: Digest
    segment: Segment
    model_facts: Segment | None = None
    observations: Segment | None = Field(default=None, exclude_if=lambda value: value is None)
    invocation_receipt_refs: list[Id] = Field(default_factory=list)
    commit_hash: Digest


class CommitStore:
    def __init__(self, root, *, fault_hook=None):
        self.root = Path(root).resolve()
        self.identity = StoreIdentity.model_validate(read_json(self.root/'identity.json'))
        if content_hash(self.identity.config) != self.identity.config_hash:
            raise ValueError('Configuration hash mismatch')
        self.observation_archive = self.identity.metadata.get('observation_archive')
        if self.observation_archive not in (None, 'observe.v1', 'observe.v2'):
            raise ValueError('Unknown observation archive format')
        self._identity_hash = content_hash(self.identity)
        self.fault_hook = fault_hook
        self.receipt_reader = None
        self._trusted_head = None

    @classmethod
    def create(cls, root, world, *, config, code_hash, run_id=None, lineage_id=None, branch_id=None, metadata=None):
        from .checkpoint import participant_versions
        versions = participant_versions(world)
        run_id = run_id or Path(root).name
        identity = StoreIdentity(run_id=run_id, lineage_id=lineage_id or run_id, branch_id=branch_id or run_id,
            code_hash=code_hash, config_hash=content_hash(config), config=config, participants=versions,
            rng=world.rng._root.model_dump(mode='json'), metadata=metadata or {})
        return cls.initialize(root, identity, world.snapshot)

    @classmethod
    def initialize(cls, root, identity, snapshot):
        from .checkpoint import validate_versions
        identity = StoreIdentity.model_validate(identity)
        validate_versions(identity.participants)
        snapshot = decode_snapshot(snapshot)
        root = Path(root)
        root.mkdir(parents=True, exist_ok=False)
        for name in ('commits', 'segments', 'checkpoints', 'diagnostics'):
            (root/name).mkdir()
        atomic_json(root/'identity.json', identity.model_dump(mode='json'))
        sync_directory(root.parent)
        store = cls(root)
        store._publish(snapshot, snapshot.model_dump(mode='json'), kind='initial', parent=None, refs=[])
        return store

    @contextmanager
    def writer(self):
        with (self.root/'writer.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def fault(self, point):
        if self.fault_hook:
            self.fault_hook(point)

    def marker_path(self, tick):
        return self.root/'commits'/f'{tick:08d}.json'

    def _read_marker(self, path):
        raw = read_json(path)
        record = Commit.model_validate(raw)
        if (record.commit_hash != content_hash({k:v for k,v in raw.items() if k!='commit_hash'})
                or record.identity_hash != self._identity_hash or path != self.marker_path(record.tick)):
            raise ValueError(f'Commit identity/hash mismatch: {path.name}')
        return record

    def _marker_paths(self):
        # Directory shape is cheap to check and does not require reading every marker.
        paths = sorted((self.root/'commits').glob('*.json'))
        if not paths or not all(p.stem.isascii() and p.stem.isdecimal() for p in paths):
            raise ValueError('Missing or invalid commit markers')
        first = int(paths[0].stem)
        if any(path != self.marker_path(first + offset) for offset, path in enumerate(paths)):
            raise ValueError('Broken commit chain: non-contiguous marker paths')
        return paths

    def markers(self):
        records = [self._read_marker(path) for path in self._marker_paths()]
        if not records or records[0].kind != 'initial' or records[0].parent_commit_hash is not None:
            raise ValueError('Missing initial commit')
        for previous, current in zip(records, records[1:]):
            if (current.kind != 'tick' or current.tick != previous.tick+1
                    or current.world_version != previous.world_version+1 or current.parent_commit_hash != previous.commit_hash):
                raise ValueError(f'Broken commit chain at tick {current.tick}')
        self._trusted_head = records[-1]
        return records

    def segment(self, record):
        return self.read_segment(record.segment)

    def segment_path(self, reference):
        suffix = {'json': '.json', 'json+gzip': '.json.gz', 'observe+zip': '.observe.zip'}[reference.encoding]
        return self.root/'segments'/(reference.sha256 + suffix)

    def _segment_bytes(self, reference):
        path = self.segment_path(reference)
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing referenced segment')
        raw = path.read_bytes()
        if len(raw) != reference.bytes or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError('Segment byte hash mismatch')
        return raw

    def read_segment(self, reference):
        if reference.encoding == 'observe+zip':
            raise ValueError('An indexed observation segment requires its observation reader')
        raw = self._segment_bytes(reference)
        if reference.encoding=='json+gzip':
            try:
                raw = gzip.decompress(raw)
            except (OSError,EOFError) as exc:
                raise ValueError('Invalid compressed segment') from exc
        return _canonical_json(raw)

    def verified_markers(self):
        from .checkpoint import validate_versions
        validate_versions(self.identity.participants)
        records = self.markers()
        initial = None
        # Verify every byte reference before exposing any recovered state.
        for index, record in enumerate(records):
            payload = self.segment(record)
            self.model_records(record)
            observations = self.observation_rows(record)
            if index:
                delta = StateDelta.model_validate(payload)
                if delta.base_hash != records[index-1].state_hash or delta.state_hash != record.state_hash:
                    raise ValueError('Delta hashes differ from commit chain')
                if self.observation_archive == 'observe.v2':
                    expected = self._indexed_decision_links(initial, index, record, records[index-1], payload)
                    if expected != {obs.actor_id: obs.observation_id for obs in observations}:
                        raise ValueError('Indexed OBSERVE directory does not match the committed decisions')
            else:
                initial = decode_snapshot(payload)
                if initial.state_hash != record.state_hash:
                    raise ValueError('Initial snapshot hash mismatch')
        return records

    def states(self):
        records = self.verified_markers()
        state = None
        state_hash = None
        for record in records:
            payload = self.segment(record)
            if record.kind == 'initial':
                state = payload
                state_hash = content_hash(state)
            else:
                state_hash = _advance_state(state, payload, base_hash=state_hash)
            snapshot = decode_snapshot(state)
            if (snapshot.tick != record.tick or snapshot.world_version != record.world_version
                    or content_hash(snapshot) != record.state_hash):
                raise ValueError(f'Business state hash mismatch at tick {record.tick}')
            yield record, snapshot

    def load(self, tick=None, *, checkpoint_tail=False):
        from .checkpoint import validate_versions
        # Explicit recovery shortcut assumes earlier segments were already audited.
        if checkpoint_tail:
            validate_versions(self.identity.participants)
        records = self.markers() if checkpoint_tail else self.verified_markers()
        tick = records[-1].tick if tick is None else tick
        selected = next((r for r in records if r.tick==tick),None)
        if type(tick) is not int or selected is None:
            raise ValueError(f'No committed tick {tick}')
        checkpoint = self._select_checkpoint(records, tick)
        if checkpoint_tail and checkpoint is None:
            return self.load(tick)
        return selected, self._recover_snapshot(records, selected, checkpoint, verify_tail=checkpoint_tail)

    def load_state_chain(self, tick):
        """Verify the selected materialized state and its referenced state bytes."""
        from .checkpoint import validate_versions
        validate_versions(self.identity.participants)
        records = self.markers()
        selected = next((record for record in records if record.tick == tick), None)
        if type(tick) is not int or selected is None:
            raise ValueError(f'No committed tick {tick}')
        first = records[0]
        state = self.segment(first)
        state_hash = content_hash(state)
        if state_hash != first.state_hash:
            raise ValueError('Initial snapshot hash mismatch')
        for record in records[1:]:
            if record.tick > tick:
                break
            state_hash = _advance_state(state, self.segment(record), base_hash=state_hash,
                                        commit_state_hash=record.state_hash)
        snapshot = decode_snapshot(state)
        if (snapshot.tick != tick or snapshot.world_version != selected.world_version
                or snapshot.state_hash != selected.state_hash):
            raise ValueError('Recovered state does not match the selected commit')
        return selected, snapshot

    def _select_checkpoint(self, records, tick):
        from .checkpoint import Checkpoint

        by_tick = {r.tick:r for r in records}
        checkpoint = None
        for path in sorted(p for p in (self.root/'checkpoints').iterdir() if p.name.endswith(('.json','.json.gz'))):
            raw = read_json(path)
            candidate = Checkpoint.model_validate(raw)
            marker = by_tick.get(candidate.tick)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if (path.name not in (digest+'.json',digest+'.json.gz') or marker is None
                    or candidate.identity_hash!=self._identity_hash or candidate.commit_hash!=marker.commit_hash
                    or candidate.state_hash!=marker.state_hash or content_hash(candidate.snapshot)!=marker.state_hash
                    or candidate.participants!=self.identity.participants or candidate.rng!=self.identity.rng):
                raise ValueError('Invalid checkpoint reference, version or hash')
            if candidate.tick<=tick and (checkpoint is None or candidate.tick>checkpoint.tick):
                checkpoint = candidate
        return checkpoint

    def _recover_snapshot(self, records, selected, checkpoint, *, verify_tail):
        tick = selected.tick
        if checkpoint is None:
            state, start = self.segment(records[0]),records[0].tick
        else:
            state, start = checkpoint.model_dump(mode='json')['snapshot'],checkpoint.tick
        journal_prefix = (CanonicalListPrefix(('data', 'economic_runtime', 'journal'))
                          if start < tick and state.get('schema_version') == 4 else None)
        digest = journal_prefix.digest if journal_prefix is not None else content_hash
        state_hash = digest(state) if start < tick else None
        for record in records:
            if verify_tail and start<=record.tick<=tick:
                self.model_records(record)
                self.observation_rows(record)
            if start<record.tick<=tick:
                state_hash = _advance_state(state, self.segment(record), base_hash=state_hash,
                                            journal_prefix=journal_prefix)
        snapshot = decode_snapshot(state)
        if snapshot.tick!=tick or snapshot.world_version!=selected.world_version or content_hash(snapshot)!=selected.state_hash:
            raise ValueError('Recovered state does not match the selected commit')
        return snapshot

    def audit_history(self, *, checkpoint_tail=False):
        """Return the validation scope actually used for the reported head."""
        scope = None
        if checkpoint_tail:
            from .checkpoint import validate_versions

            validate_versions(self.identity.participants)
            records = self.markers()
            marker = records[-1]
            checkpoint = self._select_checkpoint(records, marker.tick)
            if checkpoint is not None:
                self._recover_snapshot(records, marker, checkpoint, verify_tail=True)
                scope = {'mode':'checkpoint_tail','checkpoint_tick':checkpoint.tick,
                         'states_verified':marker.tick-checkpoint.tick+1}
        if scope is None:
            for count, (marker, _) in enumerate(self.states(), 1):
                pass
            scope = {'mode':'full_state_audit','states_verified':count}
        return {**scope,'tick':marker.tick,'state_hash':marker.state_hash,'commit_hash':marker.commit_hash}

    def write_segment(self, payload):
        raw = stored_json_bytes(payload,compressed=True)
        return self._write_segment_bytes(raw, 'json+gzip')

    def _write_segment_bytes(self, raw, encoding):
        segment = Segment(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), encoding=encoding)
        path = self.segment_path(segment)
        if not path.exists():
            temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
            with temporary.open('xb') as handle:
                midpoint = len(raw)//2
                handle.write(raw[:midpoint]); handle.flush(); os.fsync(handle.fileno())
                self.fault('segment_write')
                handle.write(raw[midpoint:]); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        elif path.is_symlink() or path.read_bytes() != raw:
            raise ValueError('Immutable segment conflict')
        return segment

    def model_records(self, record):
        rows = self.read_segment(record.model_facts) if record.model_facts is not None else []
        if (not isinstance(rows,list) or sorted(r['attempt_id'] for r in rows) != list(record.invocation_receipt_refs)
                or any(content_hash(r['request']) != r['cache_key'] for r in rows)):
            raise ValueError('Missing or conflicting model receipts')
        return rows

    def archived_records(self):
        rows = {}
        for marker in self.markers():
            for row in self.model_records(marker):
                key = row['attempt_id']
                if key in rows and rows[key] != row:
                    raise ValueError('Conflicting immutable model fact')
                rows[key] = row
        return list(rows.values())

    @property
    def capture_observations(self):
        return self.observation_archive is not None

    def _write_indexed_observations(self, observations):
        stream = BytesIO()
        rows = sorted(observations, key=lambda row: row.actor_id)
        with ZipFile(stream, 'w') as pack:
            def write(name, value):
                pack.writestr(ZipInfo(name), json_bytes(value), compress_type=ZIP_DEFLATED, compresslevel=1)

            write('index.json', {'schema_version': 2,
                                 'block_size': OBSERVATION_BLOCK_SIZE,
                                 'decisions': {row.actor_id: row.observation_id for row in rows}})
            for offset in range(0, len(rows), OBSERVATION_BLOCK_SIZE):
                write(_observation_block(offset // OBSERVATION_BLOCK_SIZE),
                      [row.model_dump(mode='json') for row in rows[offset:offset + OBSERVATION_BLOCK_SIZE]])
        return self._write_segment_bytes(stream.getvalue(), 'observe+zip')

    def _write_observations_v1(self, observations):
        stream = BytesIO()
        with gzip.GzipFile(fileobj=stream, mode='wb', compresslevel=1, mtime=0) as archive:
            archive.write(b'[')
            for index, row in enumerate(sorted(observations, key=lambda row: row.actor_id)):
                if index:
                    archive.write(b',')
                archive.write(json_bytes(row.model_dump(mode='json'))[:-1])
            archive.write(b']\n')
        # GzipFile and gzip.compress can select different OS header bytes.
        header = gzip.compress(b'', compresslevel=1, mtime=0)[:10]
        return self._write_segment_bytes(header + stream.getvalue()[10:], 'json+gzip')

    @contextmanager
    def _indexed_observations(self, record):
        if (self.observation_archive != 'observe.v2' or record.kind != 'tick' or record.observations is None
                or record.observations.encoding != 'observe+zip'):
            raise ValueError('Invalid indexed OBSERVE reference')
        raw = self._segment_bytes(record.observations)
        try:
            with ZipFile(BytesIO(raw)) as pack:
                index = _canonical_json(pack.read('index.json'))
                if (not isinstance(index, dict) or set(index) not in (
                        {'schema_version', 'decisions'}, {'schema_version', 'decisions', 'block_size'})
                        or type(index['schema_version']) is not int or index['schema_version'] != 2
                        or not isinstance(index['decisions'], dict) or not index['decisions']
                        or any(not actor or type(obs) is not str for actor, obs in index['decisions'].items())):
                    raise ValueError('Invalid indexed OBSERVE directory')
                directory = index['decisions']
                block_size = index.get('block_size')
                if 'block_size' in index and (type(block_size) is not int or block_size != OBSERVATION_BLOCK_SIZE):
                    raise ValueError('Unsupported indexed OBSERVE block size')
                members = ([_observation_member(actor) for actor in directory] if block_size is None else
                           [_observation_block(i // block_size) for i in range(0, len(directory), block_size)])
                if pack.namelist() != ['index.json', *members]:
                    raise ValueError('Indexed OBSERVE members do not match the directory')
                yield pack, directory, block_size
        except (BadZipFile, KeyError, RuntimeError, NotImplementedError) as exc:
            raise ValueError('Invalid indexed OBSERVE archive') from exc

    @staticmethod
    def _observation_row(record, raw):
        obs = Observation.model_validate(raw)
        if (obs.visible_until.phase != 'OBSERVE' or obs.visible_until.tick != record.tick
                or obs.world_version != record.world_version-1 or obs.observation_version != record.world_version-1
                or obs.observation_id != f'observation:{obs.payload_hash}'):
            raise ValueError('Archived observation identity mismatch')
        return obs

    def _indexed_row(self, record, raw, actor, expected):
        row = self._observation_row(record, raw)
        if (row.actor_id, row.observation_id) != (actor, expected):
            raise ValueError('Indexed OBSERVE row does not match the committed decision')
        return row

    @staticmethod
    def _read_observation_block(pack, number, count):
        rows = _canonical_json(pack.read(_observation_block(number)))
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError('Indexed OBSERVE block size does not match the directory')
        return rows

    @staticmethod
    def _indexed_decision_links(initial, position, record, previous, raw):
        if (initial.schema_version != 4 or not isinstance(raw, dict)
                or type(raw.get('schema_version')) is not int or raw['schema_version'] != 1
                or raw.get('base_hash') != previous.state_hash or raw.get('state_hash') != record.state_hash
                or not isinstance(raw.get('changes'), list)):
            raise ValueError('Indexed OBSERVE decision delta does not match the commit')
        changes = [change for change in raw['changes'] if isinstance(change, dict)
                   and isinstance(change.get('path'), list) and change['path'][:1] in (['actors'], ['decisions'])]
        offset = len(initial.decisions) + (position-1)*len(initial.actors)
        if (len(changes) != 1 or changes[0]['path'] != ['decisions'] or changes[0].get('op') != 'append'
                or type(changes[0].get('offset')) is not int or changes[0]['offset'] != offset
                or not isinstance(changes[0].get('value'), list)):
            raise ValueError('Indexed OBSERVE requires the fixed actor decision append')
        rows = changes[0]['value']
        if (len(rows) != len(initial.actors) or any(
                not isinstance(row, dict) or type(row.get('tick')) is not int or row['tick'] != record.tick
                or type(row.get('actor_id')) is not str or type(row.get('observation_id')) is not str
                for row in rows)):
            raise ValueError('Indexed OBSERVE decisions do not cover the fixed actor roster')
        expected = {row['actor_id']: row['observation_id'] for row in rows}
        if set(expected) != set(initial.actors):
            raise ValueError('Indexed OBSERVE decisions do not cover the fixed actor roster')
        return expected

    def observation_decision_ids(self, records, record):
        """Read just the selected period's fixed-roster decision append."""
        from .checkpoint import validate_versions
        validate_versions(self.identity.participants)
        first = records[0]
        position = record.tick - first.tick
        if not 0 < position < len(records) or records[position] != record:
            raise ValueError('Indexed observation requires a committed observation period')
        initial = decode_snapshot(self.segment(first))
        if initial.state_hash != first.state_hash:
            raise ValueError('Invalid initial state for indexed OBSERVE')
        return self._indexed_decision_links(initial, position, record, records[position-1], self.segment(record))

    def indexed_observation_for(self, records, record, actor):
        """Check the selected cursor and actor, leaving whole-history audit separate."""
        expected = self.observation_decision_ids(records, record)
        if actor not in expected:
            raise ValueError('Actor is not registered at this commit')
        with self._indexed_observations(record) as (pack, directory, block_size):
            if expected != directory:
                raise ValueError('Indexed OBSERVE directory does not match the committed decisions')
            if block_size is None:
                raw = _canonical_json(pack.read(_observation_member(actor)))
            else:
                number, row = divmod(list(directory).index(actor), block_size)
                count = min(block_size, len(directory) - number * block_size)
                raw = self._read_observation_block(pack, number, count)[row]
            return self._indexed_row(record, raw, actor, expected[actor])

    def observation_rows(self, record):
        if record.observations is None:
            if self.capture_observations and record.kind == 'tick':
                raise ValueError('Missing committed OBSERVE archive')
            return ()
        if not self.capture_observations or record.kind != 'tick':
            raise ValueError('Unexpected observation archive')
        if self.observation_archive == 'observe.v2':
            with self._indexed_observations(record) as (pack, directory, block_size):
                if block_size is None:
                    return tuple(self._indexed_row(record, _canonical_json(pack.read(_observation_member(actor))),
                                                   actor, expected) for actor, expected in directory.items())
                items = list(directory.items())
                observations = []
                for offset in range(0, len(items), block_size):
                    block = items[offset:offset + block_size]
                    rows = self._read_observation_block(pack, offset // block_size, len(block))
                    observations.extend(self._indexed_row(record, raw, actor, expected)
                                        for raw, (actor, expected) in zip(rows, block, strict=True))
                return tuple(observations)
        raw = self.read_segment(record.observations)
        if not isinstance(raw, list) or not raw:
            raise ValueError('Invalid OBSERVE archive')
        rows = tuple(self._observation_row(record, row) for row in raw)
        actors = [obs.actor_id for obs in rows]
        if actors != sorted(set(actors)):
            raise ValueError('Archived observation identity mismatch')
        return rows

    def _publish(self, snapshot, payload, *, kind, parent, refs, state_hash=None, observations=()):
        segment = self.write_segment(payload)
        facts = None
        if refs:
            if self.receipt_reader is None:
                raise ValueError('Invocation references require a durable receipt source')
            rows = {r['attempt_id']:r for r in self.receipt_reader()}
            facts = self.write_segment([rows[ref] for ref in refs])
        observed = None
        if observations:
            if self.observation_archive == 'observe.v2':
                observed = self._write_indexed_observations(observations)
            else:
                observed = self._write_observations_v1(observations)
        body = dict(schema_version=1, kind=kind, tick=snapshot.tick, world_version=snapshot.world_version,
                    parent_commit_hash=parent, identity_hash=self._identity_hash,
                    state_hash=state_hash or snapshot.state_hash, segment=segment.model_dump(), model_facts=facts.model_dump() if facts else None,
                    invocation_receipt_refs=list(refs))
        if observed is not None:
            body['observations'] = observed.model_dump()
        record = Commit(**body, commit_hash=content_hash(body))
        marker = self.marker_path(snapshot.tick)
        if marker.exists():
            raise ValueError('Duplicate committed tick')
        self.fault('before_marker')
        atomic_json(marker, record.model_dump(mode='json'))
        self.fault('after_marker')
        self._trusted_head = record
        return record

    def commit(self, base, prepared, *, observations=()):
        with self.writer():
            terminal_tick = self.identity.metadata.get('terminal_tick')
            if terminal_tick is not None and prepared.tick > terminal_tick:
                raise ValueError('Cannot publish after the assigned terminal tick')
            candidate = self._read_marker(self._marker_paths()[-1])
            head = candidate if candidate == self._trusted_head else self.markers()[-1]
            if head.tick != base.tick or head.state_hash != base.state_hash:
                raise ValueError('Commit base differs from durable head')
            if self.capture_observations:
                expected = {d.actor_id:d.observation_id for d in prepared.snapshot.decisions if d.tick == prepared.tick}
                if len(observations) != len(prepared.snapshot.actors) or {
                        o.actor_id:o.observation_id for o in observations} != expected:
                    raise ValueError('Committed observation archive must match the decision inputs')
            elif observations:
                raise ValueError('Observation archive must be configured')
            delta = StateDelta(base_hash=head.state_hash, state_hash=prepared.state_hash,
                               changes=_snapshot_changes(base, prepared.snapshot))
            return self._publish(prepared.snapshot, delta.model_dump(mode='json'), kind='tick',
                                 parent=head.commit_hash, refs=prepared.invocation_receipt_refs,
                                 state_hash=prepared.state_hash, observations=observations)

    def is_committed(self, prepared):
        path = self.marker_path(prepared.tick)
        if not path.exists():
            return False
        record = self._read_marker(path)
        self.segment(record)
        self.model_records(record)
        self.observation_rows(record)
        if record.state_hash != prepared.state_hash:
            raise ValueError('Committed tick conflicts with prepared state')
        return True

    def _update_index(self, *, verify_prefix=False):
        path = self.root/'index.sqlite'
        if path.is_symlink():
            raise sqlite3.DatabaseError('Index cannot be a symbolic link')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS commits (tick INTEGER PRIMARY KEY, commit_hash TEXT, state_hash TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS progress
                       (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), identity_hash TEXT,
                        first_tick INTEGER, last_tick INTEGER, last_hash TEXT)''')
            paths = self._marker_paths()
            first_tick = int(paths[0].stem)
            identity = self._identity_hash
            saved = db.execute('SELECT identity_hash,first_tick,last_tick,last_hash FROM progress').fetchone()
            prior = None
            if saved is not None:
                old_identity, start, tail, last_hash = saved
                count, low, high = db.execute('SELECT COUNT(*),MIN(tick),MAX(tick) FROM commits').fetchone()
                if (isinstance(tail, int) and start == first_tick and old_identity == identity
                        and first_tick <= tail <= int(paths[-1].stem) and (count, low, high) == (
                            tail-first_tick+1, first_tick, tail)):
                    marker = self._read_marker(self.marker_path(tail))
                    indexed = db.execute('SELECT commit_hash,state_hash FROM commits WHERE tick=?', (tail,)).fetchone()
                    if last_hash == marker.commit_hash and indexed == (marker.commit_hash, marker.state_hash):
                        prior = marker
            if prior is not None and verify_prefix:
                indexed = db.execute('SELECT tick,commit_hash,state_hash FROM commits ORDER BY tick').fetchall()
                expected = [(r.tick,r.commit_hash,r.state_hash) for r in self.markers() if r.tick<=prior.tick]
                if indexed != expected:
                    prior = None
            if prior is None:
                added = self.markers()
                db.execute('DELETE FROM commits')
                mode = 'rebuilt'
            else:
                added = []
                previous = prior
                for path in paths[prior.tick-first_tick+1:]:
                    marker = self._read_marker(path)
                    if (marker.kind != 'tick' or marker.parent_commit_hash != previous.commit_hash
                            or marker.world_version != previous.world_version+1):
                        raise ValueError(f'Broken commit chain at tick {marker.tick}')
                    added.append(marker)
                    previous = marker
                mode = 'incremental'
            head = added[-1] if added else prior
            if added:
                db.executemany('INSERT INTO commits VALUES (?,?,?)',
                               [(r.tick,r.commit_hash,r.state_hash) for r in added])
                db.execute('INSERT OR REPLACE INTO progress VALUES (1,?,?,?,?)',
                           (identity, first_tick, head.tick, head.commit_hash))
        return {'mode':mode, 'appended':len(added), 'tick':head.tick, 'commit_hash':head.commit_hash}

    def update_index(self, *, verify_prefix=False):
        self.fault('before_index')
        with self.writer():
            report = self._update_index(verify_prefix=verify_prefix)
        self.fault('after_index')
        return report

    def orphans(self):
        used = {self.segment_path(s).name for r in self.markers() for s in (r.segment,r.model_facts,r.observations) if s is not None}
        return sorted(str(p.relative_to(self.root)) for folder in ('segments','commits','checkpoints') for p in (self.root/folder).iterdir()
                      if p.is_file() and (p.name.endswith('.tmp') or (folder=='segments' and p.name not in used)))

    def repair_index(self):
        with self.writer():
            record, _ = self.load()
            path = self.root/'index.sqlite'
            if path.exists() or path.is_symlink():
                os.replace(path, self.root/'diagnostics'/('index-'+uuid.uuid4().hex+'.sqlite'))
            self._update_index()
            report = {'tick':record.tick,'commit_hash':record.commit_hash,'orphans':self.orphans()}
            atomic_json(self.root/'diagnostics'/('recovery-'+uuid.uuid4().hex+'.json'), report)
            return report
