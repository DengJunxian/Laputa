"""The same Policy.decide port for rules, joint beliefs and direct typed intents."""
from pydantic import Field

from civitas.contracts.base import Id, NonNegative, Positive, Record
from civitas.contracts.decisions import DecisionBundle, HoldIntent
from .beliefs import JointBelief
from .cache import PARSER_VERSION, PROMPT_VERSION
from .observations import validate_bundle
from .reflection import ReflectionCandidate
from .runtime import RunControlError


def empty_bundle(observation, policy_id, version, bundle_model=DecisionBundle):
    extra = {'tick': observation.visible_until.tick} if 'tick' in bundle_model.model_fields else {}
    return bundle_model(decision_id=f'decision:{observation.visible_until.tick}:{observation.actor_id}',
        actor_id=observation.actor_id, observation_id=observation.observation_id, batch_id=observation.batch_id,
        observation_version=observation.observation_version, world_version=observation.world_version,
        policy_id=policy_id, policy_version=version, **extra)


class RulePolicy:
    policy_id, version = 'cognition_rule', '1'

    def __init__(self, delegate=None, *, bundle_model=DecisionBundle):
        self.delegate, self.bundle_model = delegate, bundle_model

    async def decide(self, observation):
        if self.delegate:
            value = await self.delegate.decide(observation)
            return value.model_copy(update={'policy_id': self.policy_id, 'policy_version': self.version})
        value = empty_bundle(observation, self.policy_id, self.version, self.bundle_model)
        return value.model_copy(update={'economic_intents': [HoldIntent(intent_id='intent:' + value.decision_id)]})

    async def decide_from_belief(self, observation, belief: JointBelief):
        """WP06 action seam; WP05's placeholder re-evaluates rules on the current account."""
        if not belief.as_of_tick <= observation.visible_until.tick <= belief.valid_until_tick:
            raise ValueError('Expired or future strategy parameters')
        if belief.abstain:
            return await RulePolicy(bundle_model=self.bundle_model).decide(observation)
        return await self.decide(observation)


class PolicyConfig(Record):
    sample_id: Id = 'behavior-sample:0'
    period: Positive = 1
    max_horizon: Positive = 20
    reflection_period: NonNegative = 0
    demo_fallback: bool = False
    prompt_version: Id = PROMPT_VERSION
    parser_version: Id = PARSER_VERSION
    mechanism_versions: dict[Id, Id] = Field(default_factory=dict)
    institution_versions: dict[Id, Id] = Field(default_factory=dict)


class _ModelPolicy:
    version = '1'

    def __init__(self, runtime, memory, *, config=None, bundle_model=DecisionBundle, fast_rule=None):
        self.runtime, self.memory = runtime, memory
        self.config = config or PolicyConfig()
        self.bundle_model = bundle_model
        self.fast_rule = fast_rule or RulePolicy(bundle_model=bundle_model)
        numeric_versions = getattr(self.fast_rule, 'mechanism_versions', {})
        if any(k in self.config.mechanism_versions and self.config.mechanism_versions[k] != v for k,v in numeric_versions.items()):
            raise ValueError('Conflicting numeric mechanism version')
        if numeric_versions:
            self.config = self.config.model_copy(update={'mechanism_versions':{**self.config.mechanism_versions,**numeric_versions}})
        self.mechanism_versions = self.config.mechanism_versions
        if hasattr(self.fast_rule, 'configuration'):
            self.configuration = {'cognition':self.config.model_dump(), 'numeric':self.fast_rule.configuration.model_dump()}

    async def _call(self, observation, task, response_model, validator, context):
        return await self.runtime.request(observation, self.memory.for_actor(observation.actor_id),
            policy_id=self.policy_id, policy_version=self.version, task=task, sample_id=self.config.sample_id,
            response_model=response_model, validator=validator, task_context=context,
            mechanism_versions=self.config.mechanism_versions, institution_versions=self.config.institution_versions,
            prompt_version=self.config.prompt_version, parser_version=self.config.parser_version)

    async def reflect(self, observation):
        result = await self._call(observation, 'reflection', ReflectionCandidate,
            lambda value: value.validate_for(observation),
            {'instruction': 'Propose concise semantic/procedural notes citing witnessed evidence IDs. '
             'Do not fabricate events, rewrite receipts, or provide internal reasoning.',
             'actor_id': observation.actor_id, 'observation_id': observation.observation_id})
        return result.value

    async def decide(self, observation):
        old = self.memory.for_actor(observation.actor_id)
        triggers = self.triggers(observation, old)
        working = old.model_dump()['working']
        reflection = None
        try:
            if triggers:
                value = await self.slow(observation, triggers)
                working.update(last_slow_tick=observation.visible_until.tick,
                               plan=value.model_dump()['belief_patch'].get('joint_belief'))
            else:
                value = await self.fast_rule.decide_from_belief(observation, JointBelief.model_validate(working['plan']))
                value = value.model_copy(update={'policy_id': self.policy_id, 'policy_version': self.version})
            if self.config.reflection_period and observation.visible_until.tick % self.config.reflection_period == 0:
                reflection = await self.reflect(observation)
            degraded = False
        except RunControlError as exc:
            if not self.config.demo_fallback:
                raise
            value = await getattr(self.fast_rule, 'fallback_decide', self.fast_rule.decide)(observation)
            value = value.model_copy(update={'policy_id': self.policy_id, 'policy_version': self.version})
            working['last_failure'] = str(exc)
            degraded = True
        patch = value.model_dump()['belief_patch']
        patch['cognition_runtime'] = {'degraded': degraded, 'slow_triggers': triggers,
                                     'numeric_mechanism': getattr(self.fast_rule, 'mechanism_id', 'wp06_pending_rule_placeholder') if self.policy_id == 'hybrid_belief' else 'typed_intent'}
        value = value.model_copy(update={'belief_patch': patch})
        validate_bundle(value, observation, self.policy_id, self.version)
        self.memory.accept(observation, reflection=reflection, working=working)
        return value


