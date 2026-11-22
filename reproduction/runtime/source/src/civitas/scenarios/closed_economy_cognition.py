"""Model judgments over the regular C/D policy and execution path."""
from typing import Literal

from pydantic import Field

from civitas.cognition.evidence import retrieve, with_evidence
from civitas.cognition.memory import AgentMemory
from civitas.cognition.observations import validate_bundle
from civitas.cognition.policy import empty_bundle
from civitas.cognition.reflection import ReflectionCandidate
from civitas.cognition.runtime import ModelRefusal
from civitas.contracts.base import Id, Record, require_unique
from civitas.contracts.economic import Amount, CooperationIntent, CreditApplicationIntent, EconomicDecisionBundle, LaborApplyIntent, WithdrawDepositIntent
from .closed_economy_config import Bps
from .closed_economy_relations import DELIVERY_OUTCOMES, EXPERIENCE_KIND
from .closed_economy_cooperation import trial_partner_result

class OfflineCooperationPolicy:
    policy_id, version = 'closed_economy_offline_cooperation', '1'

    def __init__(self, rule, runtime, actor):
        self.rule, self.runtime, self.actor = rule, runtime, actor

    async def decide(self, observation):
        baseline = await self.rule.decide(observation)
        baseline = baseline.model_copy(update={'policy_id': self.policy_id, 'policy_version': self.version})
        if observation.actor_id != self.actor or observation.visible_until.tick != self.rule.config.cooperation.offer_tick:
            return baseline
        original = next((i for i in baseline.economic_intents if isinstance(i, CooperationIntent)), None)
        if original is None:
            return baseline

        def validate(bundle):
            validate_bundle(bundle, observation, self.policy_id, self.version)
            intents = bundle.economic_intents
            if (bundle.decision_id != baseline.decision_id or len(intents) != 1
                    or not isinstance(intents[0], CooperationIntent) or intents[0].intent_id != original.intent_id
                    or intents[0].opportunity_id != original.opportunity_id or intents[0].action == 'decline'
                    or original.opportunity_id not in bundle.evidence_ids or bundle.message_drafts
                    or bundle.network_proposals or bundle.belief_patch):
                raise ValueError('Offline cooperation response must choose one action on the current cited offer')

        result = await self.runtime.request(observation, AgentMemory(actor_id=self.actor),
            policy_id=self.policy_id, policy_version=self.version, task='closed_economy_cooperation',
            sample_id='cooperation:0', response_model=EconomicDecisionBundle, validator=validate,
            task_context={'identity': empty_bundle(observation, self.policy_id, self.version,
                                                   EconomicDecisionBundle).model_dump(mode='json'),
                          'fixture_action': self.rule.config.offline_cooperation_action,
                          'intent_id': original.intent_id})
        return baseline.model_copy(update={
            'economic_intents': [result.value.economic_intents[0] if i == original else i for i in baseline.economic_intents],
            'evidence_ids': sorted(set(baseline.evidence_ids) | set(result.value.evidence_ids)),
            'belief_patch': {**baseline.belief_patch, 'offline_cooperation': {'attempt_id': result.receipt.attempt_id}},
        })


class SupplierBelief(Record):
    actor_id: Id
    observation_id: Id
    scores_bps: dict[Id, Bps]
    evidence_ids: list[Id] = Field(min_length=1)

    def validate_for(self, observation, sellers):
        require_unique(self.evidence_ids, 'supplier evidence')
        if (self.actor_id, self.observation_id) != (observation.actor_id, observation.observation_id):
            raise ValueError('Supplier belief identity mismatch')
        if set(self.scores_bps) != sellers or not set(self.evidence_ids) <= observation.evidence_ids:
            raise ValueError('Supplier belief must cover the visible sellers and cite authorized evidence')


