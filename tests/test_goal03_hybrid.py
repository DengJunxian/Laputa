import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from civitas.application import economic_run
from civitas.application.closed_economy import build_closed_economy
from civitas.cognition.providers import ArchivedProvider, SyntheticProvider
from civitas.population.synthetic import synthetic_population_config
from civitas.storage.commit_store import CommitStore, atomic_json

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
ACTOR = 'household:0000'


def prepared_population():
    return [
        {'profile_id': f'profile:{i}', 'initial_cash_minor': 1, 'target_units': 3,
         'basket_share_bps': 10000, 'cash_reserve_minor': 0,
         'behavior': {'regular_wage_minor': 12, 'prudence_buffer_minor': 0,
                      'supplier_priors_bps': {'firm:00': 10000, 'firm:01': 0},
                      'contact_ids': [f'household:{(i + 1) % 4:04d}']}}
        for i in range(4)
    ]


@pytest.mark.parametrize(('field', 'value', 'seller', 'quantity'), [
    ('initial_cash_minor', 12, 'firm:00', 3),
    ('regular_wage_minor', 20, 'firm:00', 3),
    ('prudence_buffer_minor', 8, None, 0),
    ('basket_share_bps', 5000, 'firm:00', 1),
    ('supplier_priors_bps', {'firm:00': 0, 'firm:01': 10000}, 'firm:01', 2),
])
def test_single_household_parameter_changes_real_purchase(field, value, seller, quantity):
    async def execute(override):
        rows = prepared_population()
        if override:
            target = rows[0] if field in rows[0] else rows[0]['behavior']
            target[field] = value
        world = build_closed_economy({
            'households': 4, 'firms': 2, 'ticks': 3, 'initial_price_minor': 6,
            'firm_price_gap_minor': 0, 'min_price_minor': 6, 'max_price_minor': 6,
            'population_input': {'source_id': 'synthetic:controlled', 'data_origin': 'synthetic',
                                 'household_profiles': rows},
        })
        await world.step()
        decision = next(row for row in world.snapshot.decisions if row.actor_id == ACTOR)
        trades = [row for row in world.snapshot.receipts
                  if row.actor_id == ACTOR and row.outcome == 'purchased']
        realized = [(row.counterparty_id, row.executed_quantity, row.cash_delta_minor) for row in trades]
        if field == 'regular_wage_minor':
            firm = world.scenario.economy.account_view('firm:00')
            assert firm.work_in_progress_minor == 0
            journal = world.scenario.economy.journal
            payment = next(row for row in journal
                           if row.kind == 'wage_payment' and row.payload['worker_id'] == ACTOR)
            assert payment.details['paid_minor'] == (20 if override else 12)
            premium = [posting.amount_minor for row in journal for posting in row.postings
                       if posting.account_code == 'wage_variance_expense']
            assert premium == ([8] if override else [])
        return decision.belief_patch['closed_choice']['quoted_prices'], realized

    base_quotes, base_trade = asyncio.run(execute(False))
    quotes, trade = asyncio.run(execute(True))
    assert quotes == base_quotes == {'firm:00': 6, 'firm:01': 6}
    assert base_trade == [('firm:00', 2, -12)]
    assert trade == ([(seller, quantity, -6 * quantity)] if quantity else [])


def test_contact_changes_recipient_and_observation_access(tmp_path):
    async def execute(contact):
        rows = prepared_population()
        rows[0]['behavior']['contact_ids'] = [contact]
        config = {
            'households': 4, 'firms': 2, 'ticks': 3, 'social_mode': 'adaptive',
            'archive_observations': True,
            'initial_price_minor': 6, 'firm_price_gap_minor': 0, 'min_price_minor': 6, 'max_price_minor': 6,
            'population_input': {'source_id': 'synthetic:contacts', 'data_origin': 'synthetic',
                                 'household_profiles': rows},
        }
        destination = tmp_path / contact
        await economic_run.run_new(config, destination)
        store = CommitStore(destination)
        commit, snapshot = store.load(3)
        first_purchase = [message for message in snapshot.messages
                          if message.sender_id == ACTOR
                          and message.body.get('receipt_command_id') == 'command:intent:1:household:0000:purchase']
        assert len(first_purchase) == 1
        message, = first_purchase
        assert message.recipient_id == contact
        for observation in store.observation_rows(commit):
            visible = {m.message_id for m in observation.delivered_messages}
            assert (message.message_id in visible) is (observation.actor_id == contact)
        return message.body['receipt_command_id']

    assert asyncio.run(execute('household:0001')) == asyncio.run(execute('household:0002'))


