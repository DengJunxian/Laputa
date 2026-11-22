"""Resolve only retained evidence from the caller's validated visibility boundary."""
from civitas.contracts.base import content_hash
from civitas.contracts.observations import Observation


def with_evidence(observation, **views):
    payload = {**observation.payload(), **views}
    digest = content_hash(payload)
    return Observation.model_validate({**payload, 'observation_id': f'observation:{digest}', 'payload_hash': digest})


def retrieve(observation, memory, *, limit=12):
    memory.validate_for(observation)
    current = Observation.model_validate(observation.model_dump())
    visible = {f.fact_id: f for f in current.private_memory_refs if f.value}
    visible.update({m.message_id: m for m in current.delivered_messages if m.body})
    authorized = {(key, content_hash(value)) for key, value in visible.items()}
    episodes = sorted(
        (e for e in memory.episodic if (e.evidence_id, e.content_hash) in authorized),
        key=lambda e: (-e.seen_at, e.evidence_id, e.content_hash))[:limit]
    versions = {e.evidence_id: e.content_hash for e in episodes}
    notes = {key: [note for note in getattr(memory, key) if note.evidence_ids
                   and note.evidence_versions == {k: versions.get(k) for k in note.evidence_ids}]
             for key in ('semantic', 'procedural')}
    return memory.model_copy(update={'working': {}, 'episodic': episodes, **notes})
