"""Registered strategy choices over the normal typed economic policy."""
from typing import Literal

from civitas.cognition.evidence import retrieve, with_evidence
from civitas.cognition.runtime import ModelRefusal
from civitas.contracts.base import Id, Record
from .closed_economy_config import JOINT_CHOICE_TASK


class JointChoice(Record):
    action: Literal['baseline', 'cautious']
    evidence_id: Id


class JointPolicyAdapter:
    policy_id, version = 'closed_economy_joint_policy', '1'

    def __init__(self, rule, runtime, memory, seats):
        self.rule, self.runtime, self.memory = rule, runtime, memory
        self.seats = {(seat.actor_id, tick): seat for seat in seats for tick in seat.ticks}

    async def decide(self, observation):
        actor, tick = observation.actor_id, observation.visible_until.tick
        seat = self.seats.get((actor, tick))
        identity = {'policy_id': self.policy_id, 'policy_version': self.version}
        if seat is None:
            return (await self.rule.decide(observation)).model_copy(update=identity)

        facts = [f for f in observation.market_view if f.value.get('kind') in {
            'goods_quote', 'household_preferences', 'firm_plan', 'security_wealth'}]
        receipts = [f for f in observation.private_memory_refs if f.value.get('actor_id') == actor
                    and f.value.get('tick') == tick - 1 and f.visible_at == observation.visible_until
                    and f.value.get('outcome') in {'purchased', 'delivery_failed_refunded', 'quote_updated', 'hold'}]
        allowed = with_evidence(observation, market_view=[f.model_dump() for f in facts],
                                private_memory_refs=[f.model_dump() for f in receipts], delivered_messages=[])
        wealth = next((f for f in facts if f.value.get('kind') == 'security_wealth'), None)
        previous_spend = -sum(f.value['cash_delta_minor'] for f in receipts if f.value['outcome'] == 'purchased')
        working = {'reference_value_minor': (wealth.value['market_value_minor']
                    - wealth.value['market_value_change_minor']) if wealth is not None else None,
                   'prior_purchase_minor': previous_spend, 'feedback_ids': [f.fact_id for f in receipts]}
        memory = self.memory.accept(allowed, working=working)
        evidence = set(working['feedback_ids'])
        if wealth is not None:
            evidence.add(wealth.fact_id)
        trace = {'strategy': seat.strategy, **working}
        reserve, cautious = 0, False
        if seat.strategy == 'behavioral':
            reserve = seat.prudence_minor + previous_spend // 2
            trace.update(prudence_minor=seat.prudence_minor, additional_reserve_minor=reserve)
        else:
            role_fact = next(f for f in facts if f.value['kind'] in {'household_preferences', 'firm_plan'})
            choice, receipt = await self._structured_choice(allowed, memory, role_fact)
            if choice is not None:
                cautious = choice.action == 'cautious'
                evidence.add(choice.evidence_id)
            trace.update(action=choice.action if choice is not None else 'baseline',
                         status='accepted_choice' if choice is not None else 'rule_fallback',
                         attempt_id=receipt.attempt_id)
        result = await self.rule.decide(observation, extra_reserve_minor=reserve, cautious=cautious)
        return result.model_copy(update={**identity,
            'belief_patch': {**result.belief_patch, 'joint_policy': trace},
            'evidence_ids': sorted(set(result.evidence_ids) | evidence)})

    async def _structured_choice(self, observation, memory, role_fact):
        def validate(choice):
            if choice.evidence_id not in observation.evidence_ids:
                raise ValueError('Joint choice must cite authorized current evidence')

        prompt = {'decision_fact_id': role_fact.fact_id,
                  'instruction': ('Choose baseline or cautious. Cautious defers new goods purchases for a household '
                                  'or discounts the current quote by one permitted step for a firm. Cite one visible fact.')}
        try:
            result = await self.runtime.request(observation, retrieve(observation, memory),
                policy_id=self.policy_id, policy_version=self.version, task=JOINT_CHOICE_TASK, sample_id='1',
                response_model=JointChoice, validator=validate, task_context=prompt,
                institution_versions={'economy': 'joint-closed-economy-v4'}, prompt_version='joint-policy-choice-v1')
        except ModelRefusal as exc:
            return None, exc.receipt
        return result.value, result.receipt
