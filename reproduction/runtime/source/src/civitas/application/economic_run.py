"""Thin offline C/D runner over the existing commit/checkpoint/replay engine.

World commit markers are authoritative. All exports below are fixed-cursor,
rebuildable research views and are never used to execute an economic action.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid

from civitas.application.closed_economy import build_closed_economy
from civitas.application.labor_projection import project_labor_graph
from civitas.application.run import implementation_identity
from civitas.contracts.base import content_hash
from civitas.experiments import registry
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig, LaborShiftIntervention
from civitas.scenarios.closed_economy_labor_outcomes import research_labor_outcomes
from civitas.storage.checkpoint import _save_verified, attach, restore
from civitas.storage.commit_store import CommitStore, atomic_json, read_json, sync_directory
from civitas.storage.history import parent_reference, publish_directory, validate_parent
from civitas.storage.replay import verification_replay


APPLICATION = 'ecosim.closed-economy.v4'
NO_INTERVENTION = {'schema_version': 1, 'kind': 'none'}
PROJECTION_TABLES = frozenset({'metrics.json','economy.json','social.json','scenario.json',
                             'receipts.json','decisions.json','messages.json','relations.json'})


def parse_config(config) -> ClosedEconomyConfig:
    return ClosedEconomyConfig.model_validate(config)


def current_implementation() -> dict:
    identity = implementation_identity()
    identity['dependencies']['sqlite_runtime'] = sqlite3.sqlite_version
    identity['dependency_hash'] = content_hash(identity['dependencies'])
    return identity


def create_run(config, destination, *, run_id=None, terminal_tick=None,
               provider_config=None, project_ledger=None, limits=None, response_archive=None):
    config = parse_config(config)
    if (config.cognition_mode == 'archived_response') != (response_archive is not None):
        raise ValueError('Archived decision mode requires an explicit fixed response archive')
    if (terminal_tick is None) != (config.stopping_hazard_bps is None):
        raise ValueError('A stopping hazard requires a controller-assigned terminal tick')
    if terminal_tick is not None and (type(terminal_tick) is not int or not 1 <= terminal_tick <= config.ticks):
        raise ValueError('Terminal tick must be within the administrative horizon')
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    implementation = current_implementation()
    run_id = run_id or destination.name
    metadata = {'application': APPLICATION, 'implementation': implementation,
                'data_origin': 'synthetic_closed_economy', 'provider_mode': config.cognition_mode,
                **({'external_api_calls': 0} if config.cognition_mode != 'live' else {}),
                **({'observation_archive':config.observation_archive_format} if config.archive_observations else {}),
                'economic_model': ('single_prefunded_security_session' if config.securities is not None else
                    config.banking.credit_creation_mode if config.banking is not None else 'single_prefunded_settlement_token'),
                **({'terminal_tick': terminal_tick} if terminal_tick is not None else {})}
    runtime = None
    if config.cognition_mode != 'live' and any(v is not None for v in (provider_config, project_ledger, limits)):
        raise ValueError('Provider, project ledger and explicit limits require registered live slots')
    if config.cognition_mode != 'rule':
        from civitas.application.persistent_run import runtime_for
        from civitas.cognition.budget import BudgetController, BudgetLimits
        from civitas.cognition.invocations import ModelInvocationStore
        from civitas.cognition.providers import ProviderConfig

        supplier = config.supplier_cognition
        from civitas.cognition.supplier_archive import MAX_ARCHIVE_SLOTS

        choice_slots = config.choice_slots
        slots = (len(supplier.household_indices)
                 * (len(supplier.ticks) + ((len(supplier.ticks) - 1) if supplier.reflect else 0))) if supplier else 0
        slots = slots + len(choice_slots) or 1
        if config.cognition_mode != 'live' and slots > MAX_ARCHIVE_SLOTS:
            raise ValueError('The structured offline schedule exceeds the response archive capacity')
        if config.cognition_mode == 'archived_response':
            return _create_archived_run(config, destination, run_id, metadata, response_archive, slots)
        if config.cognition_mode == 'live':
            if provider_config is None or project_ledger is None or limits is None:
                raise ValueError('Live C/D requires a provider descriptor, cumulative project ledger and run budget')
            import importlib.metadata
            provider = ProviderConfig.model_validate(provider_config)
            limits = BudgetLimits.model_validate(limits)
            if provider.source != 'live' or (config.population_input is not None
                                             and config.population_input.data_origin != 'synthetic'):
                raise ValueError('Live C/D requires an explicit live provider and synthetic inputs')
            control = Path(project_ledger).resolve(strict=True)
            project_limits = BudgetController.project_limits(ModelInvocationStore(control, read_only=True))
            request_cap = provider.max_input_tokens + provider.max_output_tokens
            cost_cap = provider.max_input_tokens * provider.price.input_nano + provider.max_output_tokens * provider.price.output_nano
            currency = provider.price.currency.lower() + '_nano'
            if not 1 <= limits.max_attempts <= slots * provider.max_attempts or (
                    limits.max_tokens != limits.max_attempts * request_cap
                    or getattr(limits, currency) != limits.max_attempts * cost_cap
                    or (limits.usd_nano and limits.cny_nano)):
                raise ValueError('Run limits must match the declared number of attempts and per-attempt reservations')
            metadata.update(project_budget_hash=content_hash(project_limits),
                            sdk_version=importlib.metadata.version('openai'))
        else:
            control = destination.with_name(destination.name + '-model-control.sqlite')
            control.parent.mkdir(parents=True, exist_ok=True)
            control.touch(exist_ok=False)
            model = ('synthetic-cd-supplier' if supplier is not None else
                     f'synthetic-cd-{choice_slots[0][0][3:]}' if choice_slots else 'synthetic-cd-cooperation')
            provider = ProviderConfig(model=model, revision='fixture-v1', max_attempts=1)
            limits = BudgetLimits(usd_nano=0, cny_nano=0, max_attempts=slots,
                                  max_tokens=slots * (provider.max_input_tokens + provider.max_output_tokens))
        metadata.update(control_ledger=str(control), provider_config=provider.model_dump(mode='json'),
                        budget_limits=limits.model_dump(mode='json'))
        runtime = runtime_for(config, metadata, run_id, run_id, run_id, cd_structured=supplier is not None or bool(choice_slots))
    world = build_closed_economy(config, runtime=runtime)
    store = CommitStore.create(destination, world, config=config.model_dump(mode='json'),
        code_hash=implementation['source_hash'], run_id=run_id, metadata=metadata)
    attach(world, store)
    if runtime is not None:
        store.receipt_reader = lambda: runtime.store.records(run_id=run_id)
        world.model_runtime = runtime
    return world, store


def _create_archived_run(config, destination, run_id, metadata, response_archive, slots):
    from civitas.application.persistent_run import runtime_for
    from civitas.cognition.budget import BudgetLimits
    from civitas.cognition.supplier_archive import SupplierResponseArchive

    archive = SupplierResponseArchive.model_validate(response_archive)
    if not config.supplier_cognition and len(config.choice_slots) == 1:
        task, seat, tick = config.choice_slots[0]
        archive.validate_choice_slot(task, f'household:{seat:04d}', tick)
    elif config.choice_slots:
        archive.validate_registration(config)
    else:
        archive.validate_slots(config.supplier_cognition)
    provider = archive.provider_config
    limits = BudgetLimits(usd_nano=0, cny_nano=0, max_attempts=slots,
                          max_tokens=slots * (provider.max_input_tokens + provider.max_output_tokens))
    ledger = 'model-control.sqlite'
    metadata.update(response_archive_hash=content_hash(archive), control_ledger=str(destination / ledger),
                    provider_config=provider.model_dump(mode='json'), budget_limits=limits.model_dump(mode='json'))
    with publish_directory(destination, create_staging=False) as staging:
        temporary_ledger = staging.parent / ledger
        runtime = runtime_for(config, metadata, run_id, run_id, run_id, cd_structured=True,
                              tape=archive.replay_rows(), control_path=temporary_ledger)
        world = build_closed_economy(config, runtime=runtime)
        CommitStore.create(staging, world, config=config.model_dump(mode='json'),
                           code_hash=metadata['implementation']['source_hash'], run_id=run_id, metadata=metadata)
        temporary_ledger.rename(staging / ledger)
        atomic_json(staging / 'response-archive.json', archive.model_dump(mode='json'))
    world, store, _ = open_run(destination)
    return world, store


def verify_implementation(store):
    if store.identity.metadata.get('application') != APPLICATION:
        raise ValueError('This runner only opens its own v4 closed-economy runs')
    current = current_implementation()
    previous = store.identity.metadata['implementation']
    if (current['source_hash'] != store.identity.code_hash or
            current['dependency_hash'] != previous['dependency_hash']):
        raise ValueError('Code/dependency identity changed; use an explicit new run or migration')
    return current


def freeze_offline_responses(root, destination):
    from civitas.cognition.supplier_archive import SupplierResponseArchive

    store = CommitStore(root)
    verify_implementation(store)
    config = parse_config(store.identity.config)
    if config.cognition_mode != 'synthetic_test' or not config.structured_sources:
        raise ValueError('Freeze responses from a structured synthetic run')
    records = [{key: row[key] for key in ('request', 'response')} for row in store.archived_records()]
    archive = SupplierResponseArchive.model_validate({'records': records})
    archive.validate_registration(config)
    destination = Path(destination).resolve()
    if destination.is_relative_to(store.root):
        raise ValueError('Freeze responses outside the source run')
    atomic_json(destination, archive.model_dump(mode='json'), replace=False)
    return {'status': 'frozen', 'records': len(archive.records), 'archive_hash': content_hash(archive)}


def _restore_model_runtime(config, store, *, replay_path=None):
    if config.cognition_mode == 'rule':
        return None
    from civitas.application.persistent_run import runtime_for, verify_model_ledger

    identity = store.identity
    tape = None
    if replay_path is not None:
        tape = store.archived_records()
    elif config.cognition_mode == 'archived_response':
        from civitas.cognition.supplier_archive import SupplierResponseArchive

        archive = SupplierResponseArchive.model_validate(read_json(store.root / 'response-archive.json'))
        if content_hash(archive) != identity.metadata['response_archive_hash']:
            raise ValueError('Frozen response archive hash mismatch')
        tape = archive.replay_rows()
    runtime = runtime_for(config, identity.metadata, identity.run_id, identity.lineage_id, identity.branch_id,
                          replay_path=replay_path, tape=tape,
                          reopening=True, cd_structured=config.supplier_cognition is not None or bool(config.choice_slots))
    if replay_path is None:
        verify_model_ledger((store,), runtime.store)
        store.receipt_reader = lambda: runtime.store.records(run_id=identity.run_id)
    return runtime


def _with_shift(config, intervention, fork_tick, terminal_tick):
    if intervention.shock.tick != fork_tick + 1 or intervention.shock.tick > terminal_tick:
        raise ValueError('Labor shift must take effect at the next uncommitted period within the frozen horizon')
    if config.labor_income_shock is not None:
        raise ValueError('A labor shift is already scheduled in this lineage')
    if (config.cognition_mode != 'rule' or config.securities is not None
            or config.labor_transfer is not None or config.demand is not None):
        raise ValueError('Labor shift is authorized only for ordinary rule-world employment')
    return parse_config({**config.model_dump(mode='json'), 'labor_income_shock': intervention.shock})


def _execution_config(store):
    config = parse_config(store.identity.config)
    terminal_tick = store.identity.metadata.get('terminal_tick', config.ticks)
    for member in reversed(validate_parent(store)):
        metadata = member.identity.metadata
        intervention = metadata.get('intervention', NO_INTERVENTION)
        if intervention != NO_INTERVENTION:
            if 'parent' not in metadata:
                raise ValueError('A branch intervention requires a recorded parent')
            config = _with_shift(config, LaborShiftIntervention.model_validate(intervention),
                                 metadata['parent']['tick'], terminal_tick)
    return config


def _open_run(store, *, tick=None):
    verify_implementation(store)
    config = _execution_config(store)
    runtime = _restore_model_runtime(config, store)
    world = build_closed_economy(config, runtime=runtime)
    marker = restore(world, store, tick=tick)
    if runtime is not None:
        world.model_runtime = runtime
        for row in store.model_records(marker):
            runtime.emit(row)
    return world, store, marker


def open_run(root, *, tick=None):
    return _open_run(CommitStore(root), tick=tick)


def prepare_branch(root, *, tick, intervention=None):
    source = CommitStore(root)
    if parse_config(source.identity.config).cognition_mode != 'rule':
        raise ValueError('Branches require a rule run')
    if type(tick) is not int or tick < 0:
        raise ValueError('A branch requires a committed tick')
    world, _, selected = _open_run(source, tick=tick)
    payload = NO_INTERVENTION
    if intervention is not None:
        intervention = LaborShiftIntervention.model_validate(intervention)
        _with_shift(world.scenario.config, intervention, tick,
                    source.identity.metadata.get('terminal_tick', world.scenario.config.ticks))
        payload = intervention.model_dump(mode='json')
    return source, selected, world.snapshot, payload


def branch_run(root, *, tick, destination, intervention=None):
    """Publish a rule child at a verified commit, optionally scheduling one shift."""
    destination = Path(destination).resolve()
    if destination.is_relative_to(Path(root).resolve()):
        raise ValueError('Branch output must be outside the parent run')
    source, selected, snapshot, payload = prepare_branch(root, tick=tick, intervention=intervention)
    run_id = destination.name + '-' + uuid.uuid4().hex
    identity = source.identity.model_dump(mode='json')
    identity.update(run_id=run_id, branch_id=run_id)
    parent = parent_reference(source, selected)
    identity['metadata'].update(parent=parent, intervention=payload)
    with publish_directory(destination, create_staging=False) as staging:
        store = CommitStore.initialize(staging, identity, snapshot)
        validate_parent(store)
        initial = store.markers()[0]
        result = {'status': 'planned', 'path': str(destination), 'run_id': run_id,
                  'lineage_id': store.identity.lineage_id, 'branch_id': run_id, 'tick': initial.tick,
                  'commit_hash': initial.commit_hash, 'state_hash': initial.state_hash,
                  'parent': parent, 'intervention': payload, 'shared_rng': store.identity.rng,
                  'external_api_calls': 0}
        registry.record(staging, result)
    return result


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_projection(root, *, destination=None, tick=None, _verified=None):
    """All exported tables use exactly the same verified commit and snapshot."""
    world, store, marker = open_run(root, tick=tick) if _verified is None else _verified
    snapshot = world.snapshot
    destination = (Path(destination).resolve() if destination is not None else
                   store.root/'projections'/marker.commit_hash)
    cursor = {'source_commit_hash': marker.commit_hash, 'state_hash': marker.state_hash,
              'tick': marker.tick, 'world_schema_version': 4,
              'projection_version': 'closed-economy.v1', 'audience': 'researcher', 'authority': False}
    if destination.exists():
        manifest = read_json(destination/'manifest.json')
        if (manifest['cursor'] != cursor or manifest['run_id'] != store.identity.run_id
                or manifest['config_hash'] != store.identity.config_hash
                or manifest['source_hash'] != store.identity.code_hash
                or set(manifest['files']) != PROJECTION_TABLES):
            raise ValueError('Existing projection belongs to a different cursor')
        for name, expected in manifest['files'].items():
            if _sha(destination/name) != expected:
                raise ValueError('Existing projection was modified; export into a fresh directory')
        return {'path': str(destination), **manifest}
    metrics = world.scenario.metrics(world._context(snapshot))
    transfer = world.scenario.config.labor_transfer
    network = snapshot.data.get('closed_social', {}).get('network')
    # These canonical JSON views intentionally retain units, identities and
    # failed receipts. A future optional Arrow writer may project the same data.
    tables = {
        'metrics.json': {'cursor': cursor, 'metrics': metrics},
        'economy.json': {'cursor': cursor, 'state': snapshot.data['economic_runtime']},
        'social.json': {'cursor': cursor, 'state': snapshot.data.get('closed_social', {})},
        'scenario.json': {'cursor': cursor, 'state': snapshot.data.get('closed_economy', snapshot.data.get('security_market', {})),
                          **({'contract_outcomes': research_labor_outcomes(snapshot.memories)}
                             if transfer is not None and transfer.outcome_evidence else {})},
        'receipts.json': {'cursor': cursor, 'records': [r.model_dump(mode='json') for r in snapshot.receipts]},
        'decisions.json': {'cursor': cursor, 'records': [d.model_dump(mode='json') for d in snapshot.decisions]},
        'messages.json': {'cursor': cursor, 'records': [m.model_dump(mode='json') for m in snapshot.messages]},
        'relations.json': {'cursor': cursor,
                           **({'labor_graph': project_labor_graph(snapshot)} if 'closed_economy' in snapshot.data else {}),
                           'active': {k:v.model_dump(mode='json') for k,v in snapshot.relations.items()},
                           'pending': [v.model_dump(mode='json') for v in snapshot.pending_relations],
                           **({'communication_graph': {'active': network['communication'],
                                 'pending': network['pending'], 'applied': network['applied']}}
                              if network is not None else {})},
    }
    # Never expose a half-written final projection. A crash before publication
    # may leave an unreferenced staging directory; a later export can rebuild.
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.projection-', dir=destination.parent))
    try:
        for name, body in tables.items():
            atomic_json(staging/name, body)
        manifest = {'cursor': cursor, 'run_id': store.identity.run_id,
                    'config_hash': store.identity.config_hash,
                    'source_hash': store.identity.code_hash,
                    'files': {name:_sha(staging/name) for name in tables}}
        atomic_json(staging/'manifest.json', manifest)
        try:
            staging.rename(destination)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            # Concurrent readers may build the same immutable projection.
            return export_projection(root, destination=destination, tick=marker.tick)
        sync_directory(destination.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'path': str(destination), **manifest}


async def advance(world, store, *, until=None, stop_at_boundary=None):
    """Advance a head-aligned world; preserve primary and finalization errors."""
    config = parse_config(store.identity.config)
    terminal_tick = store.identity.metadata.get('terminal_tick', config.ticks)
    until = terminal_tick if until is None else until
    if type(until) is not int or not world.snapshot.tick <= until <= terminal_tick:
        raise ValueError('Target tick must be within the frozen horizon and after current head')
    verify_implementation(store)
    validate_parent(store)
    if config.cognition_mode == 'live' and world.snapshot.tick < until:
        import importlib.metadata
        import os
        provider = world.model_runtime.config
        if importlib.metadata.version('openai') != store.identity.metadata['sdk_version']:
            raise ValueError('Live SDK version differs from the frozen run')
        key = 'OPENAI_API_KEY' if provider.provider == 'openai' else 'DEEPSEEK_API_KEY'
        if not os.getenv(key):
            raise ValueError('Live credentials are not configured')
    if world.commit_store is not store:
        raise ValueError('Advancement requires a world attached to this commit store')
    before = world.snapshot.tick
    started = time.perf_counter()
    primary = None
    finalization_error = None
    stopped = None
    registry.record(store.root, {'status':'running','from_tick':before,'until':until,'application':APPLICATION})
    try:
        while world.snapshot.tick < until:
            if stop_at_boundary is not None and (stopped := stop_at_boundary()) is not None:
                break
            await world.step()
    except BaseException as exc:
        primary = exc
    status = (stopped or ('completed' if until == terminal_tick else 'checkpointed')) if primary is None else (
        'cancelled' if isinstance(primary, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed')
    result = {'application': APPLICATION, 'run_id': store.identity.run_id,
              'status': status, 'from_tick': before, 'tick': world.snapshot.tick, 'until': until,
              'state_hash': world.snapshot.state_hash, 'elapsed_seconds': time.perf_counter()-started,
              'external_api_calls':0, 'error': None if primary is None else type(primary).__name__+': '+str(primary)}
    try:
        if config.cognition_mode == 'live':
            runtime = world.model_runtime
            rows = runtime.store.records(run_id=runtime.run_id)
            result['external_api_calls'] = sum(row['response'] is not None for row in rows)
            result['model_usage'] = {'attempts': [{
                'attempt_id': row['attempt_id'], 'state': row['state'], 'usage_input_tokens': row['incoming'],
                'usage_output_tokens': row['outgoing'], 'accounted_upper_bound_nano': row['accounted'],
                'provider_model': row['response']['model'] if row['response'] else None,
                'provider_response_id': row['response']['raw'].get('id') if row['response'] else None,
                'error': row['error']} for row in rows],
                'project_budget': runtime.budget.snapshot(),
                'run_budget': store.identity.metadata['budget_limits']}
        marker, sealed_snapshot = store.load()
        result['checkpoint'] = _save_verified(store, marker, sealed_snapshot)
        # Keep the full authority audit and reuse its verified cursor when the
        # settled live world matches, including after a post-marker index failure.
        verified = (world, store, marker) if world.snapshot.state_hash == sealed_snapshot.state_hash else None
        result['projection'] = export_projection(store.root, _verified=verified)
    except BaseException as exc:
        finalization_error = exc
        result['finalization_error'] = type(exc).__name__+': '+str(exc)
        if primary is None:
            result['status'] = 'projection_failed'
    try:
        registry.record(store.root, result)
    except BaseException:
        if primary is None and finalization_error is None:
            raise
    if primary is not None:
        raise primary
    if finalization_error is not None:
        raise finalization_error
    return result


async def _run_and_close(world, store, until):
    try:
        return await advance(world, store, until=until)
    finally:
        runtime = getattr(world, 'model_runtime', None)
        if runtime is not None and runtime.config.source == 'live':
            await runtime.provider.close()


async def run_new(config, destination, *, until=None, run_id=None, **live_options):
    world, store = create_run(config, destination, run_id=run_id, **live_options)
    with registry.run_lock(store.root):
        return await _run_and_close(world, store, until)


async def resume_run(root, *, until=None):
    # Restore and advance under the same run lock.
    with registry.run_lock(root):
        world, store, _ = open_run(root)
        try:
            store.update_index(verify_prefix=True)
        except sqlite3.DatabaseError:
            store.repair_index()
        return await _run_and_close(world, store, until)


async def replay_run(root, *, destination):
    # Freeze the committed prefix while the verifier executes independently.
    # This lock coordinates runners only; it does not feed recorded outcomes
    # into the new world's execution.
    with registry.run_lock(root):
        store = CommitStore(root)
        verify_implementation(store)
        config = _execution_config(store)
        head = store.verified_markers()[-1]
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        runtime = _restore_model_runtime(config, store, replay_path=destination/'verification-control.sqlite')
        report = await verification_replay(store, lambda: build_closed_economy(config, runtime=runtime))
        if runtime is not None:
            atomic_json(destination/'invocations.json', runtime.store.records())
        report.update(application=APPLICATION, run_id=store.identity.run_id,
                      source_commit_hash=head.commit_hash,
                      code_hash=store.identity.code_hash, external_api_calls=0)
        atomic_json(destination/'report.json', report)
        return report
