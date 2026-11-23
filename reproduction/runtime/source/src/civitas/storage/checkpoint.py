"""Complete private checkpoints; callers must project observations for agents."""
from typing import Literal
from types import MappingProxyType

from civitas.contracts.base import Digest, NonNegative, Record, content_hash
from civitas.contracts.codecs import decode_snapshot
from .commit_store import atomic_json, stored_json_bytes, read_json
import hashlib


class Checkpoint(Record):
    schema_version: Literal[1] = 1
    tick: NonNegative
    commit_hash: Digest
    identity_hash: Digest
    state_hash: Digest
    snapshot: dict
    participants: dict[str, int]
    rng: dict


# Explicit identities from the existing participant contracts; data never names
# Python imports. In particular a v4/core-only recovery must not import legacy
# market scenarios, pandas, NumPy or NetworkX just to inspect this whitelist.
PARTICIPANT_VERSIONS = MappingProxyType({
    'cognition_memory': (1,),
    'legacy_settlement': (1,),
    'multiasset_market': (3,),
    'portfolio_ledger': (3,),
    'legacy_society': (1,),
    'society_traces': (1,),
    'economic_runtime': (1, 2, 3),
})


def validate_versions(versions):
    if any(type(version) is not int or version not in PARTICIPANT_VERSIONS.get(key, ())
           for key, version in versions.items()):
        raise ValueError('Participant versions are unknown or unsupported; register an explicit version/migration')


def participant_versions(world):
    versions = {}
    for participant in world.participants:
        if (not all(callable(getattr(participant, method, None)) for method in ('export_state','import_state','validate_state'))
                or type(getattr(participant, 'state_schema_version', None)) is not int):
            raise ValueError('Stateful participant lacks serialization/version contract')
        participant.validate_state()
        versions[participant.participant_id] = participant.state_schema_version
    for plugin in (world.scenario, world.policy, world.compiler, world.observer, *world.registry.mechanisms):
        if getattr(plugin, 'stateful', False) and plugin not in world.participants:
            raise ValueError('Stateful plugin must be a registered transaction participant')
    validate_versions(versions)
    return versions


def attach(world, store):
    if (participant_versions(world) != store.identity.participants or world.rng._root.model_dump(mode='json') != store.identity.rng
            or store.markers()[-1].state_hash != content_hash(world.snapshot)):
        raise ValueError('World does not match the durable checkpoint identity')
    world.commit_store = store


def restore(world, store, *, tick=None):
    if participant_versions(world) != store.identity.participants or world.rng._root.model_dump(mode='json') != store.identity.rng:
        raise ValueError('Participant versions or RNG identity mismatch')
    record, snapshot = store.load(tick)
    world.restore_snapshot(snapshot)
    if record.commit_hash == store.markers()[-1].commit_hash:
        attach(world, store)
    return record


def save(store, *, tick=None):
    record, snapshot = store.load(tick)
    return _save_verified(store, record, snapshot)


def _save_verified(store, record, snapshot):
    checkpoint = Checkpoint(tick=record.tick,commit_hash=record.commit_hash,identity_hash=content_hash(store.identity),
        state_hash=record.state_hash,snapshot=snapshot.model_dump(mode='json'),
        participants=store.identity.participants,rng=store.identity.rng)
    raw = stored_json_bytes(checkpoint.model_dump(mode='json'),compressed=True)
    digest = hashlib.sha256(raw).hexdigest()
    path = store.root/'checkpoints'/(digest+'.json.gz')
    if not path.exists():
        atomic_json(path, checkpoint.model_dump(mode='json'))
    elif path.read_bytes() != raw:
        raise ValueError('Immutable checkpoint conflict')
    return {'path':str(path),'sha256':digest,'bytes':len(raw),'encoding':'json+gzip','tick':record.tick,'commit_hash':record.commit_hash}


def load(store, path):
    checkpoint = Checkpoint.model_validate(read_json(path))
    record, snapshot = store.load(checkpoint.tick)
    if (checkpoint.identity_hash != content_hash(store.identity) or checkpoint.commit_hash != record.commit_hash
            or checkpoint.participants != store.identity.participants or checkpoint.rng != store.identity.rng
            or checkpoint.state_hash != record.state_hash or content_hash(checkpoint.snapshot) != record.state_hash):
        raise ValueError('Checkpoint differs from authoritative commit')
    return decode_snapshot(checkpoint.snapshot)
