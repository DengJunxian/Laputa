"""Private candidate state participates in the existing whole-tick transaction."""
from typing import Any, Literal

from pydantic import Field

from civitas.contracts.base import Digest, Id, NonNegative, Record, content_hash, require_unique
from civitas.contracts.observations import Observation


class MemoryEpisode(Record):
    evidence_id: Id
    observation_id: Id
    content_hash: Digest
    seen_at: NonNegative
    source: Literal['visible_fact', 'message', 'receipt']


class MemoryNote(Record):
    text: Id
    evidence_ids: list[Id]
    evidence_versions: dict[Id, Digest] | None = Field(default=None, exclude_if=lambda value: value is None)


class AgentMemory(Record):
    actor_id: Id
    version: NonNegative = 0
    working: dict[str, Any] = Field(default_factory=dict)
    episodic: list[MemoryEpisode] = Field(default_factory=list)
    semantic: list[MemoryNote] = Field(default_factory=list)
    procedural: list[MemoryNote] = Field(default_factory=list)

    def validate_for(self, observation):
        if self.actor_id != observation.actor_id:
            raise ValueError('Cross-actor memory access')
        tick = observation.visible_until.tick
        stamps = [e.seen_at for e in self.episodic]
        stamps.extend(self.working[k] for k in ('observed_tick', 'last_slow_tick') if k in self.working)
        if self.working.get('plan'):
            stamps.append(self.working['plan'].get('as_of_tick', tick))
        if any(type(t) is not int or not 0 <= t <= tick for t in stamps):
            raise ValueError('Private memory contains an invalid or future timestamp')
        return self


class MemoryState(Record):
    schema_version: Literal[1] = 1
    actors: list[AgentMemory]


class MemoryBook:
    participant_id = 'cognition_memory'
    state_schema_version = 1

    def __init__(self, actors):
        require_unique(actors, 'memory actor')
        self._actors = {actor: AgentMemory(actor_id=actor) for actor in sorted(actors)}

    def for_actor(self, actor_id):
        return self._actors[actor_id]

    def accept(self, observation: Observation, *, reflection=None, working=None, pin_reflection_versions=False):
        observation = Observation.model_validate(observation)
        old = self.for_actor(observation.actor_id)
        if reflection is not None:
            reflection.validate_for(observation)
        episodes = {(e.evidence_id, e.content_hash): e for e in old.episodic}
        for fact in observation.market_view + observation.private_memory_refs:
            digest = content_hash(fact)
            episodes.setdefault((fact.fact_id, digest), MemoryEpisode(evidence_id=fact.fact_id,
                observation_id=observation.observation_id, content_hash=digest, seen_at=observation.visible_until.tick,
                source='receipt' if 'command_id' in fact.value and 'status' in fact.value else 'visible_fact'))
        for message in observation.delivered_messages:
            digest = content_hash(message)
            episodes.setdefault((message.message_id, digest), MemoryEpisode(evidence_id=message.message_id,
                observation_id=observation.observation_id, content_hash=digest,
                seen_at=observation.visible_until.tick, source='message'))
        notes = {key: list(getattr(old, key)) for key in ('semantic', 'procedural')}
        if reflection:
            versions = None
            if pin_reflection_versions:
                versions = {fact.fact_id: content_hash(fact)
                            for fact in observation.account_view + observation.market_view + observation.private_memory_refs}
                versions.update({message.message_id: content_hash(message) for message in observation.delivered_messages})
                versions = {key: versions[key] for key in reflection.evidence_ids}
            for key in notes:
                for text in getattr(reflection, key):
                    note = MemoryNote(text=text, evidence_ids=reflection.evidence_ids, evidence_versions=versions)
                    if note not in notes[key]:
                        notes[key].append(note)
        state = {'observation_id': observation.observation_id, 'evidence_ids': sorted(observation.evidence_ids),
                 **(old.model_dump()['working'] if working is None else working)}
        state['observation_id'] = observation.observation_id
        state['evidence_ids'] = sorted(observation.evidence_ids)
        state['observed_tick'] = observation.visible_until.tick
        candidate = old.model_copy(update={'working': state, 'episodic': list(episodes.values()), **notes})
        if candidate != old:
            candidate = candidate.model_copy(update={'version': old.version + 1})
        self._actors[observation.actor_id] = candidate
        return candidate

    def export_state(self):
        return MemoryState(actors=[self._actors[a] for a in sorted(self._actors)]).model_dump(mode='json')

    def import_state(self, state):
        parsed = MemoryState.model_validate(state)
        require_unique([m.actor_id for m in parsed.actors], 'memory actor')
        if set(m.actor_id for m in parsed.actors) != set(self._actors):
            raise ValueError('Memory restore changed actor membership')
        self._actors = {m.actor_id: m for m in parsed.actors}

    def validate_state(self):
        self.import_state(self.export_state())
