"""Static admission, fee immutability and recovery through the public runner."""
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

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/closed-economy-partner-static.json"
FOCAL, INCUMBENT, BETTER = ("household:0001", "household:0002", "household:0005")


def graph(snapshot):
    return {(e["source_id"], e["target_id"])
            for e in snapshot.data["closed_social"]["network"]["communication"]}


def fees(snapshot):
    return [e for e in snapshot.data["economic_runtime"]["journal"] if e["kind"] == "link_fee"]


class CooperationPartnerStaticTests(unittest.IsolatedAsyncioTestCase):
    def test_fixed_graph_requires_separate_incumbent_history(self):
        raw = json.loads(EXAMPLE.read_text())
        ranking = raw["base_config"]["cooperation"]["followup"]["partner_choice"]["trust_ranking"]
        ranking["incumbent_trials"][0]["alternate_index"] = 5
        with self.assertRaisesRegex(ValidationError, "three binary trials for the incumbent"):
            LaborPilotSpec.model_validate(raw)

    async def test_shared_admission_and_recovery_preserve_graph_and_real_fees(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cases = {}
            for arm in ("static", "ranked"):
                raw = json.loads(EXAMPLE.read_text())
                raw["base_config"]["cooperation"]["followup"]["partner_choice"]["pin_incumbent"] = arm == "static"
                cfg = LaborPilotSpec.model_validate(raw).base_config
                cases[arm] = create_run(cfg, root / arm, terminal_tick=21)
            initial = graph(cases["static"][0].snapshot)
            await asyncio.gather(*(world.run(18) for world, _ in cases.values()))
            self.assertEqual(cases["static"][0].observe(FOCAL), cases["ranked"][0].observe(FOCAL))
            await asyncio.gather(*(world.step() for world, _ in cases.values()))
            for world, _ in cases.values():
                self.assertEqual(graph(world.snapshot), initial)
                self.assertFalse(fees(world.snapshot))
                current = world.snapshot.data["closed_social"]["current"][FOCAL]["partner_performance_trust"]
                self.assertEqual(len(current[INCUMBENT]["used_roots"]), 3)

            original = CommitStore.fault
            for arm, (world, store) in cases.items():
                path = root / arm
                baseline = world.snapshot
                def interrupt(target, point):
                    if target.root == path and point == "before_marker":
                        raise OSError("partner boundary interrupted")
                    original(target, point)

                with patch.object(CommitStore, "fault", interrupt):
                    with self.assertRaisesRegex(OSError, "partner boundary interrupted"):
                        await world.step()
                self.assertEqual(store.load()[1], baseline)
                self.assertEqual(world.snapshot, baseline)
                self.assertFalse([e for e in world.scenario.economy.journal if e.kind == "link_fee"])

            await asyncio.gather(*(resume_run(root / arm) for arm in cases))
            for arm, expected in (("static", INCUMBENT), ("ranked", BETTER)):
                restored, _, cursor = open_run(root / arm)
                self.assertEqual(cursor.tick, 21)
                self.assertEqual((graph(restored.snapshot) == initial), arm == "static")
                final_round = restored.snapshot.data["closed_economy"]["cooperation_followup_round"]
                self.assertEqual(final_round["seats"], [FOCAL, expected])
                self.assertEqual(final_round["status"], "settled")
                self.assertEqual(final_round["choices"][expected]["action"],
                                 "defect" if arm == "static" else "cooperate")
                self.assertEqual([(f["tick"], f["payload"]["amount_minor"]) for f in fees(restored.snapshot)],
                                 [] if arm == "static" else [(20, 3)])
                replay = await replay_run(root / arm, destination=root / ("replay-" + arm))
                self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 21))
