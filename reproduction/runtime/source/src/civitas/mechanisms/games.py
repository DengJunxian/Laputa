"""Public-goods proposals and theoretical payoffs; WP10 owns real distribution."""
from typing import Literal
from pydantic import Field, TypeAdapter, model_validator

from civitas.contracts.base import Id, NonNegative, Record, require_unique
from civitas.contracts.observations import Observation
from civitas.contracts.portfolio import Rate, Units
from .outcomes import ExactAmount


class PublicGoodsConfig(Record):
    group_id: Id = 'public_goods'
    endowments: dict[Id, Units]
    multiplier: ExactAmount

    @model_validator(mode='after')
    def institutional_grid(self):
        if not 1 < self.multiplier.fraction < len(self.endowments):
            raise ValueError('Public-goods multiplier must satisfy 1 < r < n')
        if any(e % self.multiplier.denominator for e in self.endowments.values()):
            raise ValueError('Endowments must be multiples of the reduced multiplier denominator')
        return self


class ContributionChoice(Record):
    actor_id: Id
    amount_minor: Units
    source: str
    evidence_ids: list[Id] = Field(default_factory=list)


def public_goods_payoff(config, contributions):
    config = PublicGoodsConfig.model_validate(config)
    contributions = TypeAdapter(dict[Id,Units]).validate_python(contributions,strict=True)
    if set(contributions) != set(config.endowments):
        raise ValueError('Contributions must name exactly the group members')
    if any(c > config.endowments[a] or c % config.multiplier.denominator for a,c in contributions.items()):
        raise ValueError('Contribution exceeds endowment or lies off the institutional grid')
    distribution = config.multiplier.fraction * sum(contributions.values()) / len(contributions)
    return {a:ExactAmount.of(e-contributions[a]+distribution) for a,e in sorted(config.endowments.items())}


COOPERATION_STAKE_MINOR = 4
COOPERATION_MAX_SUBSIDY_MINOR = 4


def bilateral_cooperation_deltas(contributions):
    """Cash deltas for the fixed two-seat, 3/2 public-goods round."""
    stake = COOPERATION_STAKE_MINOR
    if len(contributions) != 2 or any(v not in (0, stake) for v in contributions.values()):
        raise ValueError("A bilateral round requires two binary contributions")
    game = PublicGoodsConfig(endowments=dict.fromkeys(contributions, stake),
                             multiplier=ExactAmount(numerator=3, denominator=2))
    return {actor: int(value.fraction - stake) for actor, value in public_goods_payoff(game, contributions).items()}


def fixed_contribution(config, actor_id, ratio):
    config, ratio = PublicGoodsConfig.model_validate(config), Rate.model_validate(ratio)
    if actor_id not in config.endowments:
        raise ValueError('Unknown group member')
    step = config.multiplier.denominator
    amount = (config.endowments[actor_id] // step) * ratio.numerator // ratio.denominator * step
    return ContributionChoice(actor_id=actor_id,amount_minor=amount,source='fixed_ratio_floor_to_grid')


class PublicGroupHistory(Record):
    group_id: Id
    round_tick: NonNegative
    mean_contribution: ExactAmount

    @model_validator(mode='after')
    def nonnegative(self):
        if self.mean_contribution.numerator < 0:
            raise ValueError('Mean contribution cannot be negative')
        return self


def conditional_contribution(observation, config, prior):
    observation = Observation.model_validate(observation)
    config = PublicGoodsConfig.model_validate(config)
    fallback = fixed_contribution(config,observation.actor_id,prior)
    previous = []
    for fact in observation.market_view:
        if 'public_group_history' not in fact.value:
            continue
        history = PublicGroupHistory.model_validate(fact.value['public_group_history'])
        if history.group_id != config.group_id:
            continue
        if not fact.public or history.round_tick >= observation.visible_until.tick:
            raise ValueError('Conditional cooperation only consumes a public completed round')
        if history.round_tick == observation.visible_until.tick - 1:
            previous.append((history,fact.fact_id))
    if not previous:
        return fallback.model_copy(update={'source':'configured_prior'})
    if len(previous) != 1:
        raise ValueError('Ambiguous previous public round')
    history, reference = previous[0]
    step = config.multiplier.denominator
    amount = min(config.endowments[observation.actor_id],(history.mean_contribution.fraction // step) * step)
    return ContributionChoice(actor_id=observation.actor_id,amount_minor=amount,source='previous_public_round',evidence_ids=[reference])


class LearningParameters(Record):
    learning_rate: float = Field(default=0.1, ge=0.0, le=1.0)


class LearningState(Record):
    actor_id: Id
    values: dict[Id, float]
    used_ledger_tx_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode='after')
    def once(self):
        require_unique(self.used_ledger_tx_ids,'learned ledger transaction')
        return self


class ObservedGameReturn(Record):
    actor_id: Id
    action_id: Id
    round_tick: NonNegative
    endowment_minor: Units
    contribution_minor: Units
    distribution_minor: Units
    ledger_tx_id: Id
    origin: Literal['ledger_receipt','synthetic_test']

    @model_validator(mode='after')
    def feasible(self):
        if self.contribution_minor > self.endowment_minor:
            raise ValueError('Observed contribution exceeds endowment')
        return self


class ChosenActionUpdate(Record):
    state: LearningState
    action_id: Id
    old_value: float
    new_value: float
    observed_return_minor: NonNegative
    event_ref: Id
    ledger_tx_id: Id
    learning_rate: float


def update_chosen_action(state, observation, receipt_fact_id, parameters):
    state, observation = LearningState.model_validate(state), Observation.model_validate(observation)
    parameters = LearningParameters.model_validate(parameters)
    facts = {f.fact_id:f for f in observation.private_memory_refs}
    if state.actor_id != observation.actor_id or receipt_fact_id not in facts:
        raise ValueError('Learning requires own witnessed execution feedback')
    receipt = ObservedGameReturn.model_validate(facts[receipt_fact_id].value['public_goods_return'])
    if (receipt.actor_id != state.actor_id or receipt.round_tick >= observation.visible_until.tick
            or receipt.action_id not in state.values or receipt.ledger_tx_id in state.used_ledger_tx_ids):
        raise ValueError('Foreign, current, unknown-action or repeated learning feedback')
    reward = receipt.endowment_minor - receipt.contribution_minor + receipt.distribution_minor
    old = state.values[receipt.action_id]
    new = (1.0 - parameters.learning_rate) * old + parameters.learning_rate * reward
    updated = state.model_copy(update={'values':{**state.values,receipt.action_id:new},
        'used_ledger_tx_ids':[*state.used_ledger_tx_ids,receipt.ledger_tx_id]})
    return ChosenActionUpdate(state=updated,action_id=receipt.action_id,old_value=old,new_value=new,
        observed_return_minor=reward,event_ref=receipt_fact_id,ledger_tx_id=receipt.ledger_tx_id,
        learning_rate=parameters.learning_rate)