class DirectIntentPolicy(_ModelPolicy):
    policy_id = 'direct_intent'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.config.period != 1:
            raise ValueError('Direct intents require a fresh call each tick; old order quantities are never plans')

    def triggers(self, observation, memory):
        return ['direct_intent_tick']

    async def slow(self, observation, triggers):
        def validate(value):
            validate_bundle(value, observation, self.policy_id, self.version)
            if set(value.belief_patch) - {'cognition_choice'}:
                raise ValueError('Direct intent cannot supply a psychological overlay')
            choice = value.belief_patch.get('cognition_choice', 'act')
            if choice not in {'act', 'abstain'} or (choice == 'abstain' and
                    any(not isinstance(i, HoldIntent) for i in value.economic_intents)):
                raise ValueError('Invalid cognitive abstention')
        result = await self._call(observation, 'direct', self.bundle_model, validate,
            {'instruction': 'Return a DecisionBundle with typed economic_intents. Use only the current account and rules. '
             'For cognitive abstention use hold intents and belief_patch={"cognition_choice":"abstain"}. '
             'Otherwise belief_patch may be empty. Do not apply a second psychological bias.',
             'identity': empty_bundle(observation, self.policy_id, self.version, self.bundle_model).model_dump(),
             'triggers': triggers})
        return result.value


class HybridBeliefPolicy(_ModelPolicy):
    policy_id = 'hybrid_belief'

    def __init__(self, *args, scenarios, **kwargs):
        super().__init__(*args, **kwargs)
        from pydantic import TypeAdapter
        self.scenarios = TypeAdapter(dict[str, dict[str, float]]).validate_python(scenarios, strict=True)
        if len(self.scenarios) < 2 or len({tuple(sorted(s)) for s in self.scenarios.values()}) != 1:
            raise ValueError('At least two joint scenarios with matching outcome dimensions are required')
        # Validate finite scenario inputs with the shared strict boundary.
        from .memory import AgentMemory
        AgentMemory(actor_id='scenario-validation', working=self.scenarios)
        validate_scenarios = getattr(self.fast_rule, 'validate_scenarios', None)
        if validate_scenarios is not None:
            validate_scenarios(self.scenarios)
        if getattr(self.fast_rule, 'belief_contract', {}).get('horizon_ticks', 0) > self.config.max_horizon:
            raise ValueError('Numeric horizon exceeds the model horizon limit')

    def triggers(self, observation, memory):
        tick = observation.visible_until.tick
        last = memory.working.get('last_slow_tick')
        plan = memory.working.get('plan')
        reasons = []
        if last is None:
            reasons.append('initial')
        if last is not None and tick - last >= self.config.period:
            reasons.append('periodic')
        seen = {e.evidence_id for e in memory.episodic}
        if (any(m.message_id not in seen for m in observation.delivered_messages)
                or any(f.fact_id not in seen and set(f.value) & {'event_id', 'event_kind', 'text'}
                       for f in observation.market_view)):
            reasons.append('event')
        if (not plan or tick > plan['valid_until_tick'] or ('horizon_tick' in plan and tick >= plan['horizon_tick'])
                or any(f.value.get('plan_invalidated') is True for f in observation.market_view)):
            reasons.append('plan_invalidated')
        return sorted(set(reasons))

    async def slow(self, observation, triggers):
        def validate(value):
            validate_bundle(value, observation, self.policy_id, self.version)
            if value.economic_intents or set(value.belief_patch) != {'joint_belief'}:
                raise ValueError('Hybrid output is a belief; WP06 numeric actions are not model orders')
            belief = JointBelief.model_validate(value.belief_patch['joint_belief'])
            belief.validate_for(observation, self.scenarios, self.config.max_horizon)
            validate_belief = getattr(self.fast_rule, 'validate_belief', None)
            if validate_belief is not None:
                validate_belief(observation, belief)
            if not set(belief.evidence_ids) <= set(value.evidence_ids):
                raise ValueError('Belief evidence must also be declared by the DecisionBundle')
        result = await self._call(observation, 'hybrid', self.bundle_model, validate,
            {'instruction': 'Return a DecisionBundle with no economic_intents, and belief_patch={"joint_belief":...}. '
             'Give subjective probabilities over exactly the supplied joint scenarios, evidence_ids, as_of_tick, '
             'horizon_tick, valid_until_tick and abstain. Abstention uses empty probabilities. '
             'These scenarios are hypothetical inputs, not future observations.',
             'scenarios': self.scenarios, 'belief_schema': JointBelief.model_json_schema(),
             'max_horizon': self.config.max_horizon, 'triggers': triggers,
             **({'outcome_contract': self.fast_rule.belief_contract} if hasattr(self.fast_rule, 'belief_contract') else {}),
             'identity': empty_bundle(observation, self.policy_id, self.version, self.bundle_model).model_dump()})
        numeric = await self.fast_rule.decide_from_belief(observation, JointBelief.model_validate(result.value.belief_patch['joint_belief']))
        patch = result.value.model_dump()['belief_patch']
        if any(k in patch and patch[k] != v for k,v in numeric.belief_patch.items()):
            raise ValueError('Numeric proposal conflicts with model belief ownership')
        return result.value.model_copy(update={'economic_intents': numeric.economic_intents,
            'belief_patch':{**patch,**numeric.belief_patch},
            'evidence_ids':sorted(set(result.value.evidence_ids) | set(numeric.evidence_ids))})
