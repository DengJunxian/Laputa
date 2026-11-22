"""The private wage-performance axis at the observation and commit boundaries."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_labor_outcomes import LABOR_ASSESSMENT, labor_assessments
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy
from civitas.scenarios.closed_economy_relations import social_context
from test_labor_contract_outcomes import EMPLOYER, NEIGHBOR, WORKER, outcome_config, outcomes


AXIS = "wage_performance_trust"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-wage-trust.json"


def enabled_config(**options):
    config = outcome_config(**options).model_dump()
    config["labor_transfer"]["performance_trust"] = True
    return ClosedEconomyConfig.model_validate(config)


def trust(world, actor=WORKER):
    context = social_context(world.observe(actor))
    return context.value.get(AXIS, {}) if context else {}


class WagePerformanceTrustTests(unittest.IsolatedAsyncioTestCase):
    async def test_paid_results_update_only_the_worker_at_the_next_observation(self):
        world = build_closed_economy(enabled_config(ticks=7, social_mode="none", network_neighbors=0))
        await world.run(5)
        self.assertEqual(trust(world), {})
        await world.step()
        self.assertEqual(trust(world), {EMPLOYER: {"a": 2, "b": 1}})
        await world.step()
        self.assertEqual(trust(world), {EMPLOYER: {"a": 3, "b": 1}})
        self.assertEqual(trust(world, NEIGHBOR), {})
        self.assertEqual(trust(world, EMPLOYER), {})

    async def test_real_arrears_adds_one_negative_count_and_leaves_ledger_unchanged(self):
        raw = json.loads(EXAMPLE.read_text())
        active = build_closed_economy(raw)
        raw["labor_transfer"]["performance_trust"] = False
        control = build_closed_economy(raw)
        for tick in range(1, 8):
            await active.step()
            await control.step()
            self.assertEqual(active.scenario.economy.export_state(), control.scenario.economy.export_state())
            self.assertEqual(active.snapshot.receipts, control.snapshot.receipts)
            self.assertEqual(social_context(active.observe(WORKER)).value["scores_bps"],
                             social_context(control.observe(WORKER)).value["scores_bps"])
            self.assertEqual(trust(control), {})
            if tick == 6:
                self.assertEqual(trust(active), {EMPLOYER: {"a": 3, "b": 1}})
                latest = max(outcomes(active.observe(WORKER)), key=lambda event: event["due_tick"])
                self.assertEqual((latest["outcome"], latest["accrued_minor"], latest["paid_minor"]),
                                 ("arrears", 24, 1))
        self.assertEqual(trust(active), {EMPLOYER: {"a": 3, "b": 2}})
        self.assertEqual(audit_snapshot(active.snapshot)["status"], "passed")

    async def test_cancellation_is_attributed_without_a_negative_count(self):
        world = build_closed_economy(enabled_config(cancel=1))
        await world.run(6)
        self.assertEqual(trust(world), {EMPLOYER: {"a": 2, "b": 1}})
        trace = world.snapshot.data["closed_social"]["trace"]
        self.assertEqual([r["outcome"] for r in trace if r["kind"] == "wage_trust_update"], ["paid"])
        self.assertTrue(any(r["kind"] == "labor_outcome_consumption" and r["actor_id"] == WORKER
                            and r["outcome"] == "cancelled_before_work" for r in trace))

    async def test_first_visibility_expiry_and_root_dedup_even_with_repeated_citations(self):
        class DelayFirstAssessment(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != WORKER or bundle.tick < 5:
                    return bundle
                patch = dict(bundle.belief_patch)
                assessments = [] if bundle.tick == 5 else list(labor_assessments(observation).values())
                patch[LABOR_ASSESSMENT] = assessments + assessments
                return bundle.model_copy(update={"belief_patch": patch,
                    "evidence_ids": sorted(set(bundle.evidence_ids) | {a["fact_id"] for a in assessments})})

        config = enabled_config(ticks=8)
        world = build_closed_economy(config, policy=DelayFirstAssessment(config, AddressedRNG(7, 0)))
        await world.run(8)
        self.assertEqual(trust(world), {EMPLOYER: {"a": 2, "b": 1}})
        facts = {r["due_tick"]: r for r in outcomes(world.observe(WORKER))}
        trace = world.snapshot.data["closed_social"]["trace"]
        updates = [r for r in trace if r["kind"] == "wage_trust_update"]
        self.assertEqual([(r["tick"], r["root_claim_id"]) for r in updates], [(6, facts[5]["root_claim_id"])])
        self.assertTrue(any(r["kind"] == "labor_outcome_consumption" and r["actor_id"] == WORKER
                            and r["root_claim_id"] == facts[4]["root_claim_id"] for r in trace))

    async def test_forged_outcome_or_hidden_fact_cannot_update_the_score(self):
        class MisattributingPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id != actor:
                    return bundle
                if actor == NEIGHBOR:
                    return bundle.model_copy(update={"evidence_ids": [*bundle.evidence_ids, worker_fact_id]})
                assessment, = bundle.belief_patch[LABOR_ASSESSMENT]
                patch = {**bundle.belief_patch, LABOR_ASSESSMENT: [{**assessment, "outcome": "arrears"}]}
                return bundle.model_copy(update={"belief_patch": patch})

        config = enabled_config()
        world = build_closed_economy(config)
        await world.run(4)
        worker_fact_id, = [f.fact_id for f in world.snapshot.memories if f.owner_id == WORKER
                           and f.value.get("kind") == "labor_contract_outcome"]
        before = content_hash(world.snapshot)
        world.policy = MisattributingPolicy(config, AddressedRNG(7, 0))
        for actor, error in ((WORKER, "matching visible evidence"), (NEIGHBOR, "unavailable evidence")):
            with self.subTest(actor=actor):
                with self.assertRaisesRegex(ValueError, error):
                    await world.step()
                self.assertEqual(content_hash(world.snapshot), before)

    async def test_failure_at_scoring_and_activation_recovers_with_independent_replay(self):
        raw = json.loads(EXAMPLE.read_text())
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "run"
            world, store = economic_run.create_run(raw, root)
            for tick in (6, 7):
                await economic_run.advance(world, store, until=tick - 1)
                before = content_hash(world.snapshot)
                book = content_hash(world.scenario.economy.export_state())
                visible = trust(world)

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before wage trust marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "before wage trust marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), book)
                self.assertEqual(trust(world), visible)
                world, store, marker = economic_run.open_run(root)
                self.assertEqual(marker.state_hash, before)
                await economic_run.advance(world, store, until=tick)
            direct = build_closed_economy(dict(raw, concurrency=4))
            await direct.run(7)
            self.assertEqual(content_hash(world.snapshot), content_hash(direct.snapshot))
            replay = await economic_run.replay_run(root, destination=Path(scratch) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 7))
            self.assertEqual(trust(world), {EMPLOYER: {"a": 3, "b": 2}})
