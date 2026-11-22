"""Joint graph and journal verification through ordinary C/D ticks."""
import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.contracts.base import content_hash
from civitas.core.rng import AddressedRNG
from civitas.economy.runtime import EconomicReject, EconomicRuntime
from civitas.experiments.closed_economy import audit_snapshot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy

AUTHOR = "household:0000"
PAYER = "household:0003"
COLLECTOR = "firm:00"


def config(**changes):
    sample = Path(__file__).resolve().parents[1] / "examples" / "closed-economy-network-link-fee.json"
    return ClosedEconomyConfig.model_validate(dict(json.loads(sample.read_text()), **changes))


def graph(world):
    return world.snapshot.data["closed_social"]["network"]


def entries(world):
    return [entry for entry in world.scenario.economy.journal if entry.kind == "link_fee"]


def attempts(world, payer=PAYER):
    return [row for row in world.snapshot.data["closed_social"]["trace"]
            if row["kind"] == "network_proposal" and row["actor_id"] == payer]


class NetworkLinkFeeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_edge_settles_one_balanced_fee_at_activation(self):
        world = build_closed_economy(config())
        await world.run(4)
        pending, = [d for d in graph(world)["pending"] if d["target_id"] == PAYER]
        self.assertEqual((pending["source_id"], pending["effective_tick"]), (AUTHOR, 5))
        self.assertFalse(entries(world))
        self.assertEqual([(r["fee_minor"], r["fee_status"]) for r in attempts(world)], [(3, "due")])
        await world.step()
        self.assertIn(pending, graph(world)["applied"])
        entry, = [e for e in entries(world) if e.payload["delta_id"] == pending["delta_id"]]
        self.assertEqual((entry.tick, entry.payload["payer_id"], entry.payload["recipient_id"]),
                         (5, PAYER, COLLECTOR))
        self.assertEqual([(p.entity_id, p.account_code, p.side, p.amount_minor) for p in entry.postings], [
            (PAYER, "link_fee_expense", "debit", 3), (PAYER, "cash", "credit", 3),
            (COLLECTOR, "cash", "debit", 3), (COLLECTOR, "link_fee_income", "credit", 3)])
        self.assertFalse(entry.goods_movements)
        self.assertEqual(audit_snapshot(world.snapshot)["status"], "passed")
        settled, = [r for r in world.snapshot.data["closed_social"]["trace"]
                    if r["kind"] == "communication_edge_active" and r["delta_id"] == pending["delta_id"]]
        self.assertEqual((settled["fee_status"], settled["fee_tx_id"]), ("settled", entry.tx_id))

    def test_integer_posting_is_idempotent_preserves_wage_claim_and_rebuilds(self):
        opening = {"payer": 5, "collector": 0, "worker": 0}
        book = EconomicRuntime(opening)
        book.begin_tick(1, 0)
        book.accrue_wage("work", "payer", "worker", 4)
        claims = book.export_state()["wage_obligations"]
        posted = book.settle_link_fee("edge", "payer", "collector", 3)
        self.assertEqual(book.settle_link_fee("edge", "payer", "collector", 3), posted)
        after = book.export_state()
        with self.assertRaisesRegex(ValueError, "payload conflict"):
            book.settle_link_fee("edge", "payer", "collector", 2)
        with self.assertRaisesRegex(EconomicReject, "insufficient_link_fee_cash"):
            book.settle_link_fee("different-edge", "payer", "collector", 3)
        self.assertEqual(book.export_state(), after)
        self.assertEqual(after["wage_obligations"], claims)
        clone = EconomicRuntime(opening)
        clone.import_state(after)
        self.assertEqual(clone.export_state(), after)

    async def test_admission_uses_cash_left_after_goods(self):
        zero = build_closed_economy(config(link_fee_minor=0))
        await zero.run(3)
        observed = {}

        def save_pre_action_cash(phase, snapshot):
            if phase.value == "OBSERVE":
                observed["cash"] = zero.scenario.economy.account_view(PAYER).available_cash_minor

        zero.phase_hook = save_pre_action_cash
        await zero.step()
        fee = zero.scenario.economy.account_view(PAYER).available_cash_minor + 1
        self.assertLessEqual(fee, observed["cash"])
        self.assertTrue(any(d["target_id"] == PAYER for d in graph(zero)["pending"]))
        self.assertFalse(entries(zero))
        charged = build_closed_economy(config(link_fee_minor=fee))
        await charged.run(5)
        self.assertIn((4, "insufficient_link_fee_cash", 0),
                      [(r["tick"], r["status"], r["slots_used"]) for r in attempts(charged)])
        self.assertFalse(any(d["target_id"] == PAYER for d in graph(charged)["applied"]))
        self.assertFalse(any(e.payload["payer_id"] == PAYER for e in entries(charged)))

    async def test_invalid_expired_and_unpriced_proposals_leave_no_fee(self):
        class InvalidPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id == PAYER and bundle.tick == 4:
                    self.original, = bundle.network_proposals
                    return bundle.model_copy(update={"network_proposals": [
                        self.original.model_copy(update={"candidate_id": "household:0004"}),
                        self.original.model_copy(update={"fee_limit_minor": 1})]})
                if bundle.actor_id == PAYER and bundle.tick == 5:
                    return bundle.model_copy(update={"network_proposals": [self.original]})
                return bundle

        settings = config()
        world = build_closed_economy(settings, policy=InvalidPolicy(settings, AddressedRNG(7, 0)))
        await world.run(6)
        self.assertEqual([r["status"] for r in attempts(world)], [
            "link_fee_not_authorized", "introduction_unavailable", "introduction_unavailable"])
        self.assertFalse(any(e.payload["payer_id"] == PAYER for e in entries(world)))

    async def test_duplicate_proposal_charges_once_and_restore_rejects_orphaned_fee(self):
        class RepeatingPolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.actor_id == PAYER and bundle.tick == 4:
                    return bundle.model_copy(update={"network_proposals": bundle.network_proposals * 2})
                return bundle

        settings = config()
        world = build_closed_economy(settings, policy=RepeatingPolicy(settings, AddressedRNG(7, 0)))
        await world.run(5)
        self.assertEqual([r["status"] for r in attempts(world) if r["tick"] == 4], ["scheduled", "duplicate"])
        paid, = [e for e in entries(world) if e.payload["payer_id"] == PAYER]
        before = content_hash(world.snapshot)
        data = world.snapshot.model_dump()["data"]
        applied = data["closed_social"]["network"]["applied"]
        applied[:] = [d for d in applied if d["delta_id"] != paid.payload["delta_id"]]
        with self.assertRaisesRegex(ValueError, "Link fees disagree"):
            world.restore_snapshot(world.snapshot.model_copy(update={"data": data}))
        self.assertEqual(content_hash(world.snapshot), before)

    async def test_activation_and_fee_recover_together_before_commit_and_replay(self):
        settings = config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run"
            world, store = economic_run.create_run(settings, path)
            for tick in (4, 5):
                await economic_run.advance(world, store, until=tick - 1)
                before = content_hash(world.snapshot)
                book = content_hash(world.scenario.economy.export_state())

                def fail(phase, snapshot):
                    if phase.value == "COMMIT":
                        raise RuntimeError("before fee marker")

                world.phase_hook = fail
                with self.assertRaisesRegex(RuntimeError, "before fee marker"):
                    await economic_run.advance(world, store, until=tick)
                self.assertEqual(content_hash(world.snapshot), before)
                self.assertEqual(content_hash(world.scenario.economy.export_state()), book)
                world, store, marker = economic_run.open_run(path)
                self.assertEqual(marker.state_hash, before)
                await economic_run.advance(world, store, until=tick)
                self.assertEqual(bool(entries(world)), tick == 5)
            await economic_run.advance(world, store, until=6)
            replay = await economic_run.replay_run(path, destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 6))
            direct = build_closed_economy(config(concurrency=4))
            await direct.run(6)
            self.assertEqual(content_hash(world.snapshot), content_hash(direct.snapshot))
