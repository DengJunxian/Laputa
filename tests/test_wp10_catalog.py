from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from civitas.cli_v4 import main
from civitas.experiments.scenario_catalog import CATALOG, ScenarioSuite, inference, run_suite
from civitas.storage.commit_store import atomic_json, read_json

SPEC = Path(__file__).resolve().parents[1] / 'examples/wp10-scenarios.json'


def test_registered_security_arms_complete_with_explicit_no_trade_boundary():
    raw = ScenarioSuite.model_validate_json(SPEC.read_bytes()).model_dump(mode='json')
    raw['scenarios']['A']['seeds'] = [7]
    assert set(CATALOG) == set('ABCDE')
    assert {loop for scenario in CATALOG.values() for loop in scenario['loops']} == {
        'L1', 'L2', 'L3', 'L4', 'L5'}
    with TemporaryDirectory() as temp:
        root = Path(temp)/'run'
        report = run_suite(raw, root, scenarios=('A',), workers=2)
        assert report['status'] == 'completed'
        assert report['assigned_worlds'] == report['completed_worlds'] == 5
        assert report['plan_hash'] == read_json(root/'plan.json')['plan_hash']
        assert report['cases'][1]['rows']['unfunded']['drawdown'] is None
        assert all(arm['replay']['status'] == 'matched'
                   for result in report['cases'] for arm in result['rows'].values())


def test_joint_security_world_cannot_replace_a_registered_catalog_domain():
    raw = read_json(SPEC)
    raw['scenarios']['A']['config'] = read_json(SPEC.parent / 'closed-economy-security-feedback.json')
    with pytest.raises(ValueError, match='select their declared domains'):
        ScenarioSuite.model_validate(raw)


def test_unobservable_worlds_remain_in_the_denominator():
    rows = [
        {'scenario':'A', 'seed':1, 'boundary':False, 'gate':True,'outcomes':{'A1':0.2}},
        {'scenario':'A', 'seed':2, 'boundary':False, 'gate':True,'outcomes':{'A1':None}},
    ]
    observed = inference(rows)['A1']
    assert observed['assigned_roots'] == 2 and observed['observed_roots'] == 1
    assert observed['ci95'] is None and observed['p_holm'] is None


def test_audit_reports_missing_replay_and_preserves_frozen_world(tmp_path, capsys):
    import json

    raw = ScenarioSuite.model_validate_json(SPEC.read_bytes()).model_dump(mode='json')
    raw['scenarios']['A']['seeds'] = [7]
    root = tmp_path/'audit'
    run_suite(raw, root, scenarios=('A',))
    plan = (root/'plan.json').read_bytes()
    report = (root/'report.json').read_bytes()
    assert main(['audit-scenarios', str(root)]) == 0
    assert json.loads(capsys.readouterr().out)['verified_worlds'] == 5

    (root/'A-7/S0H0-replay/report.json').rename(tmp_path/'missing-replay.json')
    assert main(['audit-scenarios', str(root)]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed['assigned_worlds'] == 5 and failed['verified_worlds'] == 4
    assert 'A-7/S0H0' in failed['failures']
    assert (root/'plan.json').read_bytes() == plan
    assert (root/'report.json').read_bytes() == report


def test_fiscal_feedback_exactly_at_floor_is_a_valid_no_payment(tmp_path):
    import asyncio

    from civitas.experiments.scenario_catalog import _fiscal_feedback

    spec = ScenarioSuite.model_validate_json(SPEC.read_bytes())
    config = spec.scenarios['E'].config.model_copy(update={'root_seed':450002})
    result = asyncio.run(_fiscal_feedback(config,tmp_path/'exact-floor'))
    assert result['gate'] and result['completed_worlds']==3
    periods = result['rows']['published_floor']['responses']
    assert periods and all(r['rule']['consumed_units']==r['rule']['floor_units'] for r in periods)
    assert all(r['paid_minor']==r['requested_minor']==0 for r in periods)
    assert result['rows']['fixed']['responses'][0]['paid_minor']==8


def test_archive_rejects_synchronously_changed_outcomes_and_wrong_treatments(tmp_path):
    from civitas.contracts.base import content_hash
    from civitas.experiments.scenario_archive import audit_suite

    raw = ScenarioSuite.model_validate_json(SPEC.read_bytes()).model_dump(mode='json')
    raw['scenarios']['A']['seeds'] = [410001]
    root = tmp_path/'archive'
    report = run_suite(raw, root, scenarios=('A',))
    assert audit_suite(root)['status'] == 'completed'
    case_path = root/'A-410001/case.json'
    case = read_json(case_path)
    correct = case['outcomes']['A1']
    assert correct is not None
    case['outcomes']['A1'] = correct + .25
    report['cases'][0]['outcomes']['A1'] = correct + .25
    report['inference'] = inference(report['cases'])
    atomic_json(case_path, case)
    atomic_json(root/'report.json', report)
    rejected = audit_suite(root)
    assert rejected['status'] == 'failed'
    assert 'A-410001' in rejected['failures']
    assert rejected['recomputed_inference']['A1']['mean'] == correct

    case['outcomes']['A1'] = correct
    atomic_json(case_path, case)
    report['cases'][0]['outcomes']['A1'] = correct
    report['inference'] = inference(report['cases'])
    sealed = read_json(root/'plan.json')
    sealed['plan']['spec']['scenarios']['A']['config']['securities']['signal_gap_minor'] += 1
    sealed['plan_hash'] = report['plan_hash'] = content_hash(sealed['plan'])
    atomic_json(root/'plan.json', sealed)
    atomic_json(root/'report.json', report)
    rejected = audit_suite(root)
    assert rejected['status'] == 'failed' and rejected['verified_worlds'] == 0
    assert 'A-410001/S1H1' in rejected['failures']


def test_suite_resume_only_starts_unattempted_assignments(tmp_path, monkeypatch):
    import pytest
    from civitas.experiments import scenario_catalog

    raw = ScenarioSuite.model_validate_json(SPEC.read_bytes()).model_dump(mode='json')
    raw['scenarios']['A']['seeds'] = [410001, 410002]
    root = tmp_path/'interrupted'
    evaluate = scenario_catalog._evaluate_case

    async def interrupt(scene, config, path, boundary):
        if config.root_seed == 410002:
            raise KeyboardInterrupt
        return await evaluate(scene,config,path,boundary)

    with monkeypatch.context() as m:
        m.setattr(scenario_catalog,'_evaluate_case',interrupt)
        with pytest.raises(KeyboardInterrupt):
            run_suite(raw,root,scenarios=('A',))

    plan = (root/'plan.json').read_bytes()
    first = (root/'A-410001/case.json').read_bytes()
    assert (root/'A-410002').is_dir() and not (root/'A-boundary').exists()
    changed = ScenarioSuite.model_validate(raw).model_dump(mode='json')
    changed['scenarios']['A']['seeds'].append(410003)
    with pytest.raises(ValueError,match='original suite plan'):
        run_suite(changed,root,scenarios=('A',),resume=True)
    report = run_suite(raw,root,scenarios=('A',),resume=True)
    assert report['status'] == 'failed'
    assert report['assigned_worlds'] == 9 and report['completed_worlds'] == 5
    assert report['cases'][1]['status'] == 'interrupted'
    assert not list((root/'A-410002').iterdir())
    assert (root/'A-boundary/case.json').is_file()
    assert (root/'plan.json').read_bytes() == plan
    assert (root/'A-410001/case.json').read_bytes() == first
    assert main(['verify-scenarios','--output',str(root),'--resume']) == 1
