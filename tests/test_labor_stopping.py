"""A shared hidden ending preserves pending labor and the assigned denominator."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.economic_run import create_run, replay_run, resume_run
from civitas.contracts.base import content_hash
from civitas.experiments import registry
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-labor-matched-attempts.json"


def spec(*seeds):
    raw = json.loads(EXAMPLE.read_text())
    raw.update(schema_version=4, seeds=list(seeds))
    raw["base_config"]["stopping_hazard_bps"] = 1250
    return raw


class LaborStoppingTests(unittest.IsolatedAsyncioTestCase):
    async def test_paired_stop_at_offer_keeps_contract_pending_and_seals_both_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            report = await run_pilot(root, spec(251))
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["comparison"]["matched_pairs"], 1)
            self.assertEqual(report["comparison"]["all_assigned_bounds"], [0, 0])
            for row in report["runs"]:
                self.assertEqual(row["stopping"]["tick"], 9)
                self.assertEqual(row["stopping"]["reason"], "geometric")
                self.assertEqual(row["window_status"], "censored")
                self.assertEqual(row["signed_pending_at_stop"], 1)
                self.assertEqual((row["switch_accepted"], row["worked"], row["second_window_fee_minor"]), (1, 0, 0))
                run = root / row["relative_path"]
                self.assertEqual(CommitStore(run).verified_markers()[-1].tick, 9)
                with self.assertRaisesRegex(ValueError, "horizon"):
                    await resume_run(run, until=10)

    async def test_future_cutoff_does_not_change_any_actor_view_or_committed_prefix(self):
        raw = spec(43)["base_config"] | {"root_seed": 43}
        actors = [f"household:{i:04d}" for i in range(9)] + [f"firm:{i:02d}" for i in range(3)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            short, short_store = create_run(raw, root / "short", terminal_tick=9)
            long, long_store = create_run(raw, root / "long", terminal_tick=11)
            self.assertEqual(short_store.identity.config_hash, long_store.identity.config_hash)
            self.assertNotEqual(
                short_store.identity.metadata["terminal_tick"], long_store.identity.metadata["terminal_tick"]
            )
            for tick in range(10):
                for actor in actors:
                    view = short.observe(actor)
                    self.assertEqual(view, long.observe(actor))
                    rules = [f.value for f in view.market_view if f.value.get("kind") == "experiment_stopping_rule"]
                    self.assertEqual(
                        rules,
                        [
                            {"kind": "experiment_stopping_rule", "hazard_bps": 1250, "administrative_cap_tick": 11},
                        ],
                    )
                self.assertEqual(content_hash(short.snapshot), content_hash(long.snapshot))
                if tick < 9:
                    await short.step()
                    await long.step()
            before = content_hash(short.snapshot)
            with self.assertRaisesRegex(ValueError, "after the assigned terminal tick"):
                await short.step()
            self.assertEqual(content_hash(short.snapshot), before)
            self.assertEqual(short_store.verified_markers()[-1].tick, 9)
            await long.step()
            self.assertEqual(long_store.verified_markers()[-1].tick, 10)

    async def test_never_reached_and_observed_windows_keep_separate_denominators(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            report = await run_pilot(root, spec(241, 277))
            self.assertEqual(analyze_pilot(root), report)
            rows = {(r["seed"], r["arm"]): r for r in report["runs"]}
            self.assertEqual(report["status"], "completed")
            for arm in report["arms"]:
                early, observed = rows[241, arm], rows[277, arm]
                self.assertEqual((early["stopping"]["tick"], early["window_status"]), (3, "not_reached"))
                self.assertEqual((early["choices"], early["opportunity_hash"], early["worked"]), ([], None, 0))
                self.assertEqual((observed["stopping"]["tick"], observed["window_status"]), (10, "observed"))
                self.assertEqual(report["arms"][arm]["window_status_counts"], {"not_reached": 1, "observed": 1})
                self.assertEqual(report["arms"][arm]["no_visible_offer_worlds"], 0)
            path = root / "plan.json"
            plan = json.loads(path.read_text())
            plan["plan"]["stopping"]["241"]["tick"] = 4
            plan["plan_hash"] = content_hash(plan["plan"])
            path.write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, "Frozen stopping schedule"):
                analyze_pilot(root)

    async def test_failure_before_the_terminal_commit_is_not_a_censored_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            original_fault = CommitStore.fault

            def fail_terminal(store, point):
                if point == "before_marker" and store.root.name == "random" and store.marker_path(8).is_file():
                    raise OSError("terminal marker interrupted")
                original_fault(store, point)

            with patch.object(CommitStore, "fault", fail_terminal):
                failed = await run_pilot(root, spec(251))
            interrupted = failed["runs"][1]
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["comparison"]["all_assigned_bounds"], [-1, 0])
            self.assertIsNone(interrupted["worked"])
            self.assertEqual(interrupted["periods"][-1]["tick"], 8)
            self.assertEqual(interrupted["stopping"]["tick"], 9)
            self.assertNotIn("window_status", interrupted)
            recovered = await run_pilot(root)
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["runs"][0], failed["runs"][0])
            self.assertEqual(recovered["runs"][1]["periods"][:9], interrupted["periods"])
            self.assertEqual(recovered["runs"][1]["window_status"], "censored")
            self.assertEqual(recovered["comparison"]["all_assigned_bounds"], [0, 0])
            attempts = [r["status"] for r in registry.history(root) if r["job"] == "seed-251/random"]
            self.assertEqual(attempts, ["running", "failed", "running", "finished"])
            replay = await replay_run(root / "seed-251/random", destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 9))
