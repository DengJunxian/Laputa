import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.cognition.providers import SyntheticProvider
from civitas.contracts.economic import CreditApplicationIntent
from civitas.storage.commit_store import CommitStore

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/closed-economy-credit-offer.json'
ACTOR = 'household:0000'
LOAN = 'resident-credit'
FACT = 'credit-offer:2:resident-credit:' + ACTOR


def config():
    return json.loads(EXAMPLE.read_bytes())


def test_one_offer_one_application_rule_and_archived_wait(tmp_path):
    async def run():
        base = config()
        rule = tmp_path / 'rule'
        author = tmp_path / 'author'
        model = tmp_path / 'model'
        await economic_run.run_new(base, rule)
        seat = {'household_index': 0, 'source': 'synthetic_test'}
        await economic_run.run_new({**base, 'credit_cognition': seat}, author)
        path = tmp_path / 'tape.json'
        economic_run.freeze_offline_responses(author, path)
        tape = json.loads(path.read_bytes())
        archived = {**base, 'credit_cognition': {**seat, 'source': 'archived_response'}}
        with patch.object(SyntheticProvider, 'complete', side_effect=AssertionError('regenerated response')):
            await economic_run.run_new(archived, model, response_archive=tape)
            for run in (rule, model):
                replay = await economic_run.replay_run(run, destination=tmp_path / (run.name + '-replay'))
                assert (replay['status'], replay['compared_ticks']) == ('matched', 5)
        source, alternative = CommitStore(rule), CommitStore(model)
        marker, state = source.load(2)
        rows = source.observation_rows(marker)
        assert rows == alternative.observation_rows(alternative.load(2)[0])
        reached = [(o.actor_id, f.fact_id) for o in rows for f in o.market_view if f.value.get('kind') == 'credit_offer']
        assert reached == [(ACTOR, FACT)]
        receipt, = [r for r in state.receipts if r.outcome == 'credit_application_recorded']
        assert (receipt.accepted_quantity, receipt.executed_quantity, receipt.cash_delta_minor) == (12, 0, 0)
        request, = alternative.archived_records()
        obs = request['request']['observation']
        assert request['request']['task'] == 'cd_credit' and request['accounted'] == 0
        assert [f['fact_id'] for f in obs['market_view']] == [FACT]
        assert not (obs['account_view'] or obs['private_memory_refs'] or obs['delivered_messages'])
        assert request['parsed'] == {'action': 'wait', 'offer_fact_id': FACT}
        rule_book = economic_run.open_run(rule, tick=3)[0].scenario.economy
        alt_book = economic_run.open_run(model, tick=3)[0].scenario.economy
        funding, = [e for e in rule_book.journal if e.event_id == 'bank:3:fund:' + LOAN]
        assert sorted((p.entity_id, p.account_code, p.side, p.amount_minor) for p in funding.postings) == sorted([
            ('bank:a', 'loan_asset', 'debit', 12), ('bank:a', 'deposit_payable', 'credit', 12),
            (ACTOR, 'deposit_asset', 'debit', 12), (ACTOR, 'loan_payable', 'credit', 12)])
        assert funding.details['disbursed_minor'] == 12 and not alt_book.banking_report()['loans']
        loan = economic_run.open_run(rule)[0].scenario.economy.banking_report()['loans'][LOAN]
        assert (loan['status'], loan['principal_minor']) == ('paid', 0)
        from scripts.verify_resident_credit import audit
        report = audit(base, rule, model)
        assert report['archive_minus_rule_funded_minor'] == -12
        assert report['arms']['rule']['principal_repaid_minor'] == 12
        with pytest.raises(ValueError, match='registered configuration'):
            audit(base, model, rule)
        transferred = model.rename(tmp_path / 'transferred')
        assert audit(base, rule, transferred) == report
    asyncio.run(run())


def test_application_rejects_wrong_recipient_amount_bank_expiry_and_second_use():
    async def run():
        world = build_closed_economy(config())
        decide = world.policy.decide

        async def extra(obs):
            bundle = await decide(obs)
            if bundle.actor_id not in (ACTOR, 'household:0001') or bundle.tick not in (2, 3):
                return bundle
            additions = [CreditApplicationIntent(intent_id=f'extra:{bundle.tick}:{bundle.actor_id}:{key}',
                offer_fact_id=FACT, bank_id=bank, amount_minor=amount)
                for key, bank, amount in [('same', 'bank:a', 12), ('bank', 'bank:b', 12), ('amount', 'bank:a', 13)]]
            return bundle.model_copy(update={'economic_intents': [*bundle.economic_intents, *additions]})

        world.policy.decide = extra
        for _ in range(3):
            await world.step()
        received = [r for r in world.snapshot.receipts if r.command_id.startswith('command:extra:')]
        assert len(received) == 12
        assert all(r.status == 'rejected' and not r.ledger_tx_ids for r in received)
        assert sum(list(r.reason_codes) == ['credit_offer_already_used'] for r in received) == 1
        assert sum(list(r.reason_codes) == ['credit_offer_unauthorized'] for r in received) == 11
        assert len([e for e in world.scenario.economy.journal if e.kind == 'credit_application']) == 1
    asyncio.run(run())


