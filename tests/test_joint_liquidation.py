import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from civitas.application import economic_run
from civitas.storage.commit_store import CommitStore


class JointLiquidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_joint_fixture_books_loss_only_after_recovery_and_release(self):
        source = Path(__file__).resolve().parents[1]/'examples/closed-economy-joint-liquidation.json'
        bank = json.loads(source.read_text())['banking']
        operations = []
        for op in bank['operations']:
            if op['operation_id'].startswith('stress-'):
                short = {**op, 'tick': {16: 1, 17: 2, 25: 3, 26: 4}[op['tick']]}
                if short['kind'] == 'approve':
                    short['payment_interval'] = 1
                for field in ('actor', 'target_actor'):
                    if short.get(field):
                        short[field] = {'household:0030': 'household:0000',
                                        'household:0031': 'household:0001'}[short[field]]
                operations.append(short)
        cfg = {'ticks': 4, 'households': 4, 'banking': {
            'initial_bank_cash': {'bank:a': 2},
            'credit_creation_mode': bank['credit_creation_mode'],
            'loan_to_capital_multiple': bank['loan_to_capital_multiple'],
            'operations': operations,
        }}
        with TemporaryDirectory() as directory:
            run = Path(directory)/'world'
            await economic_run.run_new(cfg, run, until=3)
            store = CommitStore(run)
            _, before = store.load(3)
            positions = before.data['economic_runtime']['bank_positions']
            self.assertEqual(positions['resolution'], {'bank:a': 'receivership'})
            self.assertEqual(positions['deposits']['bank:a']['household:0001'], 400)
            loan = positions['loans']['stress-credit']
            self.assertEqual((loan['principal_minor'], loan['allowance_minor']), (400, 400))
            self.assertFalse(any(p['account_code']=='deposit_loss' for j in before.data['economic_runtime']['journal']
                                 for p in j['postings']))
            await economic_run.resume_run(run)
            _, after = store.load(4)
            state = after.data['economic_runtime']
            self.assertEqual(state['bank_positions']['resolution'], {'bank:a': 'liquidated'})
            self.assertEqual(state['bank_positions']['deposits']['bank:a'], {})
            settlement = next(j for j in state['journal'] if j['payload'].get('reference')=='stress-liquidation')
            self.assertEqual(settlement['details']['payouts_minor'], {'household:0001': 10})
            self.assertEqual(settlement['details']['losses_minor'], {'household:0001': 390})
            replay = await economic_run.replay_run(run, destination=Path(directory)/'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched', 4))
