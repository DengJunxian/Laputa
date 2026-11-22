from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from civitas.application import economic_run
from civitas.storage.commit_store import CommitStore


class DemandChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_hiring_cut_keeps_budget_and_employment_changes_after_acceptance(self):
        base = {"ticks": 5, "fault_tick": None, "households": 4,
                "labor_transfer": {"worker_index":1, "employer_index":0, "offer_tick":4,
                                   "duration_ticks":1, "min_previous_sales_units":1},
                "demand": {"initially_unemployed_indices":[1], "peer_uplift_units":1}}
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for active in (True, False):
                raw = {**base, "demand":{**base["demand"], "hiring_adapts": active}}
                await economic_run.run_new(raw,root/str(active))
            _, on = CommitStore(root/"True").load()
            _, off = CommitStore(root/"False").load()
            self.assertEqual(on.data["closed_economy"]["population"],off.data["closed_economy"]["population"])
            for state, expected in ((on,[False]*4+[True]), (off,[False]*5)):
                work = state.data["closed_economy"]["labor_events"]
                self.assertEqual([any(e["worker_id"]=="household:0001" and e["tick"]==t for e in work)
                                  for t in range(1,6)],expected)
            self.assertTrue(any(m.body.get("kind")=="disclosed_consumption" for m in on.messages))
            self.assertTrue(any(d.belief_patch.get("closed_choice",{}).get("peer_uplift_units")
                                for d in on.decisions))

    async def test_same_consumption_receipt_cannot_be_disclosed_under_another_id(self):
        from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy

        class DuplicateDisclosurePolicy(ClosedEconomyPolicy):
            async def decide(self, observation):
                decision = await super().decide(observation)
                copies = [draft.model_copy(update={'message_id': draft.message_id + ':copy'})
                          for draft in decision.message_drafts
                          if draft.body.get('kind') == 'disclosed_consumption']
                return decision.model_copy(update={'message_drafts': [*decision.message_drafts, *copies]})

        with TemporaryDirectory() as tmp:
            world, store = economic_run.create_run(
                {'ticks': 4, 'households': 4, 'fault_tick': None, 'demand': {'peer_uplift_units': 1}},
                Path(tmp)/'run')
            world.policy = DuplicateDisclosurePolicy(world.scenario.config, world.rng)
            await world.step()
            before = world.snapshot
            with self.assertRaisesRegex(ValueError, 'Unauthorized consumption disclosure'):
                await world.step()
            self.assertEqual(world.snapshot, before)
            self.assertEqual(store.markers()[-1].tick, 1)
