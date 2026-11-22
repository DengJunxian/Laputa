"""The research export projects verified labor activity at its fixed cursor."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from civitas.application import economic_run


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
WORKER = "household:0001"


def config(case):
    return json.loads((EXAMPLES / f"closed-economy-{case}.json").read_text())


def relation_table(root, tick):
    manifest = economic_run.export_projection(root, tick=tick)
    return json.loads((Path(manifest["path"]) / "relations.json").read_text())


def graph(root, tick):
    return relation_table(root, tick)["labor_graph"]


class LaborProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_switched_contract_is_pending_until_its_work_then_expires(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "switch"
            await economic_run.run_new(config("labor-switch"), root)
            pending = graph(root, 5)
            active = graph(root, 6)
            expired = graph(root, 7)
            recent = [view["contracts"][-1] for view in (pending, active, expired)]
            self.assertEqual([row["status"] for row in recent], ["pending", "active", "expired"])
            self.assertEqual([len(row["work_event_ids"]) for row in recent], [0, 1, 1])
            self.assertIn(WORKER, pending["unemployed_worker_ids"])
            self.assertIn(WORKER, expired["unemployed_worker_ids"])
            (worker,) = [edge for edge in active["active"] if edge["worker_id"] == WORKER]
            self.assertEqual((worker["employer_id"], worker["delivered_minutes"]), ("firm:01", 480))
            self.assertEqual(active["counts"]["households"], 6)
            self.assertIsNone(recent[0]["switch_fee_tx_id"])
            self.assertTrue(recent[1]["switch_fee_tx_id"].startswith("econ:labor-switch-fee:"))
            self.assertEqual(
                (pending["opportunity_counts"]["worked"], active["opportunity_counts"]["worked"]), (0, 1)
            )
            world, _, _ = economic_run.open_run(root, tick=6)
            other = world.observe("household:0002")
            self.assertNotIn("labor_graph", other.model_dump_json())
            self.assertFalse(any(f.value.get("worker_id") == WORKER for f in other.private_memory_refs))

    async def test_default_work_prestart_cancellation_and_early_expiry_are_distinct(self):
        raw = config("payroll-installments")
        raw["initial_firm_cash_minor"] = 160
        payroll = raw["labor_transfer"]["shared_payroll"]
        payroll["cancel_before_start_slot"] = 1
        payroll["payroll_window"]["cancel_second_wage_slot"] = 2
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "cancel"
            await economic_run.run_new(raw, root)
            initial = graph(root, 0)
            self.assertEqual(
                initial["counts"], {"households": 4, "employed": 4, "unemployed": 0, "worked_at_cursor": 0}
            )
            self.assertTrue(
                all(
                    r["origin"] == "initial_assignment" and r["work_event_id"] is None
                    for r in initial["active"]
                )
            )
            before_start = graph(root, 2)
            self.assertEqual([c["status"] for c in before_start["contracts"]], ["pending", "pending"])
            mid = graph(root, 4)
            by_worker = {c["worker_id"]: c for c in mid["contracts"]}
            self.assertEqual(
                (by_worker[WORKER]["status"], by_worker[WORKER]["wage_tx_ids"]), ("not_activated", [])
            )
            self.assertEqual(by_worker["household:0003"]["status"], "active")
            (default,) = [r for r in mid["active"] if r["worker_id"] == WORKER]
            self.assertEqual((default["origin"], default["employer_id"]), ("initial_assignment", "firm:01"))
            (ended,) = [c for c in graph(root, 5)["contracts"] if c["status"] == "expired"]
            self.assertEqual(
                (ended["worker_id"], ended["end_tick_exclusive"], ended["scheduled_end_tick_exclusive"]),
                ("household:0003", 5, 6),
            )
            self.assertEqual(len(ended["wage_tx_ids"]), 2)

    async def test_rejected_funding_never_creates_an_edge_or_reallocates_prior_claim(self):
        raw = config("wage-trust")
        raw["ticks"] = 8
        raw["labor_transfer"]["rehire"] = {"offer_tick": 7}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "funding"
            await economic_run.run_new(raw, root)
            view = graph(root, 8)
            (candidate,) = view["opportunities"]
            self.assertEqual(
                (candidate["status"], candidate["visible"], candidate["worked"], candidate["reason_codes"]),
                ("rejected", True, False, ["insufficient_labor_funding"]),
            )
            self.assertEqual(
                view["opportunity_counts"],
                {"candidates": 1, "visible": 1, "worked": 0, "by_status": {"rejected": 1}},
            )
            (predecessor,) = view["contracts"]
            self.assertEqual(predecessor["status"], "expired")
            self.assertIn(WORKER, view["unemployed_worker_ids"])
            (claim,) = [
                w
                for w in view["wage_obligations"]
                if w["worker_id"] == WORKER and w["employer_id"] == "firm:00"
            ]
            self.assertEqual(
                (
                    view["wage_obligation_basis"],
                    claim["outstanding_minor"],
                    claim["contributing_contract_ids"],
                ),
                ("employer_worker_pair", 23, [predecessor["contract_id"]]),
            )
            self.assertEqual(claim["wage_tx_ids"], predecessor["wage_tx_ids"])

    async def test_communication_withdrawal_preserves_labor_edges_and_positive_claims(self):
        raw = dict(config("network-withdrawals"), initial_firm_cash_minor=10)
        with tempfile.TemporaryDirectory() as temp:
            removed, control = Path(temp) / "removed", Path(temp) / "control"
            await economic_run.run_new(raw, removed, until=7)
            await economic_run.run_new(dict(raw, link_withdrawal_tick=None), control, until=7)
            action, baseline = relation_table(removed, 7), relation_table(control, 7)
            self.assertTrue(any(r["operation"] == "delete" for r in action["communication_graph"]["applied"]))
            self.assertFalse(
                any(r["operation"] == "delete" for r in baseline["communication_graph"]["applied"])
            )
            self.assertTrue(action["labor_graph"]["wage_obligations"])
            self.assertEqual(action["labor_graph"], baseline["labor_graph"])

    async def test_failed_publication_and_modified_projection_leave_the_history_rebuildable(self):
        with tempfile.TemporaryDirectory() as temp:
            root, output = Path(temp) / "switch", Path(temp) / "research"
            finished = await economic_run.run_new(config("labor-switch"), root)
            original = economic_run.atomic_json

            def fail_relations(path, body):
                if Path(path).name == "relations.json":
                    raise OSError("labor projection publication failed")
                return original(path, body)

            with patch.object(economic_run, "atomic_json", side_effect=fail_relations):
                with self.assertRaisesRegex(OSError, "publication failed"):
                    economic_run.export_projection(root, destination=output, tick=5)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(temp).glob(".projection-*")), [])
            expected = economic_run.export_projection(root, destination=output, tick=5)
            with self.assertRaisesRegex(ValueError, "different cursor"):
                economic_run.export_projection(root, destination=output, tick=6)
            (output / "relations.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "modified"):
                economic_run.export_projection(root, destination=output, tick=5)
            rebuilt = economic_run.export_projection(root, destination=Path(temp) / "rebuilt", tick=5)
            self.assertEqual(rebuilt["files"], expected["files"])
            _, _, head = economic_run.open_run(root)
            self.assertEqual(head.state_hash, finished["state_hash"])

    async def test_historical_resume_replay_and_source_corruption_gate(self):
        raw = config("rehire")
        del raw["labor_transfer"]["rehire"]["minimum_trust_bps"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "rehire"
            stopped = await economic_run.run_new(raw, root, until=5)
            prior = graph(root, 5)
            finished = await economic_run.resume_run(root)
            self.assertEqual((stopped["tick"], finished["tick"]), (5, 8))
            self.assertEqual(graph(root, 5), prior)
            now = graph(root, 6)
            self.assertEqual(now["opportunity_counts"]["worked"], 1)
            self.assertEqual(now["contracts"][-1]["employer_id"], "firm:00")
            replay = await economic_run.replay_run(root, destination=Path(temp) / "replayed")
            self.assertEqual((replay["status"], replay["state_hash"]), ("matched", finished["state_hash"]))
            _, store, marker = economic_run.open_run(root, tick=5)
            segment = store.segment_path(marker.segment)
            original = segment.read_bytes()
            output = Path(temp) / "bad-source"
            try:
                segment.write_bytes(b"malformed source")
                with self.assertRaisesRegex(ValueError, "Segment byte hash mismatch"):
                    economic_run.export_projection(root, destination=output, tick=5)
                self.assertFalse(output.exists())
            finally:
                segment.write_bytes(original)
            self.assertEqual(graph(root, 5), prior)
