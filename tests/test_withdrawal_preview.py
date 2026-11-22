import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from civitas.application import economic_run
from civitas.contracts.economic import WithdrawDepositIntent
from civitas.experiments.withdrawal_preview import audit_withdrawal_preview, run_withdrawal_preview
from civitas.storage.commit_store import CommitStore, atomic_json, read_json

FIXTURE = Path(__file__).resolve().parents[1]/'examples/closed-economy-bank-notice.json'
BUYER = 'household:0001'


class WithdrawalPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_pair_audits_admission_payment_and_expiration(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)/'pair'
            result = await run_withdrawal_preview(read_json(FIXTURE), root)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['denominators']['paired_opportunities'], 1)
            self.assertEqual(result['denominators']['model_attempts'], 1)
            self.assertEqual(result['denominators']['seats_with_payment'], 2)
            self.assertEqual(set(result['difference'].values()), {0})
            for arm in result['arms'].values():
                self.assertEqual((arm['requested_minor'], arm['queued_minor'], arm['paid_minor'],
                                  arm['outstanding_minor'], arm['queue_status']), (6, 6, 3, 3, 'expired'))
                self.assertEqual([(row['tick'], row['paid_minor']) for row in arm['settlements']], [(4,3),(5,0)])
            archive = CommitStore(root/'archive')
            rule = CommitStore(root/'rule')
            self.assertEqual([d for d in rule.load()[1].decisions if d.actor_id != BUYER],
                             [d for d in archive.load()[1].decisions if d.actor_id != BUYER])
            request, = archive.archived_records()
            obs = request['request']['observation']
            self.assertEqual([fact['owner_id'] for fact in obs['market_view']], [BUYER])
            self.assertEqual(obs['market_view'][0]['value']['kind'], 'verified_withdrawal_delay')
            self.assertFalse(any(obs[key] for key in ('account_view','private_memory_refs','delivered_messages')))
            replay = await economic_run.replay_run(root/'archive', destination=root/'replay')
            self.assertEqual((replay['status'], replay['compared_ticks']), ('matched',5))
            self.assertEqual(audit_withdrawal_preview(root), result)
            for private in ('household:', 'bank:', 'Bearer ', '"observation"', '"request"', '"response"'):
                self.assertNotIn(private, json.dumps(result))
            result['arms']['archive']['paid_minor'] = 6
            atomic_json(root/'shareable-comparison.json', result)
            with self.assertRaisesRegex(ValueError, 'independent audit'):
                audit_withdrawal_preview(root)

    async def test_hold_refusal_unreached_and_invalid_notice_preserve_denominators(self):
        config = read_json(FIXTURE)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            await run_withdrawal_preview(config, root/'base')
            tape = read_json(root/'base'/'response-archive.json')
            parsed = json.loads(tape['records'][0]['response']['text'])
            for variant in ('hold','refusal','unreached','invalid'):
                with self.subTest(variant=variant):
                    cfg, frozen = deepcopy(config), deepcopy(tape)
                    response = frozen['records'][0]['response']
                    if variant == 'hold':
                        response['text'] = json.dumps({**parsed, 'amount_minor':0})
                    elif variant == 'refusal':
                        response.update(ok=False, text='', error_code='refusal')
                    elif variant == 'unreached':
                        cfg['banking']['run_notice']['disclose'] = False
                    else:
                        response['text'] = json.dumps({**parsed, 'notice_fact_id':'unrelated-notice'})
                    result = await run_withdrawal_preview(cfg, root/variant, frozen)
                    archive = result['arms']['archive']
                    self.assertEqual(audit_withdrawal_preview(root/variant), result)
                    if variant == 'hold':
                        self.assertEqual(result['difference']['paid_minor'], -3)
                        summary = CommitStore(root/variant/'archive').load(3)[1]
                        control = CommitStore(root/variant/'rule').load(3)[1]
                        def other_seat_actions(state):
                            return [i for d in state.decisions if d.tick == 3 and d.actor_id == BUYER
                                    for i in d.economic_intents if not isinstance(i, WithdrawDepositIntent)]
                        self.assertEqual(other_seat_actions(summary), other_seat_actions(control))
                    elif variant == 'refusal':
                        self.assertEqual((archive['model_attempts'],archive['failed_model_attempts'],
                                          archive['rule_fallbacks'],archive['paid_minor']), (1,1,1,3))
                    elif variant == 'unreached':
                        self.assertEqual((result['denominators']['unreached_seats'], archive['model_attempts']), (2,0))
                        self.assertIsNone(result['difference'])
                    else:
                        self.assertEqual((archive['failed_execution_attempts'], archive['failed_model_attempts']), (1,1))
                        self.assertEqual(archive['commits'][-1]['tick'],2)
                        self.assertEqual(result['denominators']['unobserved_at_notice'],1)
                        self.assertIsNone(result['difference'])

    async def test_inadequate_deposit_rejects_authorized_amount_without_payment(self):
        config = read_json(FIXTURE)
        config['banking']['run_notice']['withdrawal_by_recipient'][BUYER] = 100
        with TemporaryDirectory() as folder:
            result = await run_withdrawal_preview(config, Path(folder)/'pair')
            self.assertEqual(result['denominators']['reached_seats'], 2)
            self.assertEqual(result['denominators']['rejected_intents'], 2)
            self.assertEqual(result['denominators']['insufficient_deposit'], 2)
            self.assertEqual(result['denominators']['seats_with_payment'], 0)
            self.assertTrue(all(arm['queued_minor'] == arm['paid_minor'] == 0 for arm in result['arms'].values()))