@pytest.mark.parametrize(('size', 'firms'), [(32, 4), (1000, 40)])
def test_addressed_population_presets_round_trip(size, firms):
    config = synthetic_population_config(size, 7)
    assert config.model_dump_json() == synthetic_population_config(size, 7).model_dump_json()
    assert config.population_input != synthetic_population_config(size, 8).population_input
    profiles = config.population_input.household_profiles
    assert len({p.profile_id for p in profiles}) == size
    assert len({p.initial_cash_minor for p in profiles}) > 1
    assert {p.behavior.prudence_buffer_minor for p in profiles} == {0, 10}
    assert all(len(p.behavior.supplier_priors_bps) == firms for p in profiles)


def test_partner_registration_uses_the_imported_graph():
    config = json.loads((EXAMPLES / 'closed-economy-partner-choice.json').read_bytes())['base_config']
    rows = [{'profile_id': f'profile:{i}', 'initial_cash_minor': 30, 'target_units': 2,
             'basket_share_bps': 10000, 'cash_reserve_minor': 0} for i in range(config['households'])]
    rows[1]['behavior'] = {'regular_wage_minor': 12, 'prudence_buffer_minor': 0,
                           'supplier_priors_bps': {f'firm:{i:02d}': 10000 for i in range(config['firms'])},
                           'contact_ids': ['household:0002', 'household:0003']}
    config['population_input'] = {'source_id': 'synthetic:graph', 'data_origin': 'synthetic',
                                  'household_profiles': rows}
    build_closed_economy(config)
    rows[1]['behavior']['contact_ids'] = ['household:0003']
    with pytest.raises(ValueError, match='two existing and two absent network edges'):
        build_closed_economy(config)


def test_artifact_publish_does_not_leave_partial_or_overwrite(tmp_path):
    path = tmp_path / 'archive.json'
    with patch('civitas.storage.commit_store.os.fsync', side_effect=OSError('failed write')):
        with pytest.raises(OSError, match='failed write'):
            atomic_json(path, {'i': 1}, replace=False)
    assert list(tmp_path.iterdir()) == []
    atomic_json(path, {'i': 1}, replace=False)
    with pytest.raises(FileExistsError):
        atomic_json(path, {'i': 2}, replace=False)
    assert json.loads(path.read_bytes()) == {'i': 1}
    assert list(tmp_path.iterdir()) == [path]


def mixed_configs():
    base = json.loads((EXAMPLES / 'closed-economy-joint-cognition.json').read_bytes())
    base['population_input'] = synthetic_population_config(32, 7).model_dump(mode='json')['population_input']
    overlay = json.loads((EXAMPLES / 'goal03-hybrid-slots.json').read_bytes())
    common = {key: value for key, value in {**base, **overlay}.items() if not key.endswith('_cognition')}
    authored = {**common, **{key: value for key, value in overlay.items() if key.endswith('_cognition')}}
    archived = {key: {**value, 'source': 'archived_response'} if key.endswith('_cognition') else value
                for key, value in authored.items()}
    return common, authored, archived


