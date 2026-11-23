import asyncio

import pytest

pytest.importorskip('scipy', reason='joint-shock analysis uses the pinned research extra')

from civitas.application import economic_run
from civitas.storage.commit_store import CommitStore
from scripts import joint_shock_pilot as pilot


def test_protocol_rejects_changes_and_marks_do_not_fill_missing_returns(tmp_path):
    tampered = tmp_path / 'protocol.json'
    tampered.write_bytes(pilot.REGISTERED.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='preregistered'):
        pilot.load_protocol(tampered)
    assert pilot.realized_return(None, 100) == (None, 'no_realized_close')
    assert pilot.realized_return(100, None) == (None, 'no_adjacent_prior_realized_close')
    assert pilot.realized_return(125, 100) == (0.25, None)


def test_paired_summary_uses_complete_world_roots_only():
    roots = {'1': {'status': 'eligible', 'endpoint_by_arm': {'treated': 4, 'control': 2}},
             '2': {'status': 'eligible', 'endpoint_by_arm': {'treated': 6, 'control': 2}},
             '3': {'status': 'ineligible'}}
    contrast = {'treated': 1, 'control': -1}
    result = pilot.paired_summary(roots, contrast)
    assert (result['assigned_roots'], result['n_complete_roots'], result['mean'], result['mcse']) == (3, 2, 3, 1)
    assert result['ci95'] == pytest.approx([-9.7062, 15.7062], abs=0.0001)
    roots['2'] = roots['1']
    result = pilot.paired_summary(roots, contrast)
    assert (result['ci95'], result['interval_reason']) == (None, 'zero_observed_sd')


def test_receipt_trace_and_information_matching_from_saved_worlds(tmp_path):
    protocol, fixture = pilot.load_protocol(pilot.REGISTERED)
    engineering_seed = 997

    async def create_traces():
        traces = {}
        for arm in protocol['arm_settings']:
            path = tmp_path / arm
            config = pilot.assigned_configuration(protocol, fixture, engineering_seed, arm)
            result = await economic_run.run_new(config, path)
            assert result['status'] == 'completed'
            traces[arm] = pilot.trace_world(CommitStore(path))
        return traces

    traces = asyncio.run(create_traces())
    assert pilot.evaluate_root(protocol, traces)['status'] == 'eligible'
    shocked = traces['shock_weighted']['periods']
    assert shocked['2']['asset']['realized_return'] is None
    assert shocked['2']['denominators']['wealth_observations'] == 0
    assert shocked['3']['denominators']['wealth_observations'] > 0
    assert shocked['3']['consumption_tx_ids']
    assert shocked['4']['institutions']['fiscal'][0]['details']['consumption_response']['source_tx_ids']
    traces['shock_weighted']['periods']['3']['observation_hash'] = 'mismatch'
    assert pilot.evaluate_root(protocol, traces)['reasons'] == ['exact_information_within_mark_pairs']
