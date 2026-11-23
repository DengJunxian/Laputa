"""Composition root for the offline, single-book C/D scenario."""
from civitas.core.registry import MechanismRegistry
from civitas.core.rng import AddressedRNG
from civitas.core.world import WorldKernel
from civitas.scenarios.closed_economy import ClosedEconomy
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig
from civitas.scenarios.closed_economy_policy import ClosedEconomyPolicy, EconomicIntentCompiler
from civitas.scenarios.closed_economy_relations import ClosedEconomyObserver
from civitas.scenarios.closed_economy_cognition import (
    LaborModelPolicy, OfflineCooperationPolicy, SocialChoicePolicy, SupplierModelPolicy, WithdrawalModelPolicy,
)
from civitas.cognition.memory import MemoryBook
from .cognition import bind_invocation_receipts


def build_closed_economy(config, *, policy=None, runtime=None, phase_hook=None):
    config = ClosedEconomyConfig.model_validate(config)
    rng = AddressedRNG(config.root_seed, config.replica)
    if config.securities is not None and config.security_feedback is None:
        from civitas.core.observation import ObservationBuilder
        from civitas.scenarios.security_market import SecurityMarket, SecurityPolicy

        if runtime is not None:
            raise ValueError("Security sessions require an offline rule policy")
        scenario = SecurityMarket(config, rng)
        return WorldKernel(scenario=scenario, policy=policy or SecurityPolicy(config.securities, rng),
            compiler=EconomicIntentCompiler(), observer=ObservationBuilder(),
            registry=MechanismRegistry([], None, scenario.owned_fields | {"economic_runtime"}),
            rng=rng, actors=scenario.actors, events=scenario.events, concurrency=config.concurrency,
            phase_hook=phase_hook, participants=scenario.participants, schema_version=4)
    scenario = ClosedEconomy(config)
    if (runtime is not None) != (config.cognition_mode != 'rule') or (runtime is not None and policy is not None):
        raise ValueError('Cognition requires its configured policy and model runtime')
    policy = policy or ClosedEconomyPolicy(config, rng)
    participants = list(scenario.participants)
    if config.supplier_cognition is not None:
        actors = [scenario.households[i] for i in config.supplier_cognition.household_indices]
        memory = MemoryBook(actors)
        participants.append(memory)
        policy = SupplierModelPolicy(policy, runtime, memory)
    if config.labor_cognition is not None:
        policy = LaborModelPolicy(policy, runtime)
    if config.withdrawal_cognition is not None:
        policy = WithdrawalModelPolicy(policy, runtime)
    if config.cooperation_cognition is not None or config.disclosure_cognition is not None or config.credit_cognition is not None:
        policy = SocialChoicePolicy(policy, runtime)
    if config.offline_cooperation_action is not None:
        if runtime.config.source not in ('synthetic_test', 'archived_response'):
            raise ValueError('C/D cooperation accepts offline responses only')
        policy = OfflineCooperationPolicy(policy, runtime, scenario.households[config.cooperation.household_indices[0]])
    registry = MechanismRegistry([], None,
        scenario.owned_fields | frozenset(p.participant_id for p in participants))
    world = WorldKernel(scenario=scenario,
        policy=policy,
        compiler=EconomicIntentCompiler(), observer=ClosedEconomyObserver(config,
            available_link_cash=(lambda actor: scenario.economy.account_view(actor).available_cash_minor)
            if config.link_fee_minor or config.partner_choice else None),
        registry=registry, rng=rng, actors=scenario.actors, events=scenario.events,
        concurrency=config.concurrency, phase_hook=phase_hook,
        participants=participants, schema_version=4)
    if runtime is not None:
        bind_invocation_receipts(world, runtime)
    return world
