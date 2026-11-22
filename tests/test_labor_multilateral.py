"""Paired choice among two lawful alternatives in one labor window."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application.closed_economy import build_closed_economy
from civitas.application.economic_run import replay_run
from civitas.contracts.economic import LaborAcceptIntent
from civitas.core.rng import AddressedRNG
from civitas.experiments import registry
from civitas.experiments.labor_factorial import analyze_pilot, run_pilot
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-labor-random-control.json"


def spec(seed, **changes):
    raw = json.loads(EXAMPLE.read_text())
    raw.update(schema_version=3, seeds=[seed])
    config = raw["base_config"]
    config.update(firms=3, households=9, **changes)
    window = config["labor_transfer"]["second_rehire"]
    window.pop("minimum_trust_bps")
    window["additional_alternative_employer_index"] = 2
    return raw


class MultilateralLaborTests(unittest.IsolatedAsyncioTestCase):
    async def test_matched_attempts_can_choose_different_employers(self):
        with tempfile.TemporaryDirectory() as temp:
            report = await run_pilot(Path(temp) / "pilot", spec(43))
        self.assertEqual(report["status"], "completed")
        selective, random = report["runs"]
        self.assertEqual(selective["opportunity_hash"], random["opportunity_hash"])
        self.assertEqual(selective["pretreatment_hash"], random["pretreatment_hash"])
        self.assertEqual((selective["switch_attempted"], random["switch_attempted"]), (1, 1))
        self.assertEqual((selective["switch_offer_visible"], random["switch_offer_visible"]), (2, 2))
        self.assertEqual((selective["work"]["employer_id"], random["work"]["employer_id"]), ("firm:00", "firm:02"))
        self.assertEqual((selective["second_window_fee_minor"], random["second_window_fee_minor"]), (3, 3))
        self.assertEqual(len(random["random_draw"]["candidate_offer_ids"]), 2)
        self.assertEqual(selective["chosen_score"], {"a": 3, "b": 1})
        self.assertIsNone(random["chosen_score"])
        self.assertEqual(report["comparison"]["mean"], 0)

    async def test_rejected_switch_consumes_attempt_and_incumbent_cannot_be_accepted(self):
        class ExtraCommands(ClosedEconomyPolicy):
            async def decide(self, observation):
                bundle = await super().decide(observation)
                if bundle.tick != 9 or bundle.actor_id != "household:0001":
                    return bundle
                original = next(i for i in bundle.economic_intents if isinstance(i, LaborAcceptIntent))
                return bundle.model_copy(
                    update={
                        "economic_intents": [
                            LaborAcceptIntent(intent_id="incumbent", offer_id="labor-switch:9:household:0001:firm:01"),
                            original.model_copy(update={"max_switch_fee_minor": 0}),
                            LaborAcceptIntent(
                                intent_id="try-second",
                                offer_id="labor-switch:9:household:0001:firm:02",
                                max_switch_fee_minor=3,
                            ),
                        ]
                    }
                )

        config = ClosedEconomyConfig.model_validate(spec(43)["base_config"] | {"root_seed": 43})
        world = build_closed_economy(config, policy=ExtraCommands(config, AddressedRNG(43, 0)))
        await world.run(10)
        receipts = {r.command_id: r.reason_codes for r in world.snapshot.receipts}
        self.assertEqual(receipts["command:incumbent"], ["labor_switch_required"])
        self.assertEqual(receipts["command:intent:9:household:0001:labor"], ["labor_switch_fee_not_authorized"])
        self.assertEqual(receipts["command:try-second"], ["labor_switch_attempt_limit"])
        self.assertIsNone(world.snapshot.data["closed_economy"]["second_rehire_contract"])
        self.assertEqual([row.tick for row in world.scenario.economy.journal if row.kind == "labor_switch_fee"], [7])

    async def test_no_visible_alternative_retains_both_assigned_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            report = await run_pilot(Path(temp) / "pilot", spec(43, disabled_actions=["purchase"]))
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["comparison"]["all_assigned_bounds"], [0, 0])
        for row in report["runs"]:
            self.assertEqual((len(row["choices"]), row["switch_offer_visible"], row["switch_attempted"]), (3, 0, 0))
            self.assertIsNone(row["random_draw"])
        self.assertEqual([arm["allocated_switch_slots"] for arm in report["arms"].values()], [1, 1])

    async def test_recovery_preserves_draw_quota_and_settles_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "pilot"
            original_fault = CommitStore.fault

            def fail_before_work(store, point):
                if point == "before_marker" and store.root.name == "random" and store.marker_path(9).is_file():
                    raise OSError("interrupted third-employer settlement")
                original_fault(store, point)

            with patch.object(CommitStore, "fault", fail_before_work):
                failed = await run_pilot(root, spec(43))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["comparison"]["all_assigned_bounds"], [0, 1])
            interrupted = failed["runs"][1]
            self.assertEqual(interrupted["periods"][-1]["tick"], 9)
            self.assertIsNone(interrupted["worked"])
            self.assertTrue(interrupted["random_draw"]["selected_offer_id"].endswith("firm:02"))
            recovered = await run_pilot(root)
            self.assertEqual(analyze_pilot(root), recovered)
            self.assertEqual(recovered["runs"][0], failed["runs"][0])
            row = recovered["runs"][1]
            self.assertEqual(row["periods"][:10], interrupted["periods"])
            self.assertEqual(row["random_draw"], interrupted["random_draw"])
            self.assertEqual((row["switch_worked"], row["second_window_fee_minor"]), (1, 3))
            self.assertEqual(recovered["comparison"]["all_assigned_bounds"], [0, 0])
            attempts = [r["status"] for r in registry.history(root) if r["job"] == "seed-43/random"]
            self.assertEqual(attempts, ["running", "failed", "running", "finished"])
            replay = await replay_run(root / "seed-43/random", destination=Path(temp) / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 11))

    def test_duplicate_or_out_of_range_alternative_rejected(self):
        for duplicate in (0, 1, 3):
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(ValueError, "distinct alternative"):
                fields = spec(43)["base_config"]
                fields["labor_transfer"]["second_rehire"]["additional_alternative_employer_index"] = duplicate
                ClosedEconomyConfig.model_validate(fields)
