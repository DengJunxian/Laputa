"""Shared frozen-run and archive checks for single-opportunity previews."""
from civitas.application import economic_run
from civitas.cognition.cache import semantic_key
from civitas.cognition.invocations import ModelInvocationStore
from civitas.cognition.runtime import RunControlError
from civitas.cognition.supplier_archive import SupplierResponseArchive
from civitas.contracts.base import content_hash
from civitas.storage.commit_store import CommitStore, atomic_json, read_json


async def run_choice_pair(destination, response_archive, plan_for, recompute, *, cognition_field, tick_field):
    plan, tape = await freeze_choice_pair(destination, response_archive, plan_for,
                                          cognition_field=cognition_field, tick_field=tick_field)
    return await execute_choice_pair(destination, plan, tape, recompute)


async def freeze_choice_pair(destination, response_archive, plan_for, *, cognition_field, tick_field):
    identity = economic_run.current_implementation()
    preliminary = plan_for(None, identity)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    if response_archive is None:
        author = preliminary['arms']['archive']
        synthetic = {**author, cognition_field: {**author[cognition_field], 'source': 'synthetic_test'}}
        await economic_run.run_new(synthetic, destination/'fixture-author', until=preliminary[tick_field])
        rows = CommitStore(destination/'fixture-author').archived_records()
        response_archive = {'records': [{key: row[key] for key in ('request', 'response')} for row in rows]}
    tape = SupplierResponseArchive.model_validate(response_archive)
    plan = plan_for(tape, identity)
    atomic_json(destination/'response-archive.json', tape.model_dump(mode='json'))
    atomic_json(destination/'plan.json', {'plan': plan, 'plan_hash': content_hash(plan)})
    return plan, tape


async def execute_choice_pair(destination, plan, tape, recompute, *, progress=None):
    if any(path.exists() or path.is_symlink() for path in (destination/side for side in plan['arms'])):
        raise FileExistsError('Frozen comparison already has an execution archive')
    for side, config in plan['arms'].items():
        if progress is not None:
            progress(side, 'running')
        try:
            execution = await economic_run.run_new(config, destination/side,
                response_archive=tape.model_dump(mode='json') if side == 'archive' else None)
        except (RunControlError, OSError, ValueError):
            if progress is not None:
                progress(side, 'failed')
            if not (destination/side/'identity.json').is_file():
                raise
        else:
            if progress is not None:
                progress(side, 'completed' if execution['status'] == 'completed' else 'incomplete')
    result = recompute(destination, plan, tape)
    atomic_json(destination/'shareable-comparison.json', result)
    return result


def load_choice_pair(root, plan_for):
    sealed = read_json(root/'plan.json')
    tape = SupplierResponseArchive.model_validate(read_json(root/'response-archive.json'))
    plan = plan_for(sealed['plan'], tape, economic_run.current_implementation())
    if content_hash(sealed) != content_hash({'plan': plan, 'plan_hash': content_hash(plan)}):
        raise ValueError('Frozen choice plan or runtime changed')
    return plan, tape


async def start_registered_choice(root, plan_hash, plan_for, recompute, *, progress):
    plan, tape = load_choice_pair(root, plan_for)
    if content_hash(plan) != plan_hash:
        raise ValueError('Registered choice plan changed before execution')
    return await execute_choice_pair(root, plan, tape, recompute, progress=progress)


def audit_choice_pair(root, plan_for, recompute):
    plan, tape = load_choice_pair(root, plan_for)
    result = recompute(root, plan, tape)
    if content_hash(read_json(root/'shareable-comparison.json')) != content_hash(result):
        raise ValueError('Published comparison differs from the independent audit')
    return result


def open_choice_arm(root, plan, side):
    store = CommitStore(root/side)
    identity = store.identity
    expected = plan['archive_hash'] if side == 'archive' else None
    if (identity.config != plan['arms'][side] or identity.metadata.get('parent') is not None
            or identity.code_hash != plan['implementation']['source_hash']
            or identity.metadata['implementation']['dependency_hash'] != plan['implementation']['dependency_hash']
            or identity.metadata.get('response_archive_hash') != expected):
        raise ValueError('Run identity differs from the registered choice comparison')
    return store


def choice_attempts(store, tape, projected):
    local = SupplierResponseArchive.model_validate(read_json(store.root/'response-archive.json'))
    if content_hash(local) != content_hash(tape):
        raise ValueError('Run response differs from its frozen archive')
    attempts = ModelInvocationStore(store.root/'model-control.sqlite', read_only=True).records(
        run_id=store.identity.run_id)
    frozen = tape.records[0]
    expected_observation = projected.model_dump(mode='json') if projected is not None else None
    for row in attempts:
        if (semantic_key(row['request']) != semantic_key(frozen.request) or row['accounted']
                or (row['response'] is not None and row['response'] != frozen.response.model_dump(mode='json'))
                or (expected_observation is not None and row['request']['observation'] != expected_observation)):
            raise ValueError('Invocation is not the frozen authorized choice')
    return attempts
