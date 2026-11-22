from collections.abc import Callable

from civitas.contracts.base import Positive, content_hash
from civitas.contracts.codecs import codec_for_version, decode_command, decode_snapshot
from civitas.contracts.config import MechanismSpec
from civitas.contracts.decisions import Command
from civitas.contracts.events import Event
from civitas.contracts.observations import Message, ObservationFact
from civitas.contracts.ports import ActionCompiler, ObservationBuilder, Policy, Scenario, CommitStore
from civitas.contracts.receipts import ModelCallReceipt
from civitas.contracts.state import BatchExecutionResult, Feedback, RelationDelta, TickContext, WorldSnapshot
from civitas.contracts.time import Phase, SimTime
from pydantic import TypeAdapter

from .registry import MechanismRegistry
from .rng import AddressedRNG
from .scheduler import command_order, decide_batch
from .transaction import TickTransaction
from .messages import compile_messages


class WorldKernel:
    def __init__(self, *, scenario: Scenario, policy: Policy, compiler: ActionCompiler,
                 observer: ObservationBuilder, registry: MechanismRegistry, rng: AddressedRNG,
                 actors: list[str], events: list[Event], concurrency: int = 1,
                 phase_hook: Callable[[Phase, WorldSnapshot], None] | None = None,
                 participants=(), schema_version: int = 1, arrival_key=None):
        self.scenario, self.policy, self.compiler = scenario, policy, compiler
        self.observer, self.registry, self.rng = observer, registry, rng
        self.concurrency = TypeAdapter(Positive).validate_python(concurrency, strict=True)
        self.participants = tuple(participants)
        effect_spec = getattr(observer, "effect_spec", None)
        self.observation_effect = MechanismSpec.model_validate(effect_spec) if effect_spec is not None else None
        if self.observation_effect is not None:
            protected = set(scenario.owned_fields) | {p.participant_id for p in self.participants}
            protected.update(field for m in registry.mechanisms for field in m.spec.writes)
            if self.observation_effect.mechanism_id in {m.spec.mechanism_id for m in registry.mechanisms}:
                raise ValueError("Duplicate observation effect mechanism identity")
            if self.observation_effect.phase != "FEEDBACK" or set(self.observation_effect.writes) & protected:
                raise ValueError("Observation effects require exclusive FEEDBACK field ownership")
        data = scenario.initialize()
        identifiers = [p.participant_id for p in self.participants]
        if len(set(identifiers)) != len(identifiers) or set(identifiers) & set(data):
            raise ValueError("Participant state keys must be unique and separate from scenario fields")
        for participant in self.participants:
            participant.validate_state()
            data[participant.participant_id] = participant.export_state()
        self._snapshot = codec_for_version(schema_version).snapshot.model_validate(dict(
            schema_version=schema_version, tick=0, world_version=0, execution_seq=0,
            actors=sorted(actors), data=data, pending_events=events))
        self.scenario.validate(self._context(self._snapshot))
        self.phase_hook = phase_hook
        self.phase = None
        self._stepping = False
        self._invocations: list[ModelCallReceipt] = []
        self._failures: list[dict] = []
        self.last_prepared = None
        self.arrival_key = arrival_key
        self.commit_store: CommitStore | None = None
        self.scheduled_events = ()

    def restore_snapshot(self, snapshot):
        """Import a fully verified checkpoint at a quiescent boundary."""
        if self._stepping:
            raise RuntimeError('Cannot restore a world during a tick')
        value = decode_snapshot(snapshot.model_dump(mode='json'))
        if value.schema_version != self._snapshot.schema_version:
            raise ValueError('Checkpoint world schema differs; use an explicit migration')
        if value.actors != self._snapshot.actors:
            raise ValueError('Checkpoint actor identities differ')
        if validate_snapshot := getattr(self.scenario, 'validate_snapshot', None):
            validate_snapshot(value)
        before = {p.participant_id:p.export_state() for p in self.participants}
        data = value.model_dump(mode='json')['data']
        try:
            for participant in self.participants:
                participant.import_state(data[participant.participant_id])
                participant.validate_state()
                if content_hash(participant.export_state()) != content_hash(data[participant.participant_id]):
                    raise ValueError('Participant did not preserve the imported checkpoint')
            self.scenario.validate(self._context(value))
        except BaseException:
            for participant in reversed(self.participants):
                participant.import_state(before[participant.participant_id])
            raise
        self._snapshot = value
        self.last_prepared = None

    @property
    def snapshot(self) -> WorldSnapshot:
        return self._snapshot

    @property
    def invocations(self) -> tuple[ModelCallReceipt, ...]:
        return tuple(self._invocations)

    @property
    def failures(self) -> tuple[dict, ...]:
        return tuple(dict(record) for record in self._failures)

    def record_invocation(self, receipt: ModelCallReceipt) -> None:
        fact = ModelCallReceipt.model_validate(receipt)
        if any(r.attempt_id == fact.attempt_id for r in self._invocations):
            raise ValueError("Duplicate invocation attempt ID")
        self._invocations.append(fact)

    @staticmethod
    def _context(snapshot: WorldSnapshot) -> TickContext:
        return TickContext(**{key:getattr(snapshot,key) for key in TickContext.model_fields})

    def observe(self, actor_id: str):
        context = self._context(self._snapshot)
        return self.observer.observe(actor_id, self._snapshot, self.scenario.project_observation(context, actor_id))

    def _checkpoint(self, phase: Phase, tx: TickTransaction) -> None:
        self.phase = phase
        if self.phase_hook:
            self.phase_hook(phase, tx.snapshot())

    def _scenario_data(self, tx: TickTransaction, data: dict) -> None:
        fields = getattr(self.scenario, "context_fields", None)
        before = tx.working["data"]
        if fields is not None:
            if not data.keys() <= fields:
                raise ValueError("Scenario wrote outside its declared context")
            before = {key: value for key, value in before.items() if key in fields}
        for field in before.keys() | data.keys():
            if field not in self.scenario.owned_fields and (field not in before or field not in data or before[field] != data[field]):
                raise ValueError(f"Scenario wrote an undeclared field: {field}")
        tx.set_data(data, fields=fields)

    async def run(self, ticks: int) -> WorldSnapshot:
        TypeAdapter(Positive).validate_python(ticks, strict=True)
        for _ in range(ticks):
            await self.step()
        return self._snapshot

    async def step(self):
        if self._stepping:
            raise RuntimeError("Only one tick writer is allowed")
        self._stepping = True
        tx = None
        prepared = None
        try:
            tx = TickTransaction(self._snapshot, self.participants)
            scenario_fields = getattr(self.scenario, "context_fields", None)
            self.phase = Phase.BOUNDARY
            now = SimTime(tick=tx.working["tick"], phase="BOUNDARY")
            pending = []
            for raw in tx.working["pending_relations"]:
                if RelationDelta.model_validate(raw).effective_at.key <= now.key:
                    tx.working["relations"][raw["relation_id"]] = raw
                else:
                    pending.append(raw)
            tx.working["pending_relations"] = pending
            self._scenario_data(tx, self.scenario.boundary(tx.context(scenario_fields)))
            if self.registry.mechanisms:
                tx.set_data(self.registry.apply("BOUNDARY", tx.context()))
            self._checkpoint(Phase.BOUNDARY, tx)

            self.phase = Phase.EVENTS
            tx.working['pending_events'].extend(Event.model_validate(e).model_dump(mode='json')
                for e in self.scheduled_events if e.tick == now.tick)
            for event in sorted((Event.model_validate(e) for e in tx.working['pending_events']), key=lambda e: (e.tick, e.event_id)):
                if event.tick == now.tick:
                    tx.claim(event.event_id)
                    self._scenario_data(tx, self.scenario.apply_event(tx.context(scenario_fields), event))
                    tx.working["events"].append(event)
            tx.working["pending_events"] = [e for e in tx.working["pending_events"] if e["tick"] > now.tick]
            if self.registry.mechanisms:
                tx.set_data(self.registry.apply("EVENTS", tx.context()))
            self._checkpoint(Phase.EVENTS, tx)

            self.phase = Phase.OBSERVE
            self._scenario_data(tx, await self.scenario.prepare_observations(tx.context(scenario_fields)))
            tx.capture_participants()
            compact = getattr(self.observer,'compact_snapshots',False)
            observation_fields = getattr(self.observer, "observation_data_fields", None)
            observation_data = (None if observation_fields is None else
                                {k: v for k, v in tx.working["data"].items() if k in observation_fields})
            barrier = tx.snapshot(projection=compact, memory_window=getattr(self.observer,'window',None) if compact else None,
                                  data=observation_data,
                                  history_fields=getattr(self.observer, "observation_history_fields", None))
            context = tx.context(scenario_fields)

            def project(actor):
                return self.scenario.project_observation(context, actor)

            observe_batch = getattr(self.observer, "observe_batch", None)
            observations = (observe_batch(barrier, project) if observe_batch is not None else
                            [self.observer.observe(actor, barrier, project(actor)) for actor in barrier.actors])
            self._checkpoint(Phase.OBSERVE, tx)
            self.phase = Phase.DECIDE
            bundles = await decide_batch(self.policy, observations, self.concurrency)
            self._checkpoint(Phase.DECIDE, tx)

            self.phase = Phase.VALIDATE
            commands = []
            for observation, bundle in zip(observations, bundles, strict=True):
                if self._snapshot.schema_version == 4 and (bundle.schema_version != 4 or bundle.tick != now.tick):
                    raise ValueError("Economic decision schema/tick differs from the current world")
                for field in ("actor_id", "observation_id", "batch_id", "observation_version", "world_version"):
                    if getattr(bundle, field) != getattr(observation, field):
                        raise ValueError(f"Decision disagrees with observation: {field}")
                if (bundle.policy_id, bundle.policy_version) != (self.policy.policy_id, self.policy.version):
                    raise ValueError("Decision policy identity mismatch")
                if not set(bundle.evidence_ids) <= observation.evidence_ids:
                    raise ValueError("Decision cites unavailable evidence")
                if any(m.recipient_id not in barrier.actors for m in bundle.message_drafts):
                    raise ValueError("Unknown message recipient")
                tx.claim(bundle.decision_id, *(i.intent_id for i in bundle.economic_intents),
                         *(m.message_id for m in bundle.message_drafts))
                arrival = (self.arrival_key(bundle.actor_id) if self.arrival_key else
                           self.rng.u64("scheduler.arrival", bundle.actor_id, now.tick, "decision"))
                compiled = [decode_command(value)
                            for value in self.compiler.compile(bundle, arrival)]
                if len(compiled) != len(bundle.economic_intents):
                    raise ValueError("Compiler must emit one command per intent")
                for index, command in enumerate(compiled):
                    if self._snapshot.schema_version == 4 and (command.schema_version != 4 or any(
                            getattr(command, field) != getattr(bundle, field)
                            for field in ("tick", "batch_id", "observation_version", "world_version"))):
                        raise ValueError("Economic command changed its observation/time context")
                    if (command.actor_id != bundle.actor_id or command.decision_id != bundle.decision_id
                            or command.local_order_index != index or command.arrival_key != arrival
                            or command.intent != bundle.economic_intents[index]):
                        raise ValueError("Compiler changed intent identity/order")
                    tx.claim(command.command_id)
                commands.extend(compiled)
                tx.working["decisions"].append(bundle)
            self._checkpoint(Phase.VALIDATE, tx)

            self.phase = Phase.EXECUTE
            commands.sort(key=command_order)
            result = BatchExecutionResult.model_validate(self.scenario.execute_batch(tx.context(scenario_fields), commands))
            receipts = result.receipts
            if len(receipts) != len(commands):
                raise ValueError("Execution must return one receipt per command")
            for command, receipt in zip(commands, receipts, strict=True):
                if self._snapshot.schema_version == 4 and (receipt.schema_version != 4
                        or receipt.tick != command.tick or receipt.world_version != command.world_version):
                    raise ValueError("Economic receipt changed its command time/version")
                if (receipt.command_id != command.command_id or receipt.decision_id != command.decision_id
                        or receipt.actor_id != command.actor_id or receipt.execution_seq != tx.working["execution_seq"] + 1):
                    raise ValueError("Receipt identity/sequence mismatch")
                tx.working["execution_seq"] += 1
                tx.working["receipts"].append(receipt)
            self._scenario_data(tx, result.data)
            self._checkpoint(Phase.EXECUTE, tx)

            self.phase = Phase.FEEDBACK
            next_time = SimTime(tick=now.tick + 1, phase="OBSERVE")
            belief_memories = []
            for bundle in bundles:
                tx.working["beliefs"].setdefault(bundle.actor_id, {}).update(bundle.model_dump()["belief_patch"])
                if bundle.belief_patch:
                    belief_memories.append(ObservationFact(fact_id=f"belief:{bundle.decision_id}", category="memory",
                        owner_id=bundle.actor_id, visible_at=next_time, value=tx.working["beliefs"][bundle.actor_id]))
                messages = [Message.model_validate(m) for m in
                    getattr(self.compiler, "compile_messages", compile_messages)(bundle, now.tick)]
                if messages != compile_messages(bundle, now.tick):
                    raise ValueError("Message compiler changed draft identity, content or delivery time")
                tx.working["messages"].extend(messages)
            feedback = Feedback.model_validate(self.scenario.feedback(tx.context(scenario_fields), receipts))
            self._scenario_data(tx, feedback.data)
            memories = [ObservationFact(fact_id=f"memory:{r.command_id}", category="memory", owner_id=r.actor_id,
                         visible_at=next_time, value=r.model_dump()) for r in receipts] + belief_memories + list(feedback.memories)
            for memory in memories:
                if memory.category != "memory" or memory.owner_id not in barrier.actors or memory.visible_at.key < next_time.key:
                    raise ValueError("Feedback memory must be private and visible from t+1")
                tx.claim(memory.fact_id)
                tx.working["memories"].append(memory)
            for delta in feedback.relations:
                if (delta.source_id not in barrier.actors or delta.target_id not in barrier.actors
                        or delta.effective_at.phase != "BOUNDARY" or delta.effective_at.tick <= now.tick):
                    raise ValueError("Relation proposals must take effect at a later boundary")
                tx.claim(delta.delta_id)
                tx.working["pending_relations"].append(delta.model_dump())
            if self.registry.mechanisms:
                tx.set_data(self.registry.apply("FEEDBACK", tx.context()))
            if self.observation_effect is not None:
                spec = self.observation_effect
                restricted = tx.snapshot(projection=compact,memory_window=getattr(self.observer,'window',None) if compact else None,
                    data={k:v for k,v in tx.working['data'].items() if k in spec.reads})
                patch = self.observer.prepare_commit(restricted, tuple(observations))
                if patch.keys() - set(spec.writes):
                    raise ValueError("Observation effect wrote an undeclared field")
                tx.set_data(patch, fields=patch.keys())
            self._checkpoint(Phase.FEEDBACK, tx)

            self.phase = Phase.COMMIT
            self.scenario.validate(tx.context(scenario_fields))
            tx.capture_participants()
            prepared = tx.prepare(sorted(receipt.attempt_id for receipt in self._invocations))
            if validate_snapshot := getattr(self.scenario, 'validate_snapshot', None):
                validate_snapshot(prepared.snapshot)
            self._checkpoint(Phase.COMMIT, tx)
            if self._snapshot is not tx.base:
                raise RuntimeError("World changed during tick")
            if self.commit_store is not None:
                if getattr(self.commit_store, 'capture_observations', False):
                    self.commit_store.commit(tx.base, prepared, observations=observations)
                else:
                    self.commit_store.commit(tx.base, prepared)
            self._snapshot = prepared.snapshot
            self.last_prepared = prepared
            if self.commit_store is not None:
                self.commit_store.update_index()
            return prepared
        except BaseException as exc:
            self._failures.append({"tick": tx.working["tick"] if tx is not None else self._snapshot.tick + 1,
                                   "phase": self.phase.value if self.phase else None,
                                   "error_type": type(exc).__name__, "message": str(exc)})
            if prepared is not None and self.commit_store is not None and self.commit_store.is_committed(prepared):
                self._snapshot = prepared.snapshot
                self.last_prepared = prepared
            elif tx is not None:
                tx.rollback()
            raise
        finally:
            self._stepping = False
