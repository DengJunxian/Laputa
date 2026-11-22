import asyncio
import json

import pytest

from civitas.application.economic_run import run_new
from civitas.application.workbench import open_workspace
from civitas.experiments.intervention_preview import ARMS, KINDS, audit_pair, recipe
from civitas.storage.commit_store import json_bytes


def step(arm, period):
    return next(row for row in [*arm['prior_steps'], arm] if row['cursor']['tick'] == period)


@pytest.mark.parametrize('kind', KINDS)
def test_intervention_side_by_side_recomputes_actual_mechanism(tmp_path, kind):
    async def execute():
        for side in ARMS:
            result = await run_new(recipe(kind, side), tmp_path / side)
            assert result['status'] == 'completed'

    asyncio.run(execute())
    control, treatment = (tmp_path / side for side in ARMS)
    audit = audit_pair(kind, control, treatment)
    left, right = audit['arms']
    assert audit['prefix_states_matched'] == 2
    assert audit['feedback_reached']
    assert all(row['status'] == 'passed' for row in audit['ledger_oracles'])
    for arm in (left, right):
        assert arm['cursor'] == arm['timeline'][-1]['cursor']
        assert [row['cursor']['tick'] for row in arm['timeline']] == list(range(6))
        assert all(edge[0] != edge[1] for edge in arm['network'])
    if kind == 'income':
        base, change, next_ = step(left, 2), step(right, 2), step(right, 3)
        assert (base['metrics']['affected_wage_paid_minor'], change['metrics']['affected_wage_paid_minor']) == (24, 12)
        assert len(change['visible_to']) == 2
        assert change['individuals'][0]['intents'][0]['quantity_units'] == 1
        assert base['individuals'][0]['intents'][0]['quantity_units'] == 2
        assert next_['metrics']['affected_wage_paid_minor'] == 24
    elif kind == 'liquidity':
        low, high = step(left, 3), step(right, 3)
        assert low['visible_to'] == high['visible_to'] == ['household:0001']
        assert low['individuals'][1]['facts'][0]['value']['kind'] == 'withdrawal_window_notice'
        assert high['individuals'][1]['facts'][0]['value']['kind'] == 'verified_withdrawal_delay'
        assert high['metrics']['queued_minor'] == 6
        assert high['metrics']['withdrawals_paid_minor'] == 0
        payment, = [row for row in step(right, 4)['ledger']
                    if row['event']['reference'] == 'notice:household:0001']
        assert payment['details']['paid_minor'] == 6
        assert right['metrics']['outstanding_minor'] == 0
    elif kind == 'revaluation':
        high, low = step(left, 2), step(right, 2)
        assert (high['metrics']['fundamental_minor'], low['metrics']['fundamental_minor']) == (95, 82)
        assert len(low['visible_to']) == 6
        assert (high['metrics']['security_paid_minor'], low['metrics']['security_paid_minor']) == (328, 276)
        assert step(right, 3)['individuals'][0]['choice']['prior_trade_minor'] == low['metrics']['security_close_minor']
        assert any(row[2] == 'fill' for row in right['network'])
    else:
        announcement = step(right, 2)
        assert announcement['visible_to'] == ['household:0000']
        assert announcement['individuals'][0]['fiscal_precaution']['reserve_minor'] == 3
        assert announcement['metrics']['disclosure_paid_minor'] == 2
        assert (step(right, 3)['metrics']['tax_collected_minor'], step(right, 3)['metrics']['transfers_paid_minor']) == (12, 6)
        assert step(right, 4)['metrics']['treasury_cash_minor'] == 13

    download = tmp_path / 'pair.json'
    body = {'tick': 5, 'arms': [{'report': {'views': [{'cursor': arm['cursor'],
                      'intervention_evolution': arm}]}} for arm in (left, right)]}
    download.write_bytes(json_bytes(body))
    assert audit_pair(kind, control, treatment, download=download)['download_verified']
    modified = json.loads(download.read_bytes())
    modified['arms'][0]['report']['views'][0]['cursor']['tick'] = 4
    download.write_bytes(json_bytes(modified))
    with pytest.raises(ValueError, match='differs'):
        audit_pair(kind, control, treatment, download=download)
    with pytest.raises(ValueError, match='registered intervention configuration'):
        audit_pair(kind, treatment, control)


def test_template_freeze_uses_server_recipe_and_is_idempotent(tmp_path):
    with open_workspace(tmp_path, recipe('income', 'control')) as bench:
        request = {'action': 'freeze-intervention', 'kind': 'fiscal', 'side': 'treatment',
                   'request_id': 'a' * 32}
        first = bench.command(request)
        assert bench.command(request) == first
        assert bench.load(first['job'])['config'] == recipe('fiscal', 'treatment')
        assert 'intervention_templates' not in bench.status((first['job'], 'household:0000'))
        with pytest.raises(ValueError, match='used for different inputs'):
            bench.command({**request, 'side': 'control'})
        with pytest.raises(ValueError, match='Invalid workbench command'):
            bench.command({**request, 'config': {}})
