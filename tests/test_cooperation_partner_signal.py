"""Observable holdout admission and atomic settlement recovery."""
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

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FOCAL, CANDIDATE = "household:0001", "household:0005"


def spec(rule):
    return json.loads((EXAMPLES / f"closed-economy-partner-signal-{rule}.json").read_text())


def settlement(snapshot):
    return [e for e in snapshot.data["economic_runtime"]["journal"]
            if e["kind"] == "cooperation_settlement" and e["tick"] == 15]


class CooperationPartnerSignalTests(unittest.IsolatedAsyncioTestCase):
    def test_signal_regime_requires_distinct_histories_and_a_new_version(self):
        obsolete = spec("recent")
        obsolete["schema_version"] = 10
        with self.assertRaisesRegex(ValidationError, "exclusively for pilot v11"):
            LaborPilotSpec.model_validate(obsolete)
        duplicate = spec("cumulative")
        trials = duplicate["base_config"]["cooperation"]["followup"]["partner_choice"]["trust_ranking"]["trials"]
        trials[0]["action"] = "defect"
        with self.assertRaisesRegex(ValidationError, "opposing cumulative and recent signals"):
            LaborPilotSpec.model_validate(duplicate)

    async def test_assignment_is_private_until_exchange_and_settlement_recovers_once(self):
        self.maxDiff = None
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            worlds = {}
            for rule in ("cumulative", "recent"):
                config = LaborPilotSpec.model_validate(spec(rule)).base_config
                worlds[rule] = create_run(config, root / rule, terminal_tick=15)
            await asyncio.gather(*(world.run(12) for world, _ in worlds.values()))
            for actor in (FOCAL, CANDIDATE):
                left, right = (world.observe(actor) for world, _ in worlds.values())
                self.assertEqual(left, right)
            await asyncio.gather(*(world.step() for world, _ in worlds.values()))
            for actor in (FOCAL, CANDIDATE):
                left, right = (world.observe(actor) for world, _ in worlds.values())
                self.assertEqual(left, right)
            await asyncio.gather(*(world.step() for world, _ in worlds.values()))
            for rule, (world, store) in worlds.items():
                before = world.snapshot
                marker, _ = store.load()
                self.assertEqual(marker.tick, 14)
                following = before.data["closed_economy"]["cooperation_followup_round"]
                self.assertEqual(following["seats"], [FOCAL, CANDIDATE])
                self.assertEqual(following["choices"][FOCAL]["action"], "cooperate")
                expected = "cooperate" if rule == "cumulative" else "defect"
                self.assertEqual(following["choices"][CANDIDATE]["action"], expected)
                own, = [fact for fact in world.observe(CANDIDATE).market_view if "assigned_action" in fact.value]
                self.assertEqual((own.owner_id, own.public, own.visible_at.tick, own.value["assigned_action"]),
                                 (CANDIDATE, False, 14, expected))
                for actor in (FOCAL, "household:0007"):
                    self.assertFalse(any("assigned_action" in fact.value for fact in world.observe(actor).market_view))
                decision = next(d for d in before.decisions if d.tick == 14 and d.actor_id == CANDIDATE)
                self.assertIn("cooperation:14", decision.evidence_ids)
                self.assertFalse(any(ref.startswith("cooperation-outcome:") for ref in decision.evidence_ids))
                self.assertFalse(settlement(before))
            path = root / "cumulative"
            world, store = worlds["cumulative"]
            before = world.snapshot
            original = CommitStore.fault

            def stop_at_settlement(store, point):
                if store.root == path and point == "before_marker":
                    raise OSError("holdout settlement interrupted")
                original(store, point)

            with patch.object(CommitStore, "fault", stop_at_settlement):
                with self.assertRaisesRegex(OSError, "holdout settlement"):
                    await world.step()
            self.assertEqual(store.load()[1], before)
            self.assertEqual(world.snapshot, before)
            self.assertFalse([e for e in world.scenario.economy.journal
                              if e.kind == "cooperation_settlement" and e.tick == 15])
            await resume_run(path)
            restored, _, marker = open_run(path)
            self.assertEqual(marker.tick, 15)
            entry, = settlement(restored.snapshot)
            self.assertEqual(entry["payload"]["contributions"], {FOCAL: 4, CANDIDATE: 4})
            fee, = [e for e in restored.scenario.economy.journal if e.kind == "link_fee"]
            self.assertEqual((fee.tick, fee.payload["amount_minor"]), (14, 3))
            replay = await replay_run(path, destination=root / "replay")
            self.assertEqual((replay["status"], replay["compared_ticks"]), ("matched", 15))