@pytest.mark.parametrize('fixture', ['apply', 'refusal', 'wrong_offer'])
def test_model_application_and_failure_accounting(tmp_path, fixture):
    async def run():
        base = config()
        seat = {'household_index': 0, 'source': 'synthetic_test'}
        await economic_run.run_new({**base, 'credit_cognition': seat}, tmp_path / 'author', until=2)
        path = tmp_path / 'tape.json'
        economic_run.freeze_offline_responses(tmp_path / 'author', path)
        tape = json.loads(path.read_bytes())
        response = tape['records'][0]['response']
        response['raw']['generator'] = 'credit-test-' + fixture
        if fixture == 'refusal':
            response.update(ok=False, text='', error_code='refusal')
        else:
            response['text'] = json.dumps({'offer_fact_id': FACT if fixture == 'apply' else 'other', 'action': 'apply'})
        arm = tmp_path / 'archived'
        archived = {**base, 'credit_cognition': {**seat, 'source': 'archived_response'}}
        from civitas.cognition.runtime import RunControlError
        if fixture == 'wrong_offer':
            with pytest.raises(RunControlError):
                await economic_run.run_new(archived, arm, response_archive=tape)
            world, store, marker = economic_run.open_run(arm)
            assert marker.tick == 1 and not world.scenario.economy.banking_report()['loans']
            attempt, = world.model_runtime.store.records(run_id=store.identity.run_id)
            assert attempt['state'] == 'failed' and attempt['error']['code'] == 'invalid_response'
            return
        await economic_run.run_new(archived, arm, response_archive=tape)
        store = CommitStore(arm)
        attempt, = store.archived_records()
        decision, = [d for d in store.load(2)[1].decisions if d.actor_id == ACTOR and d.tick == 2]
        trace = decision.belief_patch['cd_credit']
        assert trace['attempt_id'] == attempt['attempt_id']
        assert trace['status'] == ('rule_fallback' if fixture == 'refusal' else 'model_choice')
        assert attempt['state'] == ('failed' if fixture == 'refusal' else 'success')
        funding, = [r for r in store.load(3)[1].data['economic_runtime']['journal']
                    if r['event_id'] == 'bank:3:fund:' + LOAN]
        assert funding['details']['disbursed_minor'] == 12
        assert (await economic_run.replay_run(arm, destination=tmp_path / 'replay'))['status'] == 'matched'
    asyncio.run(run())


def test_terms_registration_binds_one_household_one_id_and_later_funding():
    for field, value in [('actor', 'household:9999'), ('actor', 'firm:00'), ('tick', 5), ('bank', 'bank:other')]:
        invalid = config()
        invalid['banking']['credit_offer'][field] = value
        with pytest.raises(ValueError):
            build_closed_economy(invalid)
    invalid = config()
    invalid['banking']['operations'] = [invalid['banking']['credit_offer']]
    with pytest.raises(ValueError, match='bank operation'):
        build_closed_economy(invalid)
    invalid = {**config(), 'credit_cognition': {'household_index': 1}}
    with pytest.raises(ValueError, match='configured household'):
        build_closed_economy(invalid)


@pytest.mark.parametrize(('bank_cash', 'status', 'disbursed'), [(0, 'refused', 0), (12, 'active', 12)])
def test_prefunded_credit_rechecks_reserves_and_recovers_before_commit(tmp_path, bank_cash, status, disbursed):
    async def run():
        base = config()
        base['banking'].update(credit_creation_mode='prefunded_intermediation')
        base['banking']['initial_bank_cash']['bank:a'] = bank_cash
        await economic_run.run_new(base, tmp_path / 'run', until=1)
        for tick in (2, 3):
            world, store, before = economic_run.open_run(tmp_path / 'run')

            def interrupt(point):
                if point == 'before_marker':
                    raise OSError('interrupt credit commit')

            store.fault_hook = interrupt
            with pytest.raises(OSError, match='interrupt credit'):
                await world.step()
            assert store.markers()[-1] == before
            await economic_run.resume_run(tmp_path / 'run', until=tick)
        book = economic_run.open_run(tmp_path / 'run')[0].scenario.economy
        assert book.banking_report()['loans'][LOAN]['status'] == status
        funding, = [e for e in book.journal if e.event_id == 'bank:3:fund:' + LOAN]
        assert funding.details.get('disbursed_minor', 0) == disbursed
        assert sum(p.amount_minor for p in funding.postings if p.entity_id == ACTOR and p.account_code == 'cash') == disbursed
        if not disbursed:
            assert funding.details['reason'] == 'funding_or_capital_recheck' and not funding.postings
        replay = await economic_run.replay_run(tmp_path / 'run', destination=tmp_path / 'replay')
        assert (replay['status'], replay['compared_ticks']) == ('matched', 3)
    asyncio.run(run())
