"""Focused runtime compatibility and failure tests; no market/LLM benchmark."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "reproduction/runtime/source"
sys.path[:0] = [str(SOURCE), str(SOURCE / "src")]

from civitas.cognition.memory import MemoryBook
from civitas.contracts.base import content_hash
from civitas.contracts.codecs import WORLD_CODECS, codec_for_version, decode_snapshot
from civitas.contracts.decisions import HoldIntent
from civitas.contracts.state import BatchExecutionResult, EconomicPreparedTick, Feedback
from civitas.core.observation import ObservationBuilder
from civitas.core.registry import MechanismRegistry
from civitas.core.rng import AddressedRNG
from civitas.core.world import WorldKernel
from civitas.economy import EconomicRuntime
from civitas.storage.checkpoint import attach, load, participant_versions, restore, save, validate_versions
from civitas.storage.commit_store import CommitStore
from civitas.storage.replay import state_replay, verification_replay


# Captured before this patch from the unmodified 6c66acc kernel, using exactly
# the no-memory two-actor/two-tick protocol fixture below (not a market result).
LEGACY_HASHES = {
    1: "ef582d1b4e3ee54d969468df43bd2200a0dc73c5ba869e271d22e7043ba28f91",
    3: "0492309d80d99981e631e60b01b795e22b9e2bb0f20005ba60274375c4a560f3",
}


class FixturePolicy:
    policy_id = "runtime_fixture"
    version = "1"

    def __init__(self, version, memory=None):
        self.schema = version
        self.memory = memory

    async def decide(self, observation):
        tick = observation.visible_until.tick
        if self.memory is not None:
            self.memory.accept(observation, working={"fixture_tick": tick})
        extra = {"tick": tick} if self.schema != 1 else {}
        return codec_for_version(self.schema).decision(
            **extra, decision_id=f"decision:{tick}:{observation.actor_id}",
            actor_id=observation.actor_id, observation_id=observation.observation_id,
            batch_id=observation.batch_id, observation_version=observation.observation_version,
            world_version=observation.world_version,
            policy_id=self.policy_id, policy_version=self.version,
            economic_intents=[HoldIntent(intent_id=f"intent:{tick}:{observation.actor_id}")],
        )


class FixtureCompiler:
    def __init__(self, version):
        self.schema = version

    def compile(self, bundle, arrival):
        extra = {key: getattr(bundle, key) for key in
                 ("tick", "batch_id", "observation_version", "world_version")} if self.schema != 1 else {}
        return [codec_for_version(self.schema).command(
            **extra, command_id=f"command:{intent.intent_id}", decision_id=bundle.decision_id,
            actor_id=bundle.actor_id, local_order_index=index, arrival_key=arrival, intent=intent,
        ) for index, intent in enumerate(bundle.economic_intents)]


class FixtureScenario:
    scenario_id = "runtime_fixture"
    version = "1"
    owned_fields = frozenset({"count"})

    def __init__(self, version):
        self.schema = version

    def initialize(self):
        return {"count": 0}

    def boundary(self, context):
        return context.data

    def apply_event(self, context, event):
        return context.data

    async def prepare_observations(self, context):
        return context.data

    def project_observation(self, context, actor):
        return []

    def execute_batch(self, context, commands):
        extra = {"outcome": "hold", "tick": context.tick,
                 "world_version": context.world_version} if self.schema == 4 else {}
        return BatchExecutionResult(
            data={**context.data, "count": context.data["count"] + len(commands)},
            receipts=[codec_for_version(self.schema).receipt(
                **extra, command_id=command.command_id, decision_id=command.decision_id,
                actor_id=command.actor_id, execution_seq=context.execution_seq + index + 1,
                status="accepted", executed_quantity=0,
            ) for index, command in enumerate(commands)],
        )

    def feedback(self, context, receipts):
        return Feedback(data=context.data)

    def validate(self, context):
        if type(context.data["count"]) is not int or context.data["count"] < 0:
            raise ValueError("Invalid count")

    def metrics(self, context):
        return {"count": context.data["count"]}


def build_fixture(version=4, *, concurrency=1, use_memory=False, policy=None, phase_hook=None):
    memory = MemoryBook(["alice", "bob"]) if use_memory else None
    world = WorldKernel(
        scenario=FixtureScenario(version), policy=policy or FixturePolicy(version, memory),
        compiler=FixtureCompiler(version), observer=ObservationBuilder(), registry=MechanismRegistry([]),
        rng=AddressedRNG(37, 0), actors=["alice", "bob"], events=[], schema_version=version,
        participants=(memory,) if memory is not None else (), concurrency=concurrency,
        phase_hook=phase_hook,
    )
    return world


def new_store(root, world):
    store = CommitStore.create(root, world, config={"scenario": "runtime_fixture", "seed": 37},
                               code_hash=content_hash("runtime_fixture"), run_id="runtime-fixture")
    attach(world, store)
    return store


class BookFixtureScenario(FixtureScenario):
    """Port adapter exercising real wages/production, not a second ledger."""
    def __init__(self, book):
        super().__init__(4)
        self.book = book

    def boundary(self, context):
        self.book.begin_tick(context.tick, context.world_version)
        self.book.accrue_wage(f"wage-accrual:{context.tick}", "bob", "alice", 6)
        self.book.pay_wage(f"wage-payment:{context.tick}", "bob", "alice", 6)
        self.book.produce(f"production:{context.tick}", "bob", 2, labor_cost_minor=6)
        return context.data

    def execute_batch(self, context, commands):
        receipts = self.book.execute_batch(commands, execution_seq_start=context.execution_seq)
        return BatchExecutionResult(
            data={**context.data, "count": context.data["count"] + len(commands)}, receipts=receipts)

    def validate(self, context):
        super().validate(context)
        self.book.validate_state()


def build_book_fixture():
    book = EconomicRuntime({"alice": 20, "bob": 100}, labor_cost_per_unit_minor=3)
    return WorldKernel(
        scenario=BookFixtureScenario(book), policy=FixturePolicy(4), compiler=FixtureCompiler(4),
        observer=ObservationBuilder(), registry=MechanismRegistry([]), rng=AddressedRNG(37, 0),
        actors=list(book.actor_ids), events=[], schema_version=4, participants=(book,),
    )


class RuntimeCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_v1_v3_golden_hashes(self):
        for version, expected in LEGACY_HASHES.items():
            with self.subTest(version=version):
                world = build_fixture(version)
                await world.run(2)
                self.assertEqual(content_hash(world.snapshot), expected)

    async def test_all_explicit_record_families_round_trip(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                world = build_fixture(version)
                prepared = await world.step()
                restored = decode_snapshot(world.snapshot.model_dump(mode="json"))
                self.assertIs(type(restored), codec_for_version(version).snapshot)
                self.assertIs(type(prepared), codec_for_version(version).prepared)
                self.assertEqual(restored, world.snapshot)
                self.assertEqual(restored.state_hash, content_hash(restored))
                self.assertEqual({d.schema_version for d in restored.decisions}, {version})
                self.assertEqual({r.schema_version for r in restored.receipts}, {version})

    async def test_prepare_reuses_a_validated_snapshot_and_still_checks_the_hash(self):
        prepared = await build_fixture().step()
        fields = prepared.model_dump()
        fields["snapshot"] = prepared.snapshot
        again = EconomicPreparedTick.model_validate(fields)
        self.assertIs(again.snapshot, prepared.snapshot)
        self.assertEqual(again.state_hash, content_hash(again.snapshot))
        with self.assertRaises((AttributeError, ValueError)):
            again.snapshot.state_hash = "0" * 64
        fields["state_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "version/hash mismatch"):
            EconomicPreparedTick.model_validate(fields)

    async def test_v4_concurrency_preserves_business_hash(self):
        serial = build_fixture(concurrency=1, use_memory=True)
        parallel = build_fixture(concurrency=4, use_memory=True)
        await serial.run(3)
        await parallel.run(3)
        self.assertEqual(content_hash(serial.snapshot), content_hash(parallel.snapshot))

    async def test_legacy_and_v4_store_checkpoint_resume_execution_replay(self):
        for version in (1, 3, 4):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                world = build_fixture(version, use_memory=True)
                store = new_store(Path(folder) / "run", world)
                await world.run(2)
                # Later deltas may reuse the recovery buffer, but the snapshots
                # already returned to a reader must retain their original values.
                history = list(store.states())
                self.assertEqual([content_hash(snapshot) for _, snapshot in history],
                                 [marker.state_hash for marker, _ in history])
                checkpoint = save(store)
                recovered = load(store, checkpoint["path"])
                self.assertEqual(recovered, world.snapshot)
                resumed = build_fixture(version, use_memory=True)
                restore(resumed, store)
                self.assertIs(resumed.commit_store, store)
                await resumed.step()
                self.assertEqual(state_replay(store), resumed.snapshot)
                verified = await verification_replay(store, lambda: build_fixture(version, use_memory=True))
                self.assertEqual(verified["status"], "matched")
                self.assertEqual(verified["compared_ticks"], 3)
                self.assertFalse(verified["applied_deltas_to_execution"])

    async def test_unknown_and_mismatched_versions_fail_closed(self):
        world = build_fixture()
        payload = world.snapshot.model_dump(mode="json")
        for invalid in (0, 5, True, 4.0, "4"):
            with self.subTest(version=invalid):
                with self.assertRaises(ValueError):
                    decode_snapshot({**payload, "schema_version": invalid})
        with self.assertRaises(ValueError):
            build_fixture(99)
        with self.assertRaises(ValueError):
            world.restore_snapshot(build_fixture(3).snapshot)
        with self.assertRaises(TypeError):
            WORLD_CODECS[5] = WORLD_CODECS[4]
        for versions in ({"new_arbitrary_module": 1}, {"economic_runtime": 4}, {"economic_runtime": True}):
            with self.assertRaises(ValueError):
                validate_versions(versions)
        validate_versions({"economic_runtime": 1, "portfolio_ledger": 3})
        validate_versions({"economic_runtime": 3})

    async def test_snapshot_rejection_precedes_store_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            world = build_fixture()
            store = new_store(Path(folder) / "good", world)
            destination = Path(folder) / "invalid"
            with self.assertRaises(ValueError):
                CommitStore.initialize(destination, store.identity,
                    {**world.snapshot.model_dump(mode="json"), "schema_version": 99})
            self.assertFalse(destination.exists())

    async def test_v4_time_context_is_enforced_at_each_boundary(self):
        for boundary in ("decision", "command", "receipt"):
            with self.subTest(boundary=boundary):
                world = build_fixture(use_memory=True)
                before = content_hash(world.snapshot)
                memory_before = world.participants[0].export_state()
                if boundary == "decision":
                    class FuturePolicy(FixturePolicy):
                        async def decide(self, observation):
                            value = await super().decide(observation)
                            return value.model_copy(update={"tick": value.tick + 1})
                    world.policy = FuturePolicy(4, world.participants[0])
                elif boundary == "command":
                    class FutureCompiler(FixtureCompiler):
                        def compile(self, bundle, arrival):
                            return [c.model_copy(update={"batch_id": "other-batch"})
                                    for c in super().compile(bundle, arrival)]
                    world.compiler = FutureCompiler(4)
                else:
                    class FutureScenario(FixtureScenario):
                        def execute_batch(self, context, commands):
                            result = super().execute_batch(context, commands)
                            return result.model_copy(update={"receipts": [
                                r.model_copy(update={"world_version": r.world_version + 1})
                                for r in result.receipts]})
                    world.scenario = FutureScenario(4)
                with self.assertRaises(ValueError):
                    await world.step()
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(world.participants[0].export_state(), memory_before)


class RuntimeFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_marker_failure_rolls_back_participant_and_world(self):
        with tempfile.TemporaryDirectory() as folder:
            world = build_fixture(use_memory=True)
            before = content_hash(world.snapshot)
            memory_before = world.participants[0].export_state()
            store = new_store(Path(folder) / "run", world)
            def fail(point):
                if point == "before_marker":
                    raise RuntimeError("injected before marker")
            store.fault_hook = fail
            with self.assertRaisesRegex(RuntimeError, "before marker"):
                await world.step()
            self.assertEqual(content_hash(world.snapshot), before)
            self.assertEqual(world.participants[0].export_state(), memory_before)
            self.assertEqual(store.load()[0].tick, 0)
            self.assertEqual(world.failures[-1]["tick"], 1)
            store.fault_hook = None
            await world.step()
            self.assertEqual(store.load()[0].tick, 1)

    async def test_published_marker_and_index_failure_keep_committed_head(self):
        for fault in ("after_marker", "before_index", "after_index"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                world = build_fixture(use_memory=True)
                store = new_store(Path(folder) / "run", world)
                def fail(point):
                    if point == fault:
                        raise RuntimeError("injected " + point)
                store.fault_hook = fail
                with self.assertRaisesRegex(RuntimeError, fault):
                    await world.step()
                self.assertIsInstance(world.last_prepared, EconomicPreparedTick)
                self.assertEqual(world.snapshot.tick, 1)
                self.assertEqual(store.load()[1], world.snapshot)
                self.assertEqual(world.participants[0].export_state(), world.snapshot.data["cognition_memory"])
                self.assertEqual(world.failures[-1]["tick"], 1)
                store.fault_hook = None
                resumed = build_fixture(use_memory=True)
                restore(resumed, store)
                store.repair_index()
                await resumed.step()
                self.assertEqual(resumed.snapshot.execution_seq, 4)
                self.assertEqual(store.load()[0].tick, 2)

    async def test_failure_cancels_and_drains_before_participant_rollback(self):
        world = build_fixture(concurrency=2, use_memory=True)
        memory = world.participants[0]
        original = memory.export_state()
        started, drained = asyncio.Event(), asyncio.Event()
        class FailingPolicy(FixturePolicy):
            async def decide(self, observation):
                if observation.actor_id == "alice":
                    await started.wait()
                    raise RuntimeError("policy failure")
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    # Cleanup is deliberately stateful: rollback must happen
                    # after this coroutine completes, not just after cancel().
                    await asyncio.sleep(0)
                    memory.accept(observation, working={"cleanup_completed": True})
                    drained.set()
        world.policy = FailingPolicy(4, memory)
        with self.assertRaisesRegex(RuntimeError, "policy failure"):
            await asyncio.wait_for(world.step(), timeout=2)
        self.assertTrue(drained.is_set())
        self.assertEqual(memory.export_state(), original)
        self.assertEqual(world.snapshot.tick, 0)
        await asyncio.sleep(0)
        self.assertEqual(memory.export_state(), original)

    async def test_external_cancellation_drains_decisions_before_rollback(self):
        world = build_fixture(concurrency=2, use_memory=True)
        memory = world.participants[0]
        original = memory.export_state()
        started, seen, drained = asyncio.Event(), set(), set()
        class WaitingPolicy(FixturePolicy):
            async def decide(self, observation):
                seen.add(observation.actor_id)
                if len(seen) == 2:
                    started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    memory.accept(observation, working={"cancel_cleanup": True})
                    drained.add(observation.actor_id)
        world.policy = WaitingPolicy(4, memory)
        task = asyncio.create_task(world.step())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(drained, {"alice", "bob"})
        self.assertEqual(memory.export_state(), original)
        self.assertEqual(world.snapshot.tick, 0)
        self.assertFalse(world._stepping)


class DependencyBoundaryTests(unittest.TestCase):
    def test_participant_validation_does_not_import_legacy_numerical_stack(self):
        script = """
