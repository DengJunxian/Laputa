from pathlib import Path
import tempfile
import unittest

from civitas.application import economic_run
from civitas.application.inspection import inspect_run
from civitas.storage.commit_store import CommitStore

BASE = {"ticks": 4, "households": 4, "firms": 2, "securities": {"shock_tick": 2},
        "archive_observations": True, "fault_tick": None}


class UnifiedSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_security_orders_share_v4_book_and_recover_a_failed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "market"
            world, store = economic_run.create_run(BASE, root)
            await world.step()
            before = world.snapshot

            def fail_before_marker(point):
                if point == "before_marker":
                    raise OSError("failure at security commit")

            store.fault_hook = fail_before_marker
            with self.assertRaisesRegex(OSError, "security commit"):
                await world.step()
            self.assertEqual(world.snapshot, before)
            self.assertEqual(store.markers()[-1].tick, 1)
            await economic_run.resume_run(root)
            _, snapshot = CommitStore(root).load()
            book = snapshot.data["economic_runtime"]
            self.assertTrue(any(e["kind"] == "security_trade" for e in book["journal"]))
            self.assertEqual(set(snapshot.data), {"security_market", "economic_runtime"})
            self.assertEqual(sum(book["initial_cash"].values()), sum(a["cash"] for a in book["balances"].values()))
            self.assertEqual(sum(book["initial_goods"].values()), sum(book["goods"].values()))
            self.assertTrue(any(r.unit == "security_units" and r.ledger_tx_ids for r in snapshot.receipts))
            result = await economic_run.replay_run(root, destination=Path(tmp)/"replay")
            self.assertEqual(result["compared_ticks"], 4)
            self.assertEqual(result["status"], "matched")

    async def test_no_trade_is_missing_price_and_funded_rejections_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "market"
            await economic_run.run_new({**BASE, "securities": {"shock_tick":2, "maker_cash_minor":0}}, root)
            store = CommitStore(root)
            marker, state = store.load(2)
            self.assertIsNone(state.data["security_market"]["history"][-1]["close_minor"])
            self.assertTrue(all(r.reason_codes == ["insufficient_security_cash"] for r in state.receipts
                                if r.tick == 2 and r.actor_id.startswith("maker:")))
            observer = next(o for o in store.observation_rows(marker) if o.actor_id == "investor:0000")
            self.assertTrue(all(f.owner_id == observer.actor_id for f in observer.account_view))
            self.assertIsNone(inspect_run(root,[2],audience="researcher")["views"][0]["metrics"][1][1])

    async def test_restore_rejects_security_receipt_that_reverses_the_cash_leg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'market'
            world, _ = economic_run.create_run(BASE, root)
            await world.step()
            original = world.snapshot
            receipts = list(original.receipts)
            i = next(i for i, row in enumerate(receipts) if row.cash_delta_minor < 0)
            receipts[i] = receipts[i].model_copy(update={'cash_delta_minor': -receipts[i].cash_delta_minor})
            forged = original.model_copy(update={'receipts': receipts})
            with self.assertRaisesRegex(ValueError, 'Security receipt differs from its journal settlement'):
                world.restore_snapshot(forged)
            self.assertEqual(world.snapshot, original)

    async def test_binding_maker_bids_change_realized_volume_and_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = {}
            for mode in ('prior_close', 'fundamental'):
                cfg = {**BASE, 'households': 2, 'network_neighbors': 1, 'root_seed': 90003,
                       'securities': {'shock_tick': 2, 'herding': False, 'binding_maker_bid': mode}}
                root = Path(tmp)/mode
                await economic_run.run_new(cfg, root, until=2)
                _, snapshot = CommitStore(root).load()
                results[mode] = snapshot.data['security_market']['history']
                replay = await economic_run.replay_run(root, destination=Path(tmp)/(mode+'-replay'))
                self.assertEqual(replay['status'], 'matched')
            self.assertEqual(results['prior_close'][0], results['fundamental'][0])
            self.assertEqual(results['prior_close'][1]['volume_units'], 4)
            self.assertEqual(results['fundamental'][1]['volume_units'], 3)
            self.assertEqual(results['prior_close'][1]['close_minor'], 96)
            self.assertEqual(results['fundamental'][1]['close_minor'], 95)
