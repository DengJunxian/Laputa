"""Model control and recorded-decision providers for v4 runs."""
from civitas.cognition.budget import BudgetController, BudgetLimits
from civitas.cognition.invocations import ModelInvocationStore
from civitas.cognition.providers import ArchivedProvider, LiveProvider, ProviderConfig, SyntheticProvider
from civitas.cognition.runtime import CognitiveRuntime
from civitas.contracts.base import content_hash


def offline_limits():
    return BudgetLimits(usd_nano=0,cny_nano=0,max_attempts=10000,max_tokens=2_000_000_000)


def runtime_for(config, metadata, run_id, lineage, branch, *, replay_path=None, tape=None, reopening=False,
                cd_structured=False, control_path=None):
    if config.cognition_mode == 'rule':
        return None
    provider_config = ProviderConfig.model_validate(metadata['provider_config'])
    limits = BudgetLimits.model_validate(metadata['budget_limits'])
    live = config.cognition_mode == 'live'
    if replay_path is not None:
        provider_config = provider_config.model_copy(update={'source':'archived_response'})
        # Failed preceding attempts determine the winning attempt identity in the world.
        provider = ArchivedProvider(tape, replay_attempts=cd_structured)
        ledger = replay_path
    elif live and cd_structured and provider_config.source == 'live':
        provider = LiveProvider(provider_config, defer_client=True)
        ledger = metadata['control_ledger']
    elif cd_structured and config.cognition_mode == 'archived_response' and provider_config.source == 'archived_response':
        if tape is None or provider_config.price.input_nano or provider_config.price.output_nano or limits.usd_nano or limits.cny_nano:
            raise ValueError('Archived structured runs require fixed responses and zero monetary limits')
        provider = ArchivedProvider(tape, replay_attempts=True)
        ledger = control_path if control_path is not None else metadata['control_ledger']
    else:
        if config.cognition_mode != 'synthetic_test' or provider_config.source != 'synthetic_test':
            raise ValueError('This execution requires its registered provider source')
        if provider_config.price.input_nano or provider_config.price.output_nano or limits.usd_nano or limits.cny_nano:
            raise ValueError('Offline budgets must have zero monetary limits and prices')
        provider = SyntheticProvider()
        ledger = metadata['control_ledger']
    existing = replay_path is None and (reopening or live)
    store = ModelInvocationStore(ledger, existing=existing)
    project_limits = BudgetController.project_limits(store) if existing else limits
    if existing and not live and project_limits != limits:
        raise ValueError('Existing invocation ledger project budget differs')
    if existing and live and content_hash(project_limits) != metadata['project_budget_hash']:
        raise ValueError('Cumulative project budget changed after this run was specified')
    if reopening and replay_path is None:
        with store.transaction() as db:
            saved = db.execute('SELECT body FROM contracts WHERE name=?',('run:'+run_id,)).fetchone()
        if saved is None or BudgetLimits.model_validate_json(saved[0]) != limits:
            raise ValueError('Missing or changed invocation ledger run budget')
    budget = BudgetController(store, project_limits)
    budget.register_run(run_id, limits, exclusive=live and not reopening and replay_path is None)
    return CognitiveRuntime(store=store,budget=budget,provider=provider,config=provider_config,run_id=run_id,
        request_namespace={'lineage':lineage,'branch':branch},
        allow_cache_reuse=metadata.get('allow_cache_reuse',False), archive_history=cd_structured)


def verify_model_ledger(lineage, ledger):
    persisted = {r['attempt_id']:r for r in ledger.records()}
    for source in lineage:
        for row in source.archived_records():
            if row['attempt_id'] not in persisted or row != persisted[row['attempt_id']]:
                raise ValueError('Model fact ledger differs from immutable commit receipt')