import builtins
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'numpy', 'pandas', 'networkx', 'torch', 'ray'}:
        raise AssertionError('Unexpected optional dependency import: '+name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from civitas.storage.checkpoint import validate_versions
validate_versions({'economic_runtime': 1})
validate_versions({'cognition_memory': 1, 'portfolio_ledger': 3})
"""
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(SOURCE), str(SOURCE / "src"))))
        result = subprocess.run([sys.executable, "-c", script], env=env, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class EconomicParticipantTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_book_checkpoint_resume_and_verification_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            world = build_book_fixture()
            self.assertEqual(participant_versions(world), {"economic_runtime": 1})
            store = new_store(Path(folder) / "run", world)
            await world.run(2)
            checkpoint = save(store)
            self.assertEqual(load(store, checkpoint["path"]), world.snapshot)
            resumed = build_book_fixture()
            restore(resumed, store)
            await resumed.step()
            book = resumed.participants[0]
            self.assertEqual(book.account_view("alice").wage_income_minor, 18)
            self.assertEqual(book.account_view("bob").goods_units, 6)
            self.assertEqual(sum(book.account_view(a).cash_minor for a in book.actor_ids), 120)
            self.assertEqual(book.export_state(), resumed.snapshot.data["economic_runtime"])
            self.assertEqual(state_replay(store), resumed.snapshot)
            verified = await verification_replay(store, build_book_fixture)
            self.assertEqual(verified["status"], "matched")
            self.assertEqual(verified["compared_ticks"], 3)

    async def test_real_book_rolls_back_or_keeps_state_by_marker(self):
        for fault, committed_tick in (("before_marker", 0), ("after_marker", 1)):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                world = build_book_fixture()
                initial = world.participants[0].export_state()
                store = new_store(Path(folder) / "run", world)
                def fail(point):
                    if point == fault:
                        raise RuntimeError("real-book " + point)
                store.fault_hook = fail
                with self.assertRaisesRegex(RuntimeError, fault):
                    await world.step()
                self.assertEqual(world.snapshot.tick, committed_tick)
                self.assertEqual(store.load()[1], world.snapshot)
                self.assertEqual(world.participants[0].export_state(), world.snapshot.data["economic_runtime"])
                if not committed_tick:
                    self.assertEqual(world.participants[0].export_state(), initial)
                store.fault_hook = None
                resumed = build_book_fixture()
                restore(resumed, store)
                await resumed.step()
                self.assertEqual(resumed.participants[0].account_view("alice").wage_income_minor,
                                 6 * (committed_tick + 1))


if __name__ == "__main__":
    unittest.main()