class SupplierModelPolicy:
    policy_id, version = 'closed_economy_supplier_cognition', '1'

    def __init__(self, rule, runtime, memory):
        self.rule, self.runtime, self.memory = rule, runtime, memory
        self.config = rule.config
        self.settings = rule.config.supplier_cognition
        self.actors = {f'household:{i:04d}' for i in self.settings.household_indices}

    async def decide(self, observation):
        tick, actor = observation.visible_until.tick, observation.actor_id
        active = actor in self.actors and tick in self.settings.ticks
        identity = {'policy_id': self.policy_id, 'policy_version': self.version}
        quotes = [f for f in observation.market_view if f.value.get('kind') == 'goods_quote'] if active else []
        if not quotes:
            return (await self.rule.decide(observation)).model_copy(update=identity)
        try:
            applied, trace = await self._supplier(observation, quotes)
        except ModelRefusal as exc:
            if self.settings.source != 'archived_response':
                raise
            bundle = await self.rule.decide(observation)
            return bundle.model_copy(update={**identity, 'belief_patch': {**bundle.belief_patch, 'model_supplier': {
                'mode': self.settings.mode, 'attempt_id': exc.receipt.attempt_id,
                'status': 'rule_fallback', 'reason': 'refusal', 'task': exc.receipt.metadata['task']}}})
        bundle = await self.rule.decide(observation, supplier_scores=applied, integrate_peer=self.settings.mode == 'M')
        return bundle.model_copy(update={
            **identity,
            'belief_patch': {**bundle.belief_patch, 'model_supplier': trace},
            'evidence_ids': sorted(set(bundle.evidence_ids) | set(trace.get('evidence_ids', []))),
        })

    async def _supplier(self, observation, quotes):
        # Derived beliefs and graph/trust context could carry peer evidence back into M.
        receipts = sorted((f for f in observation.private_memory_refs if f.fact_id.startswith('memory:')
                           and f.value.get('actor_id') == observation.actor_id
                           and f.value.get('outcome') in DELIVERY_OUTCOMES),
                          key=lambda f: (-f.visible_at.tick, f.fact_id))[:12]
        allowed = with_evidence(observation,
            market_view=[f.model_dump() for f in quotes],
            private_memory_refs=[f.model_dump() for f in receipts],
            delivered_messages=observation.payload()['delivered_messages'] if self.settings.mode == 'L' else [])
        memory = self.memory.accept(allowed)
        fresh = [f.model_dump() for f in receipts if f.visible_at == observation.visible_until]
        reflection_id = None
        if self.settings.reflect and fresh and observation.visible_until.tick != self.settings.ticks[0]:
            reflection = with_evidence(allowed, account_view=[], market_view=[],
                                       private_memory_refs=fresh, delivered_messages=[])
            result = await self._request(reflection, memory, 'cd_reflection',
                ReflectionCandidate, lambda value: value.validate_for(reflection),
                {'instruction': 'Record at most one tentative note from your visible receipts. Cite the receipts used.'})
            self.memory.accept(reflection, reflection=result.value, pin_reflection_versions=True)
            memory = self.memory.for_actor(observation.actor_id)
            reflection_id = result.receipt.attempt_id
        sellers = {f.value['seller_id'] for f in quotes}
        result = await self._request(allowed, memory, 'cd_supplier', SupplierBelief,
            lambda value: value.validate_for(allowed, sellers),
            {'instruction': 'Estimate reliability in integer basis points (0–10000) for every quoted seller. '
                            'Use visible purchase evidence. Count each root only once and cite evidence used.',
             'information_mode': self.settings.mode, 'numeric_social_weight_bps': self.settings.social_weight_bps})
        private = result.value.scores_bps
        mu = self.settings.social_weight_bps

        def apply(social):
            effective = {a: ((10000 - mu) * private[a] + mu * social.get(a, 5000)) // 10000 for a in private}
            trace['applied_scores_bps'] = effective
            return effective

        trace = {'mode': self.settings.mode, 'attempt_id': result.receipt.attempt_id,
                 'reflection_attempt_id': reflection_id, 'model_scores_bps': dict(private),
                 'evidence_ids': list(result.value.evidence_ids)}
        return apply, trace

    async def _request(self, observation, memory, task, model, validator, context):
        projected = (retrieve(observation, memory) if self.settings.retrieve_memory
                     else AgentMemory(actor_id=observation.actor_id))
        return await self.runtime.request(observation, projected, policy_id=self.policy_id, policy_version=self.version,
            task=task, sample_id='1', response_model=model, validator=validator, task_context=context,
            mechanism_versions={'SM03': 'cd-supplier-v1', 'SM08': 'authorized-receipts-v1'},
            institution_versions={'economy': 'closed-economy-v4'}, prompt_version='cd-supplier-json-v1')


class WithdrawalChoice(Record):
    notice_fact_id: Id
    amount_minor: Amount


class WithdrawalModelPolicy:
    def __init__(self, rule, runtime):
        self.rule, self.runtime = rule, runtime
        self.config = rule.config
        self.policy_id, self.version = rule.policy_id, rule.version
        self.actor = f'household:{rule.config.withdrawal_cognition.household_index:04d}'
        self.tick = rule.config.banking.run_notice.tick

    async def decide(self, observation):
        baseline = await self.rule.decide(observation)
        if (observation.actor_id, observation.visible_until.tick) != (self.actor, self.tick):
            return baseline
        original = next((intent for intent in baseline.economic_intents if isinstance(intent, WithdrawDepositIntent)), None)
        if original is None:
            return baseline
        notice, = (fact for fact in observation.market_view if fact.fact_id == original.notice_fact_id)
        allowed = opportunity_observation(observation, notice)

        def validate(choice):
            if choice.notice_fact_id != notice.fact_id or choice.amount_minor > original.amount_minor:
                raise ValueError('Withdrawal choice must cite the current authorized notice and respect its limit')

        try:
            result = await self.runtime.request(allowed, AgentMemory(actor_id=self.actor), policy_id=self.policy_id,
                policy_version=self.version, task='cd_withdrawal', sample_id='1', response_model=WithdrawalChoice,
                validator=validate, task_context={'instruction': 'Choose a withdrawal amount within the visible limit, or zero to wait.'},
                mechanism_versions={'L4': 'verified-withdrawal-notice-v1'},
                institution_versions={'banking': 'closed-economy-v4'}, prompt_version='cd-withdrawal-json-v1')
        except ModelRefusal as exc:
            trace = {'attempt_id': exc.receipt.attempt_id, 'status': 'rule_fallback'}
            return baseline.model_copy(update={'belief_patch': {**baseline.belief_patch, 'model_withdrawal': trace}})
        amount = result.value.amount_minor
        intents = [i for i in baseline.economic_intents if i != original or amount > 0]
        trace = {'attempt_id': result.receipt.attempt_id, 'status': 'model_choice'}
        return baseline.model_copy(update={
            'economic_intents': [i.model_copy(update={'amount_minor': amount}) if i == original else i for i in intents],
            'belief_patch': {**baseline.belief_patch, 'model_withdrawal': trace}})


def opportunity_observation(observation, fact):
    return with_evidence(observation, account_view=[], market_view=[fact.model_dump(mode='json')],
                         private_memory_refs=[], delivered_messages=[])


class LaborChoice(Record):
    vacancy_id: Id
    action: Literal['apply', 'wait']


class LaborModelPolicy:
    def __init__(self, rule, runtime):
        self.rule, self.runtime = rule, runtime
        self.config = rule.config
        self.policy_id, self.version = rule.policy_id, rule.version
        seat, self.tick = rule.config.labor_cognition.household_index, rule.config.labor_transfer.offer_tick
        self.actor = f'household:{seat:04d}'

    async def decide(self, observation):
        baseline = await self.rule.decide(observation)
        if (observation.actor_id, observation.visible_until.tick) != (self.actor, self.tick):
            return baseline
        application = next((intent for intent in baseline.economic_intents if isinstance(intent, LaborApplyIntent)), None)
        if application is None:
            return baseline
        vacancy, = (fact for fact in observation.market_view if fact.fact_id == application.vacancy_id)
        allowed = opportunity_observation(observation, vacancy)

        def validate(choice):
            if choice.vacancy_id != vacancy.fact_id:
                raise ValueError('Labor choice must cite the current authorized vacancy')

        intents = baseline.economic_intents
        try:
            result = await self.runtime.request(allowed, AgentMemory(actor_id=self.actor), policy_id=self.policy_id,
                policy_version=self.version, task='cd_labor', sample_id='1', response_model=LaborChoice,
                validator=validate, task_context={'instruction': 'Choose whether to apply for the visible vacancy or wait.'},
                mechanism_versions={'L3': 'directed-labor-vacancy-v1'},
                institution_versions={'labor': 'closed-economy-v4'}, prompt_version='cd-labor-choice-json-v1')
        except ModelRefusal as exc:
            trace = {'attempt_id': exc.receipt.attempt_id, 'status': 'rule_fallback'}
        else:
            action = result.value.action
            trace = {'attempt_id': result.receipt.attempt_id, 'status': 'model_choice', 'action': action}
            if action == 'wait':
                intents = [intent for intent in intents if intent != application]
        return baseline.model_copy(update={'economic_intents': intents,
            'belief_patch': {**baseline.belief_patch, 'model_labor': trace}})


class CooperationChoice(Record):
    opportunity_id: Id
    action: Literal['cooperate', 'defect', 'decline']


class DisclosureChoice(Record):
    message_id: Id
    action: Literal['share', 'withhold']


class CreditChoice(Record):
    offer_fact_id: Id
    action: Literal['apply', 'wait']


class SocialChoicePolicy:
    """Replace a registered decision with a bounded choice from its own observation."""

    def __init__(self, rule, runtime):
        self.rule, self.runtime, self.config = rule, runtime, rule.config
        self.policy_id, self.version = rule.policy_id, rule.version
        self.slots = {(task, f'household:{seat:04d}', tick) for task, seat, tick in self.config.choice_slots
                      if task in ('cd_cooperation_choice', 'cd_disclosure_choice', 'cd_credit')}

    async def decide(self, observation):
        bundle = await self.rule.decide(observation)
        actor, tick = observation.actor_id, observation.visible_until.tick
        if ('cd_credit', actor, tick) in self.slots:
            bundle = await self.credit(observation, bundle)
        if ('cd_cooperation_choice', actor, tick) in self.slots:
            bundle = await self.cooperate(observation, bundle)
        if ('cd_disclosure_choice', actor, tick) in self.slots:
            bundle = await self.disclose(observation, bundle)
        return bundle

    async def choose(self, task, observation, bundle, schema, validate, prompt):
        try:
            result = await self.runtime.request(observation, AgentMemory(actor_id=observation.actor_id),
                policy_id=self.policy_id, policy_version=self.version, task=task, sample_id='1',
                response_model=schema, validator=validate, task_context=prompt,
                mechanism_versions=({'credit': 'directed-offer-v1'} if task == 'cd_credit'
                                    else {'SM08': 'goal03-authorized-choice-v1'}),
                institution_versions={'economy': 'closed-economy-v4'}, prompt_version=task+'-v1')
        except ModelRefusal as exc:
            choice = None
            trace = {'attempt_id': exc.receipt.attempt_id, 'status': 'rule_fallback'}
        else:
            choice = result.value
            trace = {'attempt_id': result.receipt.attempt_id, 'status': 'model_choice', 'action': choice.action}
        return choice, bundle.model_copy(update={'belief_patch': {**bundle.belief_patch, task: trace}})

    async def credit(self, observation, bundle):
        intent = next((row for row in bundle.economic_intents if isinstance(row, CreditApplicationIntent)), None)
        if intent is None:
            return bundle
        offer, = (fact for fact in observation.market_view if fact.fact_id == intent.offer_fact_id)

        def validate(choice):
            if choice.offer_fact_id != offer.fact_id:
                raise ValueError('Credit choice must name the offered terms')

        choice, bundle = await self.choose('cd_credit', opportunity_observation(observation, offer), bundle,
            CreditChoice, validate, {'instruction': 'Apply for these fixed terms or wait until the offer expires.'})
        if choice is not None and choice.action == 'wait':
            return bundle.model_copy(update={'economic_intents': [row for row in bundle.economic_intents if row != intent]})
        return bundle

    async def cooperate(self, observation, bundle):
        intent = next((row for row in bundle.economic_intents if isinstance(row, CooperationIntent)), None)
        if intent is None:
            return bundle
        offer, = (fact for fact in observation.market_view if fact.fact_id == intent.opportunity_id)
        settings = self.config.cooperation
        previous = None
        if settings.followup is not None and observation.visible_until.tick == settings.followup.offer_tick:
            previous = trial_partner_result(observation.private_memory_refs, settings, offer.value['partner_id'])
        allowed = with_evidence(opportunity_observation(observation, offer),
                                private_memory_refs=[previous.model_dump(mode='json')] if previous else [])

        def validate(choice):
            if choice.opportunity_id != offer.fact_id:
                raise ValueError('Cooperation choice must cite the current authorized offer')

        choice, bundle = await self.choose('cd_cooperation_choice', allowed, bundle, CooperationChoice, validate,
                                          {'instruction': 'Choose cooperate, defect or decline on the visible offer.'})
        if choice is None:
            return bundle
        return bundle.model_copy(update={'economic_intents': [
            row.model_copy(update={'action': choice.action}) if row == intent else row for row in bundle.economic_intents]})

    async def disclose(self, observation, bundle):
        draft = next((row for row in bundle.message_drafts if row.body.get('kind') == EXPERIENCE_KIND), None)
        if draft is None:
            return bundle
        receipt_id = 'memory:' + draft.body['receipt_command_id']
        own_receipt = next(fact for fact in observation.private_memory_refs if fact.fact_id == receipt_id)
        allowed = with_evidence(observation, account_view=[], market_view=[], delivered_messages=[],
                                private_memory_refs=[own_receipt.model_dump(mode='json')])

        def validate(choice):
            if choice.message_id != draft.message_id:
                raise ValueError('Disclosure choice must name the verified draft')

        choice, bundle = await self.choose('cd_disclosure_choice', allowed, bundle, DisclosureChoice, validate,
            {'instruction': 'Share or withhold the verified receipt with the listed contact.',
             'message_id': draft.message_id, 'recipient_id': draft.recipient_id})
        if choice is None or choice.action == 'share':
            return bundle
        return bundle.model_copy(update={'message_drafts': [row for row in bundle.message_drafts if row != draft]})
