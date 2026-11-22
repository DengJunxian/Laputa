import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import goal05_research as study

PROTOCOL = Path(__file__).resolve().parents[1] / 'examples/goal05-research-protocol.json'


def test_frozen_pilot_root_assignments_and_exclusive_output(tmp_path):
    destination = tmp_path / 'plan.json'
    plan = study.freeze(PROTOCOL, destination)
    assert study.load_plan(destination) == plan
    assert plan['phase'] == 'pilot'
    allocations = [plan['suite']['scenarios'][scene]['seeds'] for scene in ('A','D')]
    allocations.append(plan['strategy_seeds'])
    assert [len(group) for group in allocations] == [8, 12, 4]
    assert len(set(seed for group in allocations for seed in group)) == 24
    with pytest.raises(FileExistsError):
        study.freeze(PROTOCOL,destination)
    changed = {**plan, 'phase': 'confirmation'}
    destination.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='Frozen matrix plan'):
        study.load_plan(destination)


def test_planning_counts_stopped_roots_in_eligibility_rate():
    protocol = json.loads(PROTOCOL.read_bytes())
    spec = protocol['studies']['D1']
    eligible = [0.0, 0.5, 0.5, 0.0, 0.5, 0.5, 0.5, 0.5]
    question = {'valid': True, 'blocks': [{'value': v} for v in [*eligible, None, None, None, None]]}
    result = study.allocate(protocol, spec, question)
    assert result['eligible_pilot_roots'] == 8
    assert 0 < result['wilson_lower_eligible_fraction'] < 8/12
    assert result['allocated_roots'] % 4 == 0
    assert result['required_allocated_roots'] > result['required_eligible_roots']
    with pytest.raises(ValueError, match='cannot estimate'):
        study.allocate(protocol, spec, {**question, 'valid': False})


def test_failed_strategy_assignment_keeps_original_attempt_on_resume(tmp_path):
    seed = 530005
    with patch.object(study, 'run_labor_choice', side_effect=OSError('injected disk fault')) as attempt:
        asyncio.run(study.execute_strategy(tmp_path, seed, {}))
        before = (tmp_path / f'{seed}-receipt.json').read_bytes()
        asyncio.run(study.execute_strategy(tmp_path, seed, {}))
        assert attempt.call_count == 1
        assert (tmp_path / f'{seed}-receipt.json').read_bytes() == before
    row, pending = study.strategy_rows(tmp_path, {'strategy_seeds': [seed, seed+1]})
    assert row['status'] == 'failed'
    assert pending['status'] == 'not_run'


def test_missing_primary_roots_and_zero_variance_keep_null_inference():
    protocol = json.loads(PROTOCOL.read_bytes())
    market = [{'scenario': 'A', 'boundary': False, 'seed': 11, 'gate': False, 'rows': {}},
              {'scenario': 'A', 'boundary': False, 'seed': 12, 'gate': False, 'rows': {}}]
    network = {'scenario': 'D', 'assigned_worlds': 15, 'completed_worlds': 15,
               'd1': {'incomplete_roots': [], 'mechanism_mismatches': [],
                      'eligible': {'contrasts': [{'seed': 21, 'delta': 0}, {'seed': 22, 'delta': 0}]}}}
    spec = {'phase': 'confirmation', 'suite': {'scenarios': {'D': {'seeds': [21,22,23]}}},
            'protocol': protocol, 'allocation': {name: {'required_eligible_roots': 2} for name in study.SCENES}}
    out = study.question_summary({'cases': [*market, network]}, spec)
    assert out['A1']['assigned_roots'] == 2
    assert out['A1']['p_raw'] is None and out['A1']['ci95'] is None
    assert out['D1']['blocks'][-1]['value'] is None
    assert out['D1']['p_holm'] is None and out['D1']['mean'] == 0


def test_confirmation_freezes_from_same_scenarios_with_disjoint_roots(tmp_path):
    pilot = tmp_path / 'pilot'
    pilot.mkdir()
    before = study.freeze(PROTOCOL, pilot/'plan.json')
    points = [0.0, 0.003, 0.006, 0.009]
    verified = {'questions': {name: {'valid': True, 'blocks': [{'value': value} for value in points]}
                              for name in study.SCENES}}
    with patch.object(study, 'audit', return_value=verified):
        confirmation = study.freeze(PROTOCOL, tmp_path/'confirmation.json', pilot)
    assert confirmation['pilot_plan_hash'] == before['plan_hash']
    assert confirmation['pilot_driver_sha256'] == before['implementation']['driver_sha256']
    for scene in study.SCENES.values():
        pre = before['suite']['scenarios'][scene]
        post = confirmation['suite']['scenarios'][scene]
        assert not set(pre['seeds']) & set(post['seeds'])
        assert {k:v for k,v in pre['config'].items() if k!='root_seed'} == {
            k:v for k,v in post['config'].items() if k!='root_seed'}
