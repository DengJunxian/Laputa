import json
from math import sqrt
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from civitas.application import economic_run
from civitas.economy.fiscal import TREASURY
from civitas.experiments.bank_policy_pilot import freeze_pilot, run_pilot, summarize_pilot
from civitas.storage.commit_store import CommitStore, atomic_json


INTERBANK = Path(__file__).resolve().parents[1]/'examples/closed-economy-interbank-resolution.json'


class BankPolicyPilotTests(unittest.TestCase):
    def test_fixed_dose_tracks_maturity_at_one_bank_and_depositor_loss_at_another(self):
        cfg = json.loads(INTERBANK.read_text())
        with TemporaryDirectory() as tmp:
            report = run_pilot(Path(tmp)/'mechanism', base=cfg, seeds=[cfg['root_seed']], through=8)
        self.assertEqual(report['full_dose_primary_roots'], 1)
        rows = {key.split('/')[1]: row for key, row in report['rows'].items()}
        self.assertEqual(rows['D1S1']['bank_id'], 'bank:b')
        self.assertEqual(rows['D1S1']['withdrawal_bank_id'], 'bank:a')
        for disclosure in (0, 1):
            control, support = (rows[f'D{disclosure}S{s}'] for s in (0, 1))
            self.assertEqual(support['treasury_support']['bank_repaid_minor'], 9)
            self.assertEqual(support['treasury_support']['public_net_cash_outlay_minor'], 0)
            self.assertEqual(control['terminal_deposit_loss_minor'] - support['terminal_deposit_loss_minor'], 8)
            self.assertEqual({e['bank'] for e in support['exposures']}, {'bank:a'} if disclosure else set())
            self.assertEqual(support['replay']['compared_ticks'], 8)

    def test_underfunded_support_and_unresolved_claims_keep_all_assignments(self):
        cfg = json.loads(INTERBANK.read_text())
        cfg['fiscal']['initial_cash_minor'] = 10
        cfg['banking']['operations'] = [op for op in cfg['banking']['operations'] if op['tick'] < 8]
        with TemporaryDirectory() as tmp:
            report = run_pilot(Path(tmp)/'unresolved', base=cfg, seeds=[cfg['root_seed']], through=7)
        self.assertEqual(report['world_statuses'], {'completed': 4})
        self.assertEqual(report['assigned_worlds'], 4)
        self.assertEqual(report['observed_primary_roots'], 0)
        for assignment, row in report['rows'].items():
            with self.subTest(assignment=assignment):
                self.assertIsNone(row['deposit_loss_rate'])
                self.assertEqual(row['terminal_deposit_loss_minor'], 0)
                self.assertGreater(row['unresolved_receivership_deposits_minor'], 0)
                self.assertEqual(row['replay']['compared_ticks'], 7)
                attempted = assignment.endswith('S1')
                self.assertEqual(row['manipulation_issues'], ['support_not_fully_disbursed'] if attempted else [])
                self.assertEqual([event['reason'] for event in row['bank_policy_rejections']],
                                 ['insufficient_treasury_cash'] if attempted else [])

    def test_policy_interaction_uses_assigned_roots_and_retains_absent_or_undelivered_arms(self):
        arms = ('D0S0', 'D0S1', 'D1S0', 'D1S1')
        plan = {'seeds': [11, 13, 17], 'contrast_weights': dict(zip(arms, [1, -1, -1, 1])),
                'primary_outcome': 'deposit_loss_rate'}
        rows = {}
        for seed, values in ((11, (.10, .08, .15, .06)), (13, (.20, .10, .20, .10)),
                             (17, (.20, .10, .20, .10))):
            for arm, value in zip(arms, values):
                rows[f'{seed}/{arm}'] = {'status': 'completed', 'pre_policy_state_hash': str(seed),
                                        'deposit_loss_rate': value, 'manipulation_issues': []}
        rows['13/D1S1']['manipulation_issues'] = ['support_not_fully_disbursed']
        rows['17/D0S1'] = {'status': 'failed', 'error': 'commit interruption'}
        report = summarize_pilot(plan, rows)
        self.assertEqual((report['assigned_roots'], report['assigned_worlds']), (3, 12))
        self.assertEqual((report['observed_primary_roots'], report['full_dose_primary_roots']), (2, 1))
        self.assertAlmostEqual(report['blocks'][0]['primary_interaction'], -.07)
        self.assertEqual(report['blocks'][1]['primary_interaction'], 0)
        self.assertIsNone(report['blocks'][2]['primary_interaction'])
        self.assertAlmostEqual(report['primary_sample_sd'], .07/sqrt(2))
        self.assertAlmostEqual(report['nondegenerate_primary_mcse'], .035)
        self.assertEqual(report['world_statuses']['failed'], 1)
        self.assertEqual(report['rows']['13/D1S1']['manipulation_issues'], ['support_not_fully_disbursed'])

    def test_resume_runs_only_unstarted_worlds_and_keeps_the_missing_block(self):
        fixture = Path(__file__).resolve().parents[1]/'examples/closed-economy-bank-policy-combination.json'
        plan = freeze_pilot(json.loads(fixture.read_text()), [19], 5)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic_json(root/'plan.json', plan)
            started = root/'receipts/19/D1S1/started.json'
            started.parent.mkdir(parents=True)
            started.write_text('{"interruption":"before first commit"}\n')
            before = run_pilot(root, analyze_only=True)
            self.assertEqual(before['world_statuses'], {'not_run': 3, 'interrupted': 1})
            result = run_pilot(root, resume=True)
            self.assertEqual(result['world_statuses'], {'completed': 3, 'interrupted': 1})
            self.assertEqual(result['assigned_worlds'], 4)
            self.assertEqual(result['observed_primary_roots'], 0)
            self.assertIn('before first commit', started.read_text())
            self.assertFalse((root/'worlds/19/D1S1').exists())
            self.assertEqual(run_pilot(root, analyze_only=True), result)
            self.assertEqual(run_pilot(root, resume=True), result)

    def test_pilot_counts_liquidation_loss_at_cutoff_separately_from_unpaid_requests(self):
        fixture = Path(__file__).resolve().parents[1]/'examples/closed-economy-bank-policy-combination.json'
        cfg = json.loads(fixture.read_text())
        cfg['banking']['loan_to_capital_multiple'] = 70
        cfg['banking']['operations'].extend([
            {'tick': 1, 'operation_id': 'loss-control-loan', 'kind': 'approve', 'bank': 'bank:b',
             'actor': 'household:0003', 'amount_minor': 200, 'installments': 1, 'default_after_misses': 1},
            {'tick': 2, 'operation_id': 'loss-control-transfer', 'kind': 'payment', 'bank': 'bank:b',
             'actor': 'household:0003', 'target_bank': 'bank:b', 'target_actor': 'household:0002',
             'amount_minor': 200},
            {'tick': 4, 'operation_id': 'loss-control-release', 'kind': 'release', 'loan_id': 'loss-control-loan'},
            {'tick': 4, 'operation_id': 'loss-control-resolve', 'kind': 'resolve', 'bank': 'bank:b'},
        ])
        with TemporaryDirectory() as tmp:
            report = run_pilot(Path(tmp)/'positive', base=cfg, seeds=[19], through=5)
            self.assertEqual(report['full_dose_primary_roots'], 1)
            self.assertEqual(report['primary_mean'], 0)
            for row in report['rows'].values():
                self.assertEqual(row['terminal_deposit_loss_minor'], 197)
                self.assertEqual(row['initial_deposits_minor'], 12)
                self.assertAlmostEqual(row['deposit_loss_rate'], 197/12)
                self.assertNotEqual(row['withdrawal_remaining_minor'], 197)


class InterbankRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_liquidation_preserves_partial_collection_and_two_round_cooperation(self):
        cfg = json.loads(INTERBANK.read_text())
        # Remove the scheduled inflow to leave the Treasury advance partly unpaid.
        cfg['banking']['operations'] = [op for op in cfg['banking']['operations']
                                        if op['operation_id'] != 'borrower-bank-inflow']
        cfg['banking']['operations'].append({
            'tick': 4, 'operation_id': 'try-spending-queued-deposit', 'kind': 'payment',
            'bank': 'bank:a', 'actor': 'household:0000', 'target_bank': 'bank:a',
            'target_actor': 'household:0002', 'amount_minor': 6,
        })
        cfg['cooperation'] = {'offer_tick': 3, 'household_indices': [1, 2], 'actions': ['cooperate', 'defect'],
                              'followup': {'offer_tick': 7, 'use_partner_result': True}}
        with TemporaryDirectory() as tmp:
            run = Path(tmp)/'world'
            checkpoint = await economic_run.run_new(cfg, run, until=7)
            world, store, marker = economic_run.open_run(run)
            previous = world.scenario.economy.export_state()
            support = previous['bank_positions']['support_advances']['bank:b']
            self.assertEqual((support['status'], support['bank_repaid_minor'], support['outstanding_minor']),
                             ('defaulted', 5, 4))
            self.assertEqual(previous['balances'][TREASURY]['loan_allowance'], -4)
            self.assertEqual(previous['bank_positions']['resolution']['bank:a'], 'receivership')
            self.assertEqual(checkpoint['tick'], 7)
            blocked, = (e for e in previous['journal'] if e['event_id'] == 'bank:4:scheduled:try-spending-queued-deposit')
            self.assertEqual(blocked['details']['reason'], 'insufficient_unreserved_deposit')
            self.assertEqual(blocked['postings'], [])
            followup = {}
            for decision in world.snapshot.decisions:
                if decision.tick == 7:
                    for intent in decision.economic_intents:
                        if intent.kind == 'cooperation_choice':
                            self.assertEqual(sum(e.startswith('cooperation-outcome:') for e in decision.evidence_ids), 1)
                            followup[decision.actor_id] = intent.action
            self.assertEqual(followup, {'household:0001': 'defect', 'household:0002': 'cooperate'})

            def interrupt(point):
                if point == 'before_marker':
                    raise OSError('interrupted liquidation')

            store.fault_hook = interrupt
            with self.assertRaisesRegex(OSError, 'interrupted liquidation'):
                await world.step()
            self.assertEqual(world.scenario.economy.export_state(), previous)
            self.assertEqual(CommitStore(run).markers()[-1], marker)
            resumed = await economic_run.resume_run(run)
            replay = await economic_run.replay_run(run, destination=Path(tmp)/'replay')
            self.assertEqual((replay['status'], replay['compared_ticks'], replay['state_hash']),
                             ('matched', 8, resumed['state_hash']))
            final, _, _ = economic_run.open_run(run)
            state = final.scenario.economy.export_state()
            self.assertEqual(state['bank_positions']['resolution']['bank:a'], 'liquidated')
            self.assertEqual(state['bank_positions']['support_advances']['bank:b'], support)
            self.assertEqual([e['tick'] for e in state['journal'] if e['kind'] == 'cooperation_settlement'], [4, 8])
