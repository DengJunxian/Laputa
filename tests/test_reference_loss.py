from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from civitas.application.closed_economy import build_closed_economy
from civitas.application import economic_run
from civitas.cognition.evidence import with_evidence
from civitas.experiments.institutions import audit_institutions
from civitas.storage.commit_store import CommitStore, read_json


EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
BASE = read_json(EXAMPLES / 'closed-economy-security-feedback.json')
SETTINGS = read_json(EXAMPLES / 'reference-loss-settings.json')


class ReferenceLossTests(unittest.IsolatedAsyncioTestCase):
    async def test_weight_and_disclosure_cuts_have_the_same_initial_state_and_keep_null_results(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            stores, books = {}, {}
            for arm, overrides in (('linear', {'lambda_bps': 10000}), ('weighted', {}),
                                   ('cut', {'peer_disclosure': False})):
                path = folder / arm
                raw = {**BASE, 'reference_loss': {**SETTINGS, **overrides}}
                await economic_run.run_new(raw, path, until=3)
                final = await economic_run.resume_run(path)
                replay = await economic_run.replay_run(path, destination=folder / (arm + '-replay'))
                self.assertEqual((replay['status'], replay['compared_ticks'], replay['state_hash']),
                                 ('matched', 5, final['state_hash']))
                self.assertEqual((replay['external_api_calls'], replay['applied_deltas_to_execution']), (0, False))
                store = stores[arm] = CommitStore(path)
                _, state = store.load(5)
                book = books[arm] = state.data['economic_runtime']
                mark, = [e for e in book['journal'] if e['kind'] == 'security_mark']
                self.assertEqual((mark['tick'], mark['postings'], mark['goods_movements']), (2, [], []))
                self.assertEqual([m['consumed_units'] for m in state.data['closed_economy']['macro_history']],
                                 [16, 16, 10, 10, 10])
                self.assertEqual(audit_institutions(state)['economy']['security_final_units'], 4)

                response_count = 0
                delivered = set()
                receipts = {r.command_id: r for r in state.receipts}
                transactions = {e['tx_id']: e for e in book['journal']}
                for tick in range(1, 6):
                    decisions = {d.actor_id: d for d in state.decisions if d.tick == tick}
                    for obs in store.observation_rows(store.load(tick)[0]):
                        decision = decisions[obs.actor_id]
                        wealth = next((f for f in obs.market_view if f.value.get('kind') == 'security_wealth'), None)
                        if wealth is not None:
                            self.assertGreaterEqual(tick, 3)
                            response_count += 1
                            self.assertEqual(wealth.owner_id, obs.actor_id)
                            self.assertIn(wealth.fact_id, decision.evidence_ids)
                            response = decision.belief_patch['security_wealth_response']
                            self.assertEqual((response['reference_value_minor'], response['market_value_minor'],
                                              response['loss_minor'], response['mark_tx_id']),
                                             (100, 10, 90, mark['tx_id']))
                            self.assertEqual(response['reserve_increase_minor'], 90 if arm == 'linear' else 180)
                            self.assertEqual(decision.belief_patch['closed_choice']['budget_minor'], 0)
                            self.assertFalse(any(i.kind == 'purchase' for i in decision.economic_intents))
                        for message in obs.delivered_messages:
                            if message.body.get('kind') != 'disclosed_consumption':
                                continue
                            receipt = receipts[message.body['receipt_command_id']]
                            self.assertEqual((receipt.tick, message.created_at.tick, message.visible_at.tick),
                                             (tick - 2, tick - 1, tick))
                            self.assertEqual((receipt.outcome, receipt.actor_id, receipt.executed_quantity),
                                             ('consumed', message.sender_id, message.body['units']))
                            settlement, = receipt.ledger_tx_ids
                            self.assertEqual(transactions[settlement]['kind'], 'consumption')
                            self.assertEqual(set(message.body), {'kind', 'root_claim_id', 'receipt_command_id', 'units'})
                            channel = (obs.actor_id, message.body['root_claim_id'])
                            self.assertNotIn(channel, delivered)
                            delivered.add(channel)
                            if decision.belief_patch['closed_choice']['peer_uplift_units']:
                                self.assertIn(message.message_id, decision.evidence_ids)
                self.assertEqual(response_count, 9)
                self.assertEqual(len(delivered), 0 if arm == 'cut' else 42)
                self.assertEqual(sum(d.belief_patch.get('closed_choice', {}).get('peer_uplift_units', 0)
                                     for d in state.decisions), 0 if arm == 'cut' else 3)
            self.assertEqual(len({s.load(0)[0].state_hash for s in stores.values()}), 1)
            self.assertEqual(books['linear'], books['weighted'])

            # Exercise the same policy at a fixed authorized-input boundary where the floor does not bind at lambda=1.
            store = stores['weighted']
            obs = next(o for o in store.observation_rows(store.load(3)[0]) if o.actor_id == 'household:0000')
            account = obs.account_view[0].model_dump()
            account['value'].update(cash_minor=120, available_cash_minor=120)
            hypothetical = with_evidence(obs, account_view=[account])
            for weight, units in ((10000, 2), (20000, 0)):
                world = build_closed_economy({**BASE, 'reference_loss': {**SETTINGS, 'lambda_bps': weight}})
                decision = await world.policy.decide(hypothetical)
                self.assertEqual(sum(i.quantity_units for i in decision.economic_intents if i.kind == 'purchase'), units)

    async def test_edge_removal_cuts_inflight_consumption_without_erasing_debt(self):
        banking = {**BASE['banking'], 'credit_offer': {**BASE['banking']['credit_offer'], 'installments': 6}}
        raw = {**BASE, 'banking': banking, 'reference_loss': SETTINGS, 'ticks': 8,
               'introductions': True, 'link_budget_per_tick': 1}
        world = build_closed_economy({**raw, 'link_withdrawal_tick': 6})
        await world.run(6)
        data = world.snapshot.data
        withdrawn = {(d['source_id'], d['target_id']) for d in data['closed_social']['network']['pending']
                     if d['operation'] == 'delete'}
        packets = {m.message_id for m in world.snapshot.messages
                   if m.created_at.tick == 6 and m.body.get('kind') == 'disclosed_consumption'
                   and (m.sender_id, m.recipient_id) in withdrawn}
        self.assertTrue(packets)
        self.assertEqual(data['economic_runtime']['bank_positions']['loans']['joint-credit']['principal_minor'], 6)
        await world.step()
        data = world.snapshot.data
        edges = {(e['source_id'], e['target_id']) for e in data['closed_social']['network']['communication']}
        self.assertFalse(withdrawn & edges)
        self.assertTrue(packets <= {m.message_id for m in world.snapshot.messages})
        self.assertFalse(packets & {r['message_id'] for r in data['closed_social']['trace'] if r['kind'] == 'exposure'})
        control = build_closed_economy(raw)
        await control.run(7)
        loan = data['economic_runtime']['bank_positions']['loans']['joint-credit']
        self.assertEqual(loan, control.snapshot.data['economic_runtime']['bank_positions']['loans']['joint-credit'])
        self.assertEqual((loan['status'], loan['principal_minor']), ('active', 4))
        self.assertEqual(data['economic_runtime']['balances']['household:0003']['loan_payable'], -4)
        self.assertEqual(audit_institutions(world.snapshot)['economy']['status'], 'passed')
