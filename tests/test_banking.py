import json
import tempfile
import unittest
from pathlib import Path

from civitas.application import economic_run
from civitas.experiments.closed_economy import audit_snapshot

A, B = 'bank:a', 'bank:b'
H0, H1 = 'household:0000', 'household:0001'
NOTICE_CONFIG = Path(__file__).resolve().parents[1] / 'examples/closed-economy-bank-notice.json'
FUNDED_CONFIG = NOTICE_CONFIG.with_name('closed-economy-funded-bank-disclosure.json')
SUPPORT_CONFIG = NOTICE_CONFIG.with_name('closed-economy-treasury-support.json')
POLICY_CONFIG = NOTICE_CONFIG.with_name('closed-economy-bank-policy-combination.json')


def operation(tick, id_, kind, **fields):
    return {'tick': tick, 'operation_id': id_, 'kind': kind, **fields}


class BankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_joint_bank_policies_freeze_distinct_dose_and_realized_budget_comparisons(self):
        from civitas.experiments.institutions import compare_institutions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'policy'
            report = await compare_institutions(json.loads(POLICY_CONFIG.read_bytes()), root, bank_policy_combination=True)
            self.assertEqual((report['status'], report['assigned_worlds'], report['completed_worlds']),
                             ('completed', 6, 6))
            fixed, budget = report['fixed_component_dose'], report['matched_gross_budget']
            self.assertTrue(fixed['valid'])
            self.assertTrue(budget['valid'])
            for group, data in [('fixed_component_dose', fixed), ('matched_gross_budget', budget)]:
                plan = json.loads((root / group / 'plan.json').read_bytes())
                self.assertEqual(plan['plan_hash'], data['plan_hash'])
                self.assertTrue(all(r['replay']['status'] == 'matched' for r in data['rows'].values()))
            self.assertEqual(fixed['interaction']['terminal_deposit_loss_minor'], 0)
            self.assertEqual(fixed['interaction']['deposit_loss_rate'], 0.0)
            self.assertTrue(all(row['initial_deposits_minor'] == 12 for row in fixed['rows'].values()))
            self.assertEqual([fixed['rows'][a]['actual_gross_outlay_minor']
                              for a in ('D0S0', 'D0S1', 'D1S0', 'D1S1')], [0, 9, 6, 15])
            self.assertEqual([budget['rows'][a]['actual_gross_outlay_minor']
                              for a in ('support_only', 'mixed')], [9, 9])
            self.assertEqual([budget['rows'][a]['public_net_cash_outlay_minor']
                              for a in ('support_only', 'mixed')], [0, 6])

    async def test_joint_bank_policy_keeps_partial_and_equal_underspend_worlds(self):
        from civitas.experiments.institutions import compare_institutions

        for cash in (14, 2):
            with self.subTest(treasury_cash=cash), tempfile.TemporaryDirectory() as tmp:
                config = json.loads(POLICY_CONFIG.read_bytes())
                config['fiscal']['initial_cash_minor'] = cash
                report = await compare_institutions(
                    config, Path(tmp) / 'policy', bank_policy_combination=True)
                self.assertEqual((report['status'], report['assigned_worlds'], report['completed_worlds']),
                                 ('completed', 6, 6))
                fixed, budget = report['fixed_component_dose'], report['matched_gross_budget']
                self.assertFalse(fixed['valid'])
                self.assertIsNone(fixed['interaction'])
                joint = fixed['rows']['D1S1']
                self.assertIn('disclosure_payment_mismatch', joint['manipulation_issues'])
                self.assertEqual(len(joint['exposures']), 1 if cash == 14 else 0)
                self.assertEqual(budget['valid'], cash == 14)
                if cash == 2:
                    self.assertIsNone(budget['difference'])
                    self.assertEqual([row['actual_gross_outlay_minor'] for row in budget['rows'].values()], [0, 0])
                    self.assertTrue(all('support_not_fully_disbursed' in row['manipulation_issues']
                                        for row in budget['rows'].values()))

    async def test_joint_bank_policy_keeps_failed_arm_and_resumes_its_durable_world(self):
        from unittest.mock import patch

        from civitas.experiments.institutions import compare_institutions
        from civitas.storage.commit_store import CommitStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'policy'
            original_commit = CommitStore.commit

            def interrupted_commit(store, base, prepared, **fields):
                if prepared.tick == 1:
                    self.assertTrue((root / 'fixed_component_dose' / 'plan.json').is_file())
                    self.assertTrue((root / 'matched_gross_budget' / 'plan.json').is_file())
                if prepared.tick == 3 and store.root.name == 'D1S1':
                    raise OSError('injected policy arm interruption')
                return original_commit(store, base, prepared, **fields)

            with patch.object(CommitStore, 'commit', interrupted_commit):
                report = await compare_institutions(json.loads(POLICY_CONFIG.read_bytes()), root,
                                                    bank_policy_combination=True)
            self.assertEqual((report['status'], report['assigned_worlds'], report['completed_worlds']), ('failed', 6, 5))
            fixed = report['fixed_component_dose']
            self.assertIsNone(fixed['interaction'])
            self.assertEqual(fixed['rows']['D1S1']['manipulation_issues'], ['execution_failed'])
            self.assertTrue(report['matched_gross_budget']['valid'])
            world_path = root / 'fixed_component_dose' / 'D1S1'
            self.assertEqual(CommitStore(world_path).markers()[-1].tick, 2)
            original_report = (root / 'comparison.json').read_bytes()
            await economic_run.resume_run(world_path)
            replay = await economic_run.replay_run(world_path, destination=root / 'recovered-replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))
            self.assertEqual((root / 'comparison.json').read_bytes(), original_report)

    async def test_joint_bank_policy_marks_unresolved_deposit_recovery_as_unmeasured(self):
        from civitas.experiments.institutions import compare_institutions

        config = json.loads(POLICY_CONFIG.read_bytes())
        config['banking']['loan_to_capital_multiple'] = 70
        debtor, depositor = 'household:0003', 'household:0002'
        config['banking']['operations'].extend([
            operation(1, 'other-bank-loan', 'approve', bank=B, actor=debtor, amount_minor=200,
                      installments=1, default_after_misses=1),
            operation(2, 'other-bank-transfer', 'payment', bank=B, actor=debtor, amount_minor=200,
                      target_bank=B, target_actor=depositor),
            operation(4, 'other-bank-resolution', 'resolve', bank=B),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            report = await compare_institutions(config, Path(tmp) / 'policy', bank_policy_combination=True)
            fixed = report['fixed_component_dose']
            self.assertTrue(fixed['valid'])
            self.assertIsNone(fixed['interaction']['deposit_loss_rate'])
            for row in fixed['rows'].values():
                self.assertGreaterEqual(row['unresolved_receivership_deposits_minor'], 200)
                self.assertIsNone(row['deposit_loss_rate'])

    async def test_treasury_advance_funds_reserves_and_recovers_cash_at_maturity(self):
        from civitas.experiments.institutions import audit_institutions

        config = json.loads(SUPPORT_CONFIG.read_bytes())
        config['banking']['treasury_support']['guarantee_limit_minor'] = 0
        maturity_inflow = next(op for op in config['banking']['operations'] if op['operation_id'] == 'maturity-inflow')
        maturity_inflow['amount_minor'] = 2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'advance'
            await economic_run.run_new(config, root, until=3)
            world, _, _ = economic_run.open_run(root)
            book = world.scenario.economy
            state = book.export_state()['balances']
            self.assertEqual((state['public:treasury']['cash'], state['public:treasury']['loan_asset']), (2, 6))
            self.assertEqual((state[A]['bank_reserve'], state[A]['loan_payable']), (4, -6))
            self.assertEqual((state['public:settlement']['cash'], state['public:settlement']['reserve_payable']), (8, -8))
            self.assertEqual(book.banking_report()['withdrawals']['cash-demand']['paid_minor'], 15)
            self.assertEqual(book.banking_report()['banks'][A]['equity_minor'], 2)
            await economic_run.resume_run(root, until=4)
            world, _, _ = economic_run.open_run(root)
            end = audit_institutions(world.snapshot)
            self.assertEqual(end['fiscal']['cash_remaining_minor'], 8)
            support = end['treasury_support']
            self.assertEqual((support['disbursed_minor'], support['bank_repaid_minor'],
                              support['guarantee_paid_minor'], support['outstanding_minor']), (6, 6, 0, 0))
            await economic_run.resume_run(root)
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))

    async def test_treasury_advance_separates_shortfall_guarantee_and_late_recovery(self):
        from civitas.application.closed_economy import build_closed_economy
        from civitas.experiments.institutions import audit_institutions

        for limit in (0, 1):
            with self.subTest(guarantee_limit=limit):
                config = json.loads(SUPPORT_CONFIG.read_bytes())
                config['banking']['treasury_support']['guarantee_limit_minor'] = limit
                world = build_closed_economy(config)
                for _ in range(4):
                    await world.step()
                due = audit_institutions(world.snapshot)['treasury_support']
                self.assertEqual((due['bank_repaid_minor'], due['guarantee_paid_minor']), (5, limit))
                self.assertEqual((due['outstanding_minor'], due['allowance_minor']), (1-limit, 1-limit))
                book = world.scenario.economy
                self.assertEqual(book.banking_report()['support_advances'][A]['status'],
                                 'paid' if limit else 'defaulted')
                self.assertEqual(book.account_view('public:guarantee').cash_minor, 1-limit)
                await world.step()
                end = audit_institutions(world.snapshot)['treasury_support']
                self.assertEqual((end['bank_repaid_minor'], end['outstanding_minor'], end['allowance_minor']),
                                 (6-limit, 0, 0))
                self.assertEqual((end['treasury_net_cash_outlay_minor'], end['public_net_cash_outlay_minor']),
                                 (0, limit))

    async def test_treasury_advance_declines_ineligible_bank_and_unfunded_cash(self):
        from civitas.application.closed_economy import build_closed_economy
        from civitas.experiments.institutions import audit_institutions

        for treasury_cash, bank_cash, reason in (
                (5, 2, 'insufficient_treasury_cash'), (8, 0, 'bank_ineligible_equity')):
            with self.subTest(treasury_cash=treasury_cash, bank_cash=bank_cash):
                config = json.loads(SUPPORT_CONFIG.read_bytes())
                config['fiscal']['initial_cash_minor'] = treasury_cash
                config['banking']['initial_bank_cash'][A] = bank_cash
                world = build_closed_economy(config)
                for _ in range(5):
                    await world.step()
                book = world.scenario.economy
                events = [e for e in book.journal if e.payload.get('action', '').startswith('support_')]
                self.assertEqual(len(events), 1)
                self.assertEqual((events[0].details['reason'], events[0].postings), (reason, []))
                self.assertEqual(audit_institutions(world.snapshot)['treasury_support']['outstanding_minor'], 0)
                self.assertEqual(book.account_view('public:treasury').cash_minor, treasury_cash)

    async def test_treasury_advance_rolls_back_disbursement_and_collection_before_marker(self):
        config = json.loads(SUPPORT_CONFIG.read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'support'
            await economic_run.run_new(config, root, until=2)
            for tick in (3, 4):
                world, store, marker = economic_run.open_run(root)
                before = world.scenario.economy.export_state()

                def fail(point):
                    if point == 'before_marker':
                        raise OSError('Treasury support commit interrupted')

                store.fault_hook = fail
                with self.assertRaisesRegex(OSError, 'Treasury support commit interrupted'):
                    await world.step()
                self.assertEqual(world.scenario.economy.export_state(), before)
                self.assertEqual(store.markers()[-1].commit_hash, marker.commit_hash)
                await economic_run.resume_run(root, until=tick)
            await economic_run.resume_run(root)
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))

    async def test_treasury_advance_comparison_freezes_shock_and_measures_realized_payments(self):
        from civitas.experiments.institutions import compare_institutions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'comparison'
            report = await compare_institutions(json.loads(SUPPORT_CONFIG.read_bytes()), root)
            self.assertTrue(report['manipulation_passed'])
            self.assertEqual((report['status'], report['assigned_worlds'], report['completed_worlds']),
                             ('completed', 3, 3))
            rows = report['rows']
            self.assertEqual([rows[arm]['withdrawals_paid_by_support_tick_minor']
                              for arm in ('control', 'unguaranteed', 'guaranteed')], [13, 15, 15])
            self.assertEqual(rows['unguaranteed']['at_maturity']['outstanding_minor'], 1)
            self.assertEqual(rows['guaranteed']['at_maturity']['guarantee_paid_minor'], 1)
            self.assertEqual(rows['guaranteed']['treasury_support']['public_net_cash_outlay_minor'], 1)
            self.assertTrue(all(row['replay']['status'] == 'matched' for row in rows.values()))
            self.assertTrue((root / 'plan.json').is_file())

    async def test_funded_notice_spends_shared_treasury_cash_only_on_actual_recipients(self):
        from civitas.experiments.institutions import audit_institutions

        config = json.loads(FUNDED_CONFIG.read_bytes())
        config['banking']['run_notice']['disclosure_funding']['budget_cap_minor'] = 4
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'partial'
            await economic_run.run_new(config, root, until=3)
            world, store, marker = economic_run.open_run(root)
            book = world.scenario.economy
            notice = next(row for row in book.journal if row.payload.get('action') == 'notice')
            self.assertEqual(notice.details['disclosure_by_recipient_minor'], {H1: 3})
            self.assertEqual(notice.details['reached'], [H1])
            self.assertEqual(book.account_view('public:treasury').cash_minor, 7)
            self.assertEqual(book.account_view('public:disclosure').cash_minor, 5)
            observations = store.observation_rows(marker)
            exposed = [obs.actor_id for obs in observations if any(
                f.value.get('kind') == 'verified_withdrawal_delay' for f in obs.market_view)]
            self.assertEqual(exposed, [H1])
            self.assertEqual(audit_institutions(world.snapshot)['fiscal']['program_cash_outlay_minor'], 5)
            await economic_run.resume_run(root)
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))

    async def test_funded_disclosure_comparison_keeps_initial_cash_and_notice_tick_fixed(self):
        from civitas.experiments.institutions import compare_institutions

        config = json.loads(FUNDED_CONFIG.read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'coverage'
            report = await compare_institutions(config, root)
            self.assertEqual((report['status'], report['assigned_worlds'], report['completed_worlds']),
                             ('completed', 4, 4))
            self.assertTrue(report['manipulation_passed'])
            rows = report['rows']
            for arm, expected in {
                'zero_budget': (0, 0, 8, 'unfunded'),
                'partial_budget': (3, 1, 5, 'disclosed'),
                'full_budget': (6, 2, 2, 'disclosed'),
                'expired': (0, 0, 8, 'authorization_expired'),
            }.items():
                row = rows[arm]
                self.assertEqual((row['disclosure']['disclosure_paid_minor'], len(row['exposures']),
                                  row['treasury_cash_remaining_minor'], row['disclosure']['status']), expected)
                self.assertTrue(all(e['tick'] == 3 for e in row['exposures']))
                self.assertEqual(row['replay']['status'], 'matched')
            plans = json.loads((root / 'plan.json').read_bytes())['plan']['arms']
            self.assertEqual({cfg['fiscal']['initial_cash_minor'] for cfg in plans.values()}, {12})
            self.assertEqual({cfg['banking']['run_notice']['tick'] for cfg in plans.values()}, {3})

    async def test_funded_disclosure_requires_available_cash_and_a_verified_source(self):
        from civitas.application.closed_economy import build_closed_economy

        for opening_cash, source_amount, status in (
                (4, 15, 'unfunded'), (12, 21, 'not_emitted_no_verified_root')):
            with self.subTest(opening_cash=opening_cash, source_amount=source_amount):
                config = json.loads(FUNDED_CONFIG.read_bytes())
                config['fiscal']['initial_cash_minor'] = opening_cash
                source = next(op for op in config['banking']['operations'] if op['operation_id'] == 'first-delay')
                source['amount_minor'] = source_amount
                world = build_closed_economy(config)
                for _ in range(3):
                    await world.step()
                book = world.scenario.economy
                notice = next(row for row in book.journal if row.payload.get('action') == 'notice')
                self.assertEqual((notice.details['status'], notice.details['reached'], notice.postings),
                                 (status, [], []))
                self.assertEqual(book.account_view('public:treasury').cash_minor, opening_cash - 2)
                self.assertEqual(book.account_view('public:disclosure').cash_minor, 2)
                self.assertFalse(any(r.unit == 'cash_minor' for r in world.snapshot.receipts))

    async def test_funded_disclosure_rolls_back_charge_and_coverage_before_marker(self):
        config = json.loads(FUNDED_CONFIG.read_bytes())
        config['fiscal']['initial_cash_minor'] = 6
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'recovered'
            await economic_run.run_new(config, root, until=2)
            world, store, marker = economic_run.open_run(root)
            before = world.scenario.economy.export_state()

            def fail(point):
                if point == 'before_marker':
                    raise OSError('funded disclosure commit interrupted')

            store.fault_hook = fail
            with self.assertRaisesRegex(OSError, 'funded disclosure commit interrupted'):
                await world.step()
            self.assertEqual(world.scenario.economy.export_state(), before)
            self.assertEqual(store.markers()[-1].commit_hash, marker.commit_hash)
            await economic_run.resume_run(root, until=3)
            world, store, marker = economic_run.open_run(root)
            book = world.scenario.economy
            self.assertEqual(book.account_view('public:treasury').cash_minor, 1)
            self.assertEqual(book.account_view('public:disclosure').cash_minor, 5)
            self.assertEqual([obs.actor_id for obs in store.observation_rows(marker) if any(
                f.value.get('kind') == 'verified_withdrawal_delay' for f in obs.market_view)], [H1])
            await economic_run.resume_run(root)
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))

    async def test_verified_notice_drives_actor_request_then_next_tick_settlement(self):
        config = json.loads(NOTICE_CONFIG.read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bank'
            await economic_run.run_new(config, root, until=3)
            world, store, marker = economic_run.open_run(root)
            book = world.scenario.economy
            notice = next(row for row in book.journal if row.payload.get('action') == 'notice')
            origin = next(row for row in book.journal if row.event_id == 'bank:2:queue:first-delay')
            self.assertEqual(notice.details['source_tx_id'], origin.tx_id)
            fact_id = f'bank-notice:{notice.tx_id}:{H1}'
            self.assertEqual([obs.actor_id for obs in store.observation_rows(marker)
                              if fact_id in obs.evidence_ids], [H1])
            decision = next(d for d in world.snapshot.decisions if d.actor_id == H1 and d.tick == 3)
            self.assertIn(fact_id, decision.evidence_ids)
            self.assertFalse(any(fact_id in d.evidence_ids for d in world.snapshot.decisions if d.actor_id != H1))
            intent = next(i for i in decision.economic_intents if i.kind == 'withdraw_deposit')
            self.assertEqual((intent.bank_id, intent.amount_minor, intent.notice_fact_id), (A, 6, fact_id))
            receipt = next(r for r in world.snapshot.receipts if r.command_id == 'command:' + intent.intent_id)
            self.assertEqual((receipt.outcome, receipt.accepted_quantity, receipt.cash_delta_minor),
                             ('withdrawal_queued', 6, 0))
            report = book.banking_report()
            self.assertEqual(report['deposit_claims'][A][H1], 8)
            self.assertEqual(report['withdrawals']['notice:' + H1]['paid_minor'], 0)
            self.assertEqual(marker.tick, 3)
            await economic_run.resume_run(root)
            world, store, _ = economic_run.open_run(root)
            report = world.scenario.economy.banking_report()
            request = report['withdrawals']['notice:' + H1]
            self.assertEqual((request['status'], request['paid_minor'], request['remaining_minor']),
                             ('expired', 3, 3))
            self.assertTrue(all(audit_snapshot(s)['status'] == 'passed' for _, s in store.states()))
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))

    async def test_notice_timing_comparison_holds_source_audience_and_shock_fixed(self):
        from civitas.experiments.institutions import compare_institutions

        config = json.loads(NOTICE_CONFIG.read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'timing'
            report = await compare_institutions(config, root)
            self.assertEqual(report['status'], 'completed')
            self.assertTrue(report['manipulation_passed'])
            self.assertEqual(report['assigned_worlds'], 3)
            self.assertTrue((root / 'plan.json').is_file())
            rows = report['rows']
            self.assertEqual([rows[arm]['requested_minor'] for arm in ('early', 'delayed', 'withheld')], [6, 6, 0])
            self.assertEqual([rows[arm]['paid_minor'] for arm in ('early', 'delayed', 'withheld')], [3, 5, 0])
            self.assertEqual([rows[arm]['initial_deposits_minor'] for arm in rows], [9, 9, 9])
            self.assertEqual([r['tick'] for r in rows['early']['exposures']], [3])
            self.assertEqual([r['tick'] for r in rows['delayed']['exposures']], [4])
            self.assertEqual(rows['withheld']['exposures'], [])
            self.assertEqual(rows['early']['exposures'][0]['source_tx_id'],
                             rows['delayed']['exposures'][0]['source_tx_id'])
            self.assertEqual(report['early_minus_delayed_requested_minor'], 0)
            self.assertEqual(report['early_minus_delayed_paid_minor'], -2)
            self.assertTrue(all(row['replay']['status'] == 'matched' for row in rows.values()))

    async def test_withdrawal_rejects_unauthorized_duplicate_and_unfunded_requests(self):
        from civitas.application.closed_economy import build_closed_economy
        from civitas.contracts.economic import WithdrawDepositIntent

        config = json.loads(NOTICE_CONFIG.read_bytes())
        unfunded = 'household:0003'
        config['banking']['run_notice']['withdrawal_by_recipient'][unfunded] = 2
        world = build_closed_economy(config)
        decide = world.policy.decide
        real_fact = f'bank-notice:econ:bank:3:notice:first-delay:{H1}'

        async def propose_extra(observation):
            bundle = await decide(observation)
            if bundle.tick != 3 or bundle.actor_id not in (H0, H1):
                return bundle
            forged = WithdrawDepositIntent(intent_id='extra:' + bundle.actor_id,
                                          bank_id=A, notice_fact_id=real_fact, amount_minor=6)
            return bundle.model_copy(update={'economic_intents': [*bundle.economic_intents, forged]})

        world.policy.decide = propose_extra
        for _ in range(3):
            await world.step()
        rejections = {r.command_id: list(r.reason_codes) for r in world.snapshot.receipts
                      if r.tick == 3 and r.unit == 'cash_minor' and r.status == 'rejected'}
        self.assertEqual(rejections, {
            'command:extra:' + H0: ['withdrawal_notice_unauthorized'],
            'command:extra:' + H1: ['withdrawal_notice_already_used'],
            f'command:intent:3:{unfunded}:withdraw-deposit': ['insufficient_unreserved_deposit'],
        })
        book = world.scenario.economy
        self.assertEqual([e.payload['actor_id'] for e in book.journal if e.kind == 'bank_withdrawal'], [H1])
        self.assertEqual(set(book.banking_report()['withdrawals']), {'first-delay', 'notice:' + H1})

    async def test_withdrawal_request_and_payment_both_rollback_before_marker(self):
        config = json.loads(NOTICE_CONFIG.read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bank'
            await economic_run.run_new(config, root, until=2)
            for tick in (3, 4):
                world, store, marker = economic_run.open_run(root)
                before = world.scenario.economy.export_state()

                def fail(point):
                    if point == 'before_marker':
                        raise OSError('bank notice commit interrupted')

                store.fault_hook = fail
                with self.assertRaisesRegex(OSError, 'bank notice commit interrupted'):
                    await world.step()
                self.assertEqual(world.scenario.economy.export_state(), before)
                self.assertEqual(store.markers()[-1].commit_hash, marker.commit_hash)
                await economic_run.resume_run(root, until=tick)
            await economic_run.resume_run(root)
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))

    async def test_commitment_creation_cross_bank_and_queued_withdrawal(self):
        config = {'ticks': 4, 'households': 4, 'banking': {
            'credit_creation_mode': 'bank_credit_creation', 'initial_bank_cash': {A: 10, B: 2},
            'operations': [
                operation(1, 'd1', 'deposit', bank=A, actor=H0, amount_minor=10),
                operation(1, 'line1', 'approve', bank=A, actor=H0, amount_minor=20, installments=2),
                operation(2, 'p1', 'payment', bank=A, actor=H0, target_bank=B, target_actor=H1, amount_minor=15),
                operation(2, 'p2-insufficient-reserves', 'payment', bank=A, actor=H0,
                          target_bank=B, target_actor=H1, amount_minor=6),
                operation(2, 'w1', 'withdraw', bank=A, actor=H0, amount_minor=8, expires_tick=3),
            ]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bank'
            await economic_run.run_new(config, root, until=1)
            world, _, _ = economic_run.open_run(root)
            report = world.scenario.economy.banking_report()
            self.assertEqual(report['loans']['line1']['status'], 'committed')
            self.assertEqual(report['deposit_claims'][A], {H0: 10})
            self.assertEqual(report['banks'][A]['reserves_minor'], 20)
            await economic_run.resume_run(root, until=2)
            world, _, _ = economic_run.open_run(root)
            report = world.scenario.economy.banking_report()
            self.assertEqual(report['loans']['line1']['principal_minor'], 20)
            self.assertEqual(report['banks'][A]['reserves_minor'], 0)
            self.assertEqual(report['banks'][B]['reserves_minor'], 17)
            self.assertEqual(report['deposit_claims'], {A: {H0: 10}, B: {H1: 15}})
            self.assertEqual(report['withdrawals']['w1']['remaining_minor'], 3)
            self.assertEqual(report['withdrawals']['w1']['status'], 'queued')
            self.assertEqual(report['banks'][A]['equity_minor'], 10)
            refusal = next(e for e in world.scenario.economy.journal if 'p2-insufficient-reserves' in e.event_id)
            self.assertEqual(refusal.details['reason'], 'insufficient_reserves')
            self.assertEqual(refusal.postings, [])
            self.assertTrue(report['banks'][A]['illiquid'])
            self.assertFalse(report['banks'][A]['insolvent'])
            self.assertEqual(audit_snapshot(world.snapshot)['status'], 'passed')
            await economic_run.resume_run(root)
            world, _, _ = economic_run.open_run(root)
            report = world.scenario.economy.banking_report()
            self.assertEqual(report['loans']['line1']['status'], 'paid')
            self.assertEqual(report['withdrawals']['w1']['status'], 'paid')
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 4))

    async def test_default_keeps_nominal_debt_until_recovery_and_resolution(self):
        from civitas.application.closed_economy import build_closed_economy

        cfg = {'ticks': 4, 'households': 4, 'banking': {
            'credit_creation_mode': 'bank_credit_creation', 'initial_bank_cash': {A: 10},
            'operations': [
                operation(1, 'line', 'approve', bank=A, actor=H0, amount_minor=120, installments=1,
                          period_rate_bps=500, default_after_misses=1),
                operation(2, 'gift', 'payment', bank=A, actor=H0, target_bank=A, target_actor=H1, amount_minor=120),
                operation(4, 'collect', 'recover', loan_id='line', amount_minor=8),
                operation(4, 'release', 'release', loan_id='line'),
                operation(4, 'resolve', 'resolve', bank=A),
            ]}}
        world = build_closed_economy(cfg)
        for _ in range(3):
            await world.step()
        report = world.scenario.economy.banking_report()
        self.assertEqual((report['loans']['line']['status'], report['loans']['line']['principal_minor'],
                          report['loans']['line']['interest_minor']), ('defaulted', 120, 6))
        self.assertEqual(report['loans']['line']['allowance_minor'], 126)
        self.assertFalse(report['banks'][A]['illiquid'])
        self.assertTrue(report['banks'][A]['insolvent'])
        await world.step()
        report = world.scenario.economy.banking_report()
        self.assertEqual(report['loans']['line']['status'], 'released')
        self.assertEqual(report['banks'][A]['resolution'], 'liquidated')
        resolution = next(e for e in world.scenario.economy.journal
                          if e.kind == 'bank' and e.details['status'] == 'liquidated')
        self.assertEqual((resolution.details['losses_minor'][H1], resolution.details['payouts_minor'][H1]), (102, 18))
        self.assertEqual(audit_snapshot(world.snapshot)['status'], 'passed')

    async def test_prefunded_credit_refuses_to_use_customer_reserve(self):
        from civitas.application.closed_economy import build_closed_economy

        cfg = {'ticks': 3, 'households': 4, 'banking': {
            'credit_creation_mode': 'prefunded_intermediation', 'initial_bank_cash': {A: 10},
            'operations': [
                operation(1, 'deposit', 'deposit', bank=A, actor=H0, amount_minor=10),
                operation(1, 'line1', 'approve', bank=A, actor=H0, amount_minor=6, installments=1),
                operation(1, 'line2', 'approve', bank=A, actor=H1, amount_minor=10, installments=1),
            ]}}
        world = build_closed_economy(cfg)
        for _ in range(2):
            await world.step()
        report = world.scenario.economy.banking_report()
        self.assertEqual(report['deposit_claims'][A], {H0: 10})
        self.assertEqual(report['banks'][A]['reserves_minor'], 14)
        self.assertEqual(report['loans']['line1']['principal_minor'], 6)
        self.assertEqual(report['loans']['line2']['status'], 'refused')

    async def test_fault_undoes_bank_and_fiscal_postings_then_replay_matches(self):
        from civitas.experiments.institutions import audit_institutions

        examples = Path(__file__).resolve().parents[1] / 'examples'
        config = json.loads((examples / 'closed-economy-banking.json').read_bytes())
        config['fiscal'] = json.loads((examples / 'closed-economy-fiscal.json').read_bytes())['fiscal']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'joint'
            await economic_run.run_new(config, root, until=1)
            world, store, marker = economic_run.open_run(root)
            before = world.scenario.economy.export_state()

            def fail(point):
                if point == 'before_marker':
                    raise OSError('before joint institutional commit')

            store.fault_hook = fail
            with self.assertRaisesRegex(OSError, 'before joint institutional commit'):
                await world.step()
            self.assertEqual(world.scenario.economy.export_state(), before)
            self.assertEqual(store.markers()[-1].commit_hash, marker.commit_hash)
            await economic_run.resume_run(root)
            restored, _, _ = economic_run.open_run(root)
            audit = audit_institutions(restored.snapshot)
            self.assertEqual(audit['economy']['status'], 'passed')
            self.assertEqual(audit['fiscal']['collected_minor'], 48)
            self.assertEqual(restored.scenario.economy.banking_report()['banks'][B]['resolution'], 'liquidated')
            corrupted = restored.scenario.economy.export_state()
            corrupted['bank_positions']['deposits'][A][H0] = 1
            with self.assertRaisesRegex(ValueError, 'Bank claim mirror mismatch'):
                restored.scenario.economy.import_state(corrupted)
            replay = await economic_run.replay_run(root, destination=root.parent / 'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 5))