def test_mixed_archive_durable_commit_and_common_economic_executor(tmp_path):
    async def execute():
        rule, authored, archived = mixed_configs()
        await economic_run.run_new(rule, tmp_path / 'rule')
        await economic_run.run_new(authored, tmp_path / 'author')
        tape_path = tmp_path / 'archive.json'
        assert economic_run.freeze_offline_responses(tmp_path / 'author', tape_path)['records'] == 6
        tape = json.loads(tape_path.read_bytes())
        root = tmp_path / 'archived'
        with patch.object(SyntheticProvider, 'complete', side_effect=AssertionError('regenerated archive')):
            await economic_run.run_new(archived, root, until=1, response_archive=tape)
            world, store, before = economic_run.open_run(root)

            def interrupt(point):
                if point == 'before_marker':
                    raise OSError('before mixed commit')

            store.fault_hook = interrupt
            with pytest.raises(OSError, match='before mixed commit'):
                await world.step()
            assert store.markers()[-1] == before
            assert store.archived_records() == []
            with patch.object(ArchivedProvider, 'complete', side_effect=AssertionError('archive reread on retry')):
                await economic_run.resume_run(root, until=2)
            result = await economic_run.resume_run(root)
            replay = await economic_run.replay_run(root, destination=tmp_path / 'replay')
        assert replay['status'] == 'matched'
        assert replay['state_hash'] == result['state_hash']

        rule_store, mixed_store = CommitStore(tmp_path / 'rule'), CommitStore(root)
        baseline, baseline_state = rule_store.load(1)
        cursor, mixed_state = mixed_store.load(1)
        assert baseline_state.data['economic_runtime'] == mixed_state.data['economic_runtime']
        assert rule_store.observation_rows(baseline) == mixed_store.observation_rows(cursor)
        rule_cursor, rule_at2 = rule_store.load(2)
        mixed_cursor, mix_at2 = mixed_store.load(2)
        assert rule_store.observation_rows(rule_cursor) == mixed_store.observation_rows(mixed_cursor)

        def cooperation_deltas(snapshot):
            return next(row['details']['cash_delta_minor'] for row in snapshot.data['economic_runtime']['journal']
                        if row['kind'] == 'cooperation_settlement' and row['payload']['round_id'] == 'cooperation:2')

        assert cooperation_deltas(rule_store.load(5)[1]) == {'firm:00': -4, ACTOR: 2, 'household:0002': 2}
        assert cooperation_deltas(mixed_store.load(5)[1]) == {'firm:00': -2, ACTOR: 3, 'household:0002': -1}
        limits = mixed_store.identity.metadata['budget_limits']
        assert (limits['max_attempts'], limits['usd_nano'], limits['cny_nano']) == (6, 0, 0)

        rows = mixed_store.archived_records()
        assert len(rows) == 6
        assert all(row['state'] == 'success' and row['accounted'] == 0 for row in rows)
        assert sorted(row['request']['task'] for row in rows) == [
            'cd_cooperation_choice', 'cd_cooperation_choice', 'cd_disclosure_choice',
            'cd_labor', 'cd_supplier', 'cd_withdrawal',
        ]
        disclosure, = [r for r in rows if r['request']['task'] == 'cd_disclosure_choice']
        request = disclosure['request']
        assert request['observation']['delivered_messages'] == []
        own_receipt, = request['observation']['private_memory_refs']
        assert own_receipt['owner_id'] == ACTOR
        message_id = request['task_context']['message_id']
        assert any(m.message_id == message_id and m.recipient_id == 'household:0001' for m in rule_at2.messages)
        assert all(m.message_id != message_id for m in mix_at2.messages)
        cooperation = [r for r in rows if r['request']['task'] == 'cd_cooperation_choice']
        assert [r['parsed']['action'] for r in cooperation] == ['defect', 'defect']
        previous, = cooperation[1]['request']['observation']['private_memory_refs']
        assert previous['owner_id'] == ACTOR
        assert previous['fact_id'].startswith('cooperation-outcome:')
        for state, actor in ((rule_at2, 'household:0003'), (mix_at2, 'household:0005')):
            assert any(r.outcome == 'labor_offer_accepted' and r.actor_id == actor for r in state.receipts)

        bad = {**archived, 'disclosure_cognition': {**archived['disclosure_cognition'], 'tick': 3}}
        with pytest.raises(ValueError, match='registered'):
            await economic_run.run_new(bad, tmp_path / 'mismatch', response_archive=tape)
        assert not (tmp_path / 'mismatch').exists()

    asyncio.run(execute())
