import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.experiments.closed_economy import audit_snapshot


class FiscalPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_announcement_budget_paid_wage_tax_and_transfers_replay(self):
        config = {'ticks': 3, 'households': 4, 'fiscal': {
            'initial_cash_minor': 5, 'announced_tick': 1, 'effective_tick': 2,
            'audience': [0, 1, 2], 'disclosure_cost_minor': 2,
            'brackets': [{'start_minor': 0, 'rate_bps': 2500}, {'start_minor': 6, 'rate_bps': 5000}],
            'transfer_recipients': [0, 1], 'transfer_minor': 12}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'fiscal'
            await economic_run.run_new(config, root, until=1)
            world, _, _ = economic_run.open_run(root)
            for i in range(4):
                actor = f'household:{i:04d}'
                kinds = [x.value.get('kind') for x in world.observe(actor).market_view]
                self.assertEqual('fiscal_announcement' in kinds, i < 2)
                self.assertNotIn('fiscal_settlement', kinds)
            await economic_run.resume_run(root, until=2)
            world, _, _ = economic_run.open_run(root)
            announcement, settlement = [e.details for e in world.scenario.economy.journal if e.kind == 'fiscal']
            self.assertEqual((announcement['disclosure_paid_minor'], announcement['tax_collected_minor']), (4, 0))
            self.assertEqual(settlement['tax_collected_minor'], 16)
            self.assertEqual(settlement['transfers_minor'], {'household:0000': 9, 'household:0001': 8})
            self.assertEqual(settlement['treasury_cash_minor'], 0)
            self.assertEqual(audit_snapshot(world.snapshot)['status'], 'passed')
            replay = await economic_run.replay_run(root, destination=root.parent / 'fiscal-replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 2))

    def test_each_marginal_bracket_negative_zero_and_rounding(self):
        from civitas.economy.fiscal import TaxBracket, progressive_tax

        brackets = [TaxBracket(start_minor=0, rate_bps=2500), TaxBracket(start_minor=6, rate_bps=5000)]
        self.assertEqual([progressive_tax(x, brackets) for x in (-7, 0, 1, 6, 7, 12, 13)],
                         [0, 0, 0, 1, 2, 4, 5])

    async def test_a_paid_wage_tax_announcement_affects_only_reached_budget_before_effect(self):
        from civitas.application.closed_economy import build_closed_economy

        cfg = {'ticks': 2, 'households': 4, 'cash_jitter_minor': 0, 'fiscal': {
            'initial_cash_minor': 2, 'announced_tick': 1, 'effective_tick': 2, 'audience': [0],
            'disclosure_cost_minor': 2, 'brackets': [{'start_minor': 0, 'rate_bps': 5000}],
            'transfer_recipients': [], 'precautionary_bps': 10000}}
        world = build_closed_economy(cfg)
        await world.step()
        choices = {r.actor_id: r.belief_patch for r in world.snapshot.decisions}
        self.assertEqual(choices['household:0000']['fiscal_precaution']['reserve_minor'], 6)
        self.assertNotIn('fiscal_precaution', choices['household:0001'])
        self.assertEqual(world.scenario.economy.journal[-1].kind, 'consumption')
        await world.step()
        self.assertEqual([j.details['tax_collected_minor'] for j in world.scenario.economy.journal if j.kind == 'fiscal'],
                         [0, 24])
