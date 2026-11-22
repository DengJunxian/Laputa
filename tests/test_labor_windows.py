"""Two separate private work windows over the same worker and employers."""

import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.application.labor_projection import project_labor_graph
from civitas.contracts.base import content_hash
from civitas.contracts.economic import LaborAcceptIntent
from civitas.core.rng import AddressedRNG
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-labor-windows.json"
WORKER = "household:0001"


def config():
    return json.loads(EXAMPLE.read_text())


def choices(world, tick):
    return {
        row["candidate_employer_id"]: row
        for row in world.snapshot.data["closed_social"]["trace"]
        if row["kind"] == "rehire_choice" and row["tick"] == tick
    }


def fees(world):
    return [row for row in world.scenario.economy.journal if row.kind == "labor_switch_fee"]


class LaborWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_wage_changes_a_second_choice_and_fees_follow_the_actual_predecessor(self):
        world = build_closed_economy(config())
        await world.run(10)
        first = choices(world, 6)
        second = choices(world, 9)
        self.assertIsNone(first["firm:01"]["score"])
        self.assertEqual(first["firm:01"]["status"], "accepted")
        self.assertEqual(second["firm:01"]["score"], {"a": 2, "b": 1})
        self.assertEqual(second["firm:01"]["status"], "skipped_low_trust")
        self.assertEqual(second["firm:00"]["status"], "accepted")
        (first_outcome,) = [
            r
            for r in world.snapshot.data["closed_social"]["trace"]
            if r["kind"] == "wage_trust_update" and r["actor_id"] == WORKER and r["employer_id"] == "firm:01"
        ]
        self.assertEqual(first_outcome["effective_tick"], 9)
        self.assertEqual(second["firm:01"]["score_roots"], [first_outcome["root_claim_id"]])
        self.assertEqual(
            [(r.tick, r.payload["recipient_id"], r.payload["amount_minor"]) for r in fees(world)],
            [(7, "firm:00", 3), (10, "firm:01", 3)],
        )
        self.assertNotEqual(fees(world)[0].payload["contract_id"], fees(world)[1].payload["contract_id"])

    async def test_early_window_cannot_consume_pending_wage_score_or_overlap_prior_contract(self):
        fields = config()
        second = fields["labor_transfer"]["second_rehire"]
        second["offer_tick"] = 7
        with self.assertRaisesRegex(ValueError, "requires the first to have ended"):
            ClosedEconomyConfig.model_validate(fields)
        second["offer_tick"] = 8
        world = build_closed_economy(fields)
        await world.run(9)
        choice = choices(world, 8)["firm:01"]
        self.assertEqual(
            (choice["score"], choice["status"], choice["offer"]["kind"]),
            (None, "accepted", "labor_rehire_offer"),
        )
        self.assertEqual(len(fees(world)), 1)

    async def test_each_window_has_a_single_contract_and_earlier_offer_id_expires(self):
        class CompetingPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick != 9 or bundle.actor_id != WORKER:
                    return bundle
                current = next(i for i in bundle.economic_intents if isinstance(i, LaborAcceptIntent))
                repeat = current.model_copy(update={"intent_id": "duplicate-later-accept"})
                earlier = LaborAcceptIntent(
                    intent_id="expired-earlier-offer",
                    offer_id=f"labor-switch:6:{WORKER}:firm:01",
                    max_switch_fee_minor=3,
                )
                competing = LaborAcceptIntent(
                    intent_id="also-retain-firm", offer_id=f"labor-switch:9:{WORKER}:firm:01"
                )
                return bundle.model_copy(
                    update={"economic_intents": [*bundle.economic_intents, repeat, earlier, competing]}
                )

        settings = ClosedEconomyConfig.model_validate(config())
        world = build_closed_economy(settings, policy=CompetingPolicy(settings, AddressedRNG(7, 0)))
        await world.run(10)
        receipts = {r.command_id: r for r in world.snapshot.receipts}
        self.assertEqual(
            receipts["command:duplicate-later-accept"].reason_codes, ["labor_offer_already_accepted"]
        )
        self.assertEqual(receipts["command:also-retain-firm"].reason_codes, ["labor_offer_already_accepted"])
        self.assertEqual(receipts["command:expired-earlier-offer"].reason_codes, ["labor_offer_unavailable"])
        work = [r for r in world.snapshot.data["closed_economy"]["labor_events"] if r["worker_id"] == WORKER]
        self.assertEqual([r["tick"] for r in work], [1, 2, 3, 4, 7, 10])
        self.assertEqual(len(fees(world)), 2)

    async def test_later_cash_rejection_preserves_earlier_fee_and_earlier_failure_does_not_block_later_window(
        self,
    ):
        for unaffordable in ("rehire", "second_rehire"):
            with self.subTest(window=unaffordable):
                raw = config()
                raw["labor_transfer"][unaffordable]["switch_fee_minor"] = 1000
                world = build_closed_economy(raw)
                await world.run(11)
                if unaffordable == "rehire":
                    first, then = choices(world, 6), choices(world, 9)
                    self.assertEqual(first["firm:01"]["reason_codes"], ["insufficient_labor_switch_fee_cash"])
                    self.assertEqual(then["firm:00"]["status"], "accepted")
                    self.assertEqual(fees(world), [])
                else:
                    self.assertEqual(
                        choices(world, 9)["firm:00"]["reason_codes"], ["insufficient_labor_switch_fee_cash"]
                    )
                    self.assertEqual([r.tick for r in fees(world)], [7])
                    self.assertIsNone(world.snapshot.data["closed_economy"]["second_rehire_contract"])
                self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")

    async def test_initial_and_both_later_windows_keep_visibility_and_whole_household_denominators(self):
        world = build_closed_economy(config())
        await world.run(9)
        graph = project_labor_graph(world.snapshot)
        self.assertEqual(graph["opportunity_scope"], "recorded_initial_and_followup_candidates")
        self.assertEqual(graph["opportunity_counts"]["candidates"], 5)
        self.assertEqual(graph["window_counts"]["9"]["signed_not_started"], 1)
        self.assertEqual(graph["window_counts"]["6"]["visible_unknown_score"], 1)
        self.assertIn(WORKER, graph["unemployed_worker_ids"])
        await world.step()
        graph = project_labor_graph(world.snapshot)
        self.assertEqual([graph["window_counts"][str(t)]["worked"] for t in (2, 6, 9)], [1, 1, 1])
        self.assertEqual(graph["counts"]["households"], 6)

        no_sales = build_closed_economy(dict(config(), disabled_actions=["purchase"]))
        await no_sales.run(9)
        hidden = project_labor_graph(no_sales.snapshot)
        self.assertEqual(
            hidden["opportunity_counts"],
            {"candidates": 5, "visible": 0, "worked": 0, "by_status": {"not_visible": 5}},
        )

    async def test_economic_horizon_is_not_exposed_via_quote_expiry(self):
        left = build_closed_economy(config())
        right = build_closed_economy(dict(config(), ticks=13))
        await left.run(6)
        await right.run(6)
        a, b = [world.observe(WORKER) for world in (left, right)]
        self.assertEqual(a, b)
        quotes = [f for f in a.market_view if f.value.get("kind") == "goods_quote"]
        self.assertTrue(quotes)
        self.assertTrue(all("expires_after_tick" not in f.value for f in quotes))

    async def test_later_signature_and_fee_rollback_replay_and_fixed_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run"
            world, store = economic_run.create_run(config(), path)
            for tick in (9, 10):
                await economic_run.advance(world, store, until=tick - 1)
                before = content_hash(world.snapshot)
                book = world.scenario.economy.export_state()

                def abort(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before the later window marker")

                world.phase_hook = abort
                with self.assertRaisesRegex(RuntimeError, "later window marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(world.scenario.economy.export_state(), book)
                world, store, marker = economic_run.open_run(path)
                self.assertEqual(marker.state_hash, before)
                await economic_run.advance(world, store, until=tick)
                self.assertEqual(len(fees(world)), 1 if tick == 9 else 2)
            await economic_run.advance(world, store, until=11)
            direct = build_closed_economy(dict(config(), concurrency=4))
            await direct.run(11)
            self.assertEqual(content_hash(world.snapshot), content_hash(direct.snapshot))
            replay = await economic_run.replay_run(path, destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 11))
            export = economic_run.export_projection(path, tick=9)
            graph = json.loads((Path(export["path"]) / "relations.json").read_text())["labor_graph"]
            self.assertEqual(graph["window_counts"]["9"]["signed_not_started"], 1)
