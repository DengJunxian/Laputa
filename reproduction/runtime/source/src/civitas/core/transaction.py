from civitas.contracts.codecs import codec_for_version
from civitas.contracts.state import APPEND_ONLY_WORLD_RECORDS, EconomicWorldSnapshot, PreparedTick, TickContext, WorldSnapshot


_HISTORY_ROWS = frozenset({"events", "decisions", "receipts", "messages", "memories"})


class TickTransaction:
    """Logical deep snapshot: no working state is published before prepare passes."""

    def __init__(self, base: WorldSnapshot, participants=()):
        self.base = base
        self._history = ({key: getattr(base, key) for key in APPEND_ONLY_WORLD_RECORDS}
                         if type(base) is EconomicWorldSnapshot else {})
        self.working = base.working_dump(mode="json", exclude=_HISTORY_ROWS)
        self.working.update({key: [] if key in self._history else list(getattr(base, key)) for key in _HISTORY_ROWS})
        self.working["tick"] += 1
        self._used = set(self.working['used_ids'])
        self.participants = tuple(participants)
        self.before = {}
        for participant in self.participants:
            key = participant.participant_id
            matches_committed = getattr(participant, "matches_committed_state", None)
            if matches_committed is not None and matches_committed(base.data[key]):
                self.before[key] = base.data[key]
            else:
                state = participant.export_state()
                if state != self.working["data"][key]:
                    raise RuntimeError("Participant changed outside a tick transaction")
                self.before[key] = state

    def context(self, fields=None) -> TickContext:
        value = {key: self.working[key] for key in TickContext.model_fields}
        if fields is not None:
            value["data"] = {key: data for key, data in value["data"].items() if key in fields}
        return TickContext(**value)

    def snapshot(self, *, data=None, projection=False, memory_window=None, history_fields=None) -> WorldSnapshot:
        value = self.working
        if projection or history_fields is not None:
            # Read barriers need current inputs; the commit still validates the entire history.
            history = ('events', 'messages', 'memories') if history_fields is None else history_fields
            value = {key: value[key] for key in (*TickContext.model_fields, 'schema_version', *history)}
            value = self._snapshot_history(value)
            if memory_window is not None:
                if type(memory_window) is not int or memory_window<1:
                    raise ValueError('Observation history window must be positive')
                beliefs, recent = {}, []
                for fact in value['memories']:
                    if fact.visible_at.tick > value['tick']:
                        continue
                    if fact.fact_id.startswith('belief:'):
                        beliefs[fact.owner_id] = fact
                    elif fact.visible_at.tick > value['tick']-memory_window:
                        recent.append(fact)
                value['memories'] = [*recent,*beliefs.values()]
        else:
            value = self._snapshot_history(value)
        if data is not None:
            value = {**value,'data':data}
        return codec_for_version(self.base.schema_version).snapshot.model_validate(value)

    def _snapshot_history(self, value):
        if not self._history:
            return value
        payload = dict(value)
        for field, prefix in self._history.items():
            if field in payload:
                tail = self.working[field]
                sealed = prefix.appended(tail)
                self._history[field] = payload[field] = sealed
                tail.clear()
        return payload

    def capture_participants(self) -> None:
        for participant in self.participants:
            participant.validate_state()
            snapshot_state = getattr(participant, "snapshot_state", participant.export_state)
            self.working["data"][participant.participant_id] = snapshot_state()

    def rollback(self) -> None:
        for participant in reversed(self.participants):
            participant.import_state(self.before[participant.participant_id])

    def set_data(self, data: dict, *, fields=None) -> None:
        validated = TickContext(**{key: data if key == "data" else self.working[key] for key in TickContext.model_fields})
        values = validated.working_dump()["data"]
        if fields is None:
            self.working["data"] = values
        else:
            self.working["data"] = {**{k: v for k, v in self.working["data"].items() if k not in fields}, **values}

    def claim(self, *identifiers: str) -> None:
        used = self.working["used_ids"]
        for identifier in identifiers:
            if identifier in self._used:
                raise ValueError(f"Duplicate world ID: {identifier}")
            used.append(identifier)
            self._used.add(identifier)

    def prepare(self, invocation_refs: list[str]) -> PreparedTick:
        codec = codec_for_version(self.base.schema_version)
        payload = self._snapshot_history(self.working)
        snapshot = codec.snapshot.model_validate({**payload,'world_version':self.base.world_version+1})
        return codec.prepared(base_version=self.base.world_version, tick=snapshot.tick, snapshot=snapshot,
                              state_hash=snapshot.state_hash, invocation_receipt_refs=invocation_refs)
