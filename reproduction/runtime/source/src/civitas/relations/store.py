from civitas.contracts.base import NonNegative
from pydantic import TypeAdapter

from .types import EdgeDelta, RelationState


class RelationStore:
    """Pure view: proposed states become visible only when WorldKernel commits them."""

    def __init__(self, state: RelationState):
        # Exact typed states are already validated and recursively immutable.
        state = state if type(state) is RelationState else RelationState.model_validate(state)
        self.state = state
        self._out = {actor: set() for actor in state.actors}
        self._trust = {(e.source_id, e.target_id): e for e in state.fact_trust}
        for edge in state.communication:
            self._out[edge.source_id].add(edge.target_id)

    def _actor(self, actor):
        if actor not in self._out:
            raise ValueError(f"Unknown relation actor: {actor}")

    def neighbors(self, actor: str) -> tuple[str, ...]:
        self._actor(actor)
        return tuple(sorted(self._out[actor]))

    def can_deliver(self, author: str, recipient: str) -> bool:
        self._actor(author)
        self._actor(recipient)
        return recipient in self._out[author]

    def trust_weight(self, observer: str, author: str) -> float:
        self._actor(observer)
        self._actor(author)
        trust = self._trust.get((observer, author))
        if not self.state.config.trust_enabled:
            return 1.0
        return trust.a / (trust.a + trust.b) if trust else 0.5

    def propose(self, deltas: list[EdgeDelta], *, tick: int) -> RelationState:
        TypeAdapter(NonNegative).validate_python(tick, strict=True)
        if tick != self.state.tick:
            raise ValueError("Proposal tick must match active state")
        known = {d.delta_id: d for d in self.state.pending + self.state.applied}
        pending = list(self.state.pending)
        for raw in deltas:
            delta = EdgeDelta.model_validate(raw)
            if delta.delta_id in known:
                if delta != known[delta.delta_id]:
                    raise ValueError("Conflicting delta ID")
                continue
            if delta.proposed_tick != tick:
                raise ValueError("Delta proposal time mismatch")
            pending.append(delta)
            known[delta.delta_id] = delta
        return self.state.model_copy(update={"pending": sorted(pending, key=lambda d: (d.effective_tick, d.delta_id))})

    def advance(self, tick: int) -> RelationState:
        TypeAdapter(NonNegative).validate_python(tick, strict=True)
        if tick == self.state.tick:
            return self.state
        if tick != self.state.tick + 1:
            raise ValueError("Relation boundary must advance one tick")
        collections = {name: {(e.source_id, e.target_id): e for e in getattr(self.state, name)}
                       for name in ("communication", "fact_trust", "prediction_trust", "memberships")}
        pending, applied = [], list(self.state.applied)
        for delta in sorted(self.state.pending, key=lambda d: (d.effective_tick, d.delta_id)):
            if delta.effective_tick > tick:
                pending.append(delta)
                continue
            table = collections["memberships" if delta.layer == "membership" else delta.layer]
            key = (delta.source_id, delta.target_id)
            if delta.operation == "delete":
                table.pop(key, None)
            else:
                table[key] = delta.value
            applied.append(delta)
        return self.state.model_copy(update={"tick": tick, "pending": pending, "applied": applied,
            **{name: [edges[key] for key in sorted(edges)] for name, edges in collections.items()}})
