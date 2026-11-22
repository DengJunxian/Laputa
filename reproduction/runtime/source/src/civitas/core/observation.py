from civitas.contracts.observations import Observation, ObservationFact
from civitas.contracts.state import WorldSnapshot
from civitas.contracts.time import SimTime


class ObservationBuilder:
    """The sole time/permission filter; reading does not mutate state or RNG."""

    effect_spec = None

    def select_messages(self, actor_id, snapshot, messages):
        return messages

    def select_memories(self, actor_id, snapshot):
        return snapshot.memories

    def message_candidates(self, actor_id, snapshot):
        return snapshot.messages

    def observe(self, actor_id: str, snapshot: WorldSnapshot, candidates: list[ObservationFact]) -> Observation:
        if actor_id not in snapshot.actors:
            raise ValueError("Unknown observation actor")
        now = SimTime(tick=snapshot.tick, phase="OBSERVE")
        views = {"account": [], "market": [], "memory": []}
        # Snapshot facts are already validated and deeply immutable.
        facts = (value if type(value) is ObservationFact else ObservationFact.model_validate(value)
                 for value in candidates)
        for fact in [*facts, *self.select_memories(actor_id,snapshot)]:
            allowed = fact.owner_id == actor_id or (fact.category == "market" and fact.public)
            if allowed and fact.visible_at.key <= now.key:
                views[fact.category].append(fact)
        messages = [m for m in self.message_candidates(actor_id, snapshot) if m.recipient_id == actor_id
                    and m.visible_at.key <= now.key and (m.expires_at is None or now.key < m.expires_at.key)]
        messages = self.select_messages(actor_id, snapshot, messages)
        return Observation.from_views(schema_version=1, actor_id=actor_id,
                       batch_id=f"tick:{snapshot.tick}:base:{snapshot.world_version}",
                       observation_version=snapshot.world_version, world_version=snapshot.world_version,
                       visible_until=now,
                       account_view=sorted(views["account"], key=lambda f: f.fact_id),
                       market_view=sorted(views["market"], key=lambda f: f.fact_id),
                       private_memory_refs=sorted(views["memory"], key=lambda f: f.fact_id),
                       delivered_messages=sorted(messages, key=lambda m: m.message_id))
