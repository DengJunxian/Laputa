"""Private continuation noise on the existing world, decision and journal paths."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from civitas.application.economic_run import create_run, open_run, replay_run, resume_run
from civitas.experiments.labor_factorial import LaborPilotSpec
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-partner-noise-zero.json"
FOCAL, INCUMBENT = "household:0001", "household:0002"


def spec(dose):
    raw = json.loads(EXAMPLE.read_text())
    follow = raw["base_config"]["cooperation"]["followup"]
    follow["partner_noise_bps"] = dose
    follow["partner_choice"]["pin_incumbent"] = True
    return raw


def followup(snapshot):
    return snapshot.data["closed_economy"]["cooperation_followup_round"]


class CooperationPartnerNoiseTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_zero_requires_version_13_and_noise_is_bounded(self):
        old = spec(0)
        old["schema_version"] = 12
        with self.assertRaisesRegex(ValidationError, "explicit continuation noise dose"):
            LaborPilotSpec.model_validate(old)
        with self.assertRaises(ValidationError):
            LaborPilotSpec.model_validate(spec(10001))

    async def test_noise_reaches_only_the_assigned_candidate_and_recovers_settlement(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve()
            cases = {
                dose: create_run(LaborPilotSpec.model_validate(spec(dose)).base_config,
                                 path / str(dose), terminal_tick=21)
                for dose in (0, 10000)
            }
            await asyncio.gather(*(w.run(19) for w, _ in cases.values()))
            left, right = (w for w, _ in cases.values())
            self.assertEqual(left.snapshot, right.snapshot)
            for actor in (FOCAL, INCUMBENT, "household:0005"):
                self.assertEqual(left.observe(actor), right.observe(actor))
            await asyncio.gather(*(w.step() for w, _ in cases.values()))
            for dose, (world, _) in cases.items():
                expected = "defect" if dose == 0 else "cooperate"
                self.assertEqual(followup(world.snapshot)["choices"][INCUMBENT]["action"], expected)
                self.assertEqual(followup(world.snapshot)["choices"][FOCAL]["action"], "cooperate")
                offer, = [f for f in world.observe(INCUMBENT).market_view if "assigned_action" in f.value]
                self.assertEqual((offer.value["assigned_action"], offer.owner_id, offer.public),
                                 (expected, INCUMBENT, False))
                for actor in (FOCAL, "household:0005"):
                    self.assertFalse(any("assigned_action" in f.value for f in world.observe(actor).market_view))
            world, store = cases[10000]
            original = CommitStore.fault
            before = store.load()

            def fail(target, point):
                if target.root == path / "10000" and point == "before_marker":
                    raise OSError("noisy settlement interrupted")
                original(target, point)

            with patch.object(CommitStore, "fault", fail):
                with self.assertRaisesRegex(OSError, "noisy settlement"):
                    await world.step()
            self.assertEqual(store.load(), before)
            self.assertEqual(world.snapshot, before[1])
            await resume_run(path / "10000")
            recovered, _, cursor = open_run(path / "10000")
            self.assertEqual(cursor.tick, 21)
            records = [e for e in recovered.scenario.economy.journal if e.kind == "cooperation_settlement" and e.tick == 21]
            receipt, = records
            self.assertEqual(receipt.payload["contributions"], {FOCAL: 4, INCUMBENT: 4})
            self.assertFalse([e for e in recovered.scenario.economy.journal if e.kind == "link_fee"])
            results = await replay_run(path / "10000", destination=path / "replay")
            self.assertEqual((results["status"], results["compared_ticks"]), ("matched", 21))
