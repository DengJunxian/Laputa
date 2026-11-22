"""Pure joint-scenario wealth projections using visible quotes and integer execution fees."""
from fractions import Fraction
import math
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from civitas.contracts.base import Id, NonNegative, Positive, Record, content_hash, require_unique
from civitas.contracts.observations import Observation
from civitas.contracts.portfolio import AssetSpec, FeeSchedule, PortfolioAccountView, SignedUnits, Ticks
from civitas.resources.fixedpoint import fee, notional

MASS_TOLERANCE = 1e-9


class ExactAmount(Record):
    numerator: int
    denominator: Positive = 1

    @model_validator(mode='after')
    def reduced(self):
        if math.gcd(self.numerator, self.denominator) != 1:
            raise ValueError('Exact amount must be reduced')
        return self

    @property
    def fraction(self):
        return Fraction(self.numerator, self.denominator)

    @classmethod
    def of(cls, value):
        if type(value) not in (int, Fraction):
            raise ValueError('Exact amounts require an integer or Fraction')
        value = Fraction(value)
        return cls(numerator=value.numerator, denominator=value.denominator)


class PriceAnchor(Record):
    asset_id: Id
    price_minor: Positive
    evidence_id: Id
    observed_tick: NonNegative


class AccountingCost(Record):
    asset_id: Id
    cost_minor: NonNegative


class JointOutcome(Record):
    scenario_id: Id
    horizon_ticks: Positive
    probability: float = Field(ge=0.0, le=1.0)
    asset_returns: dict[Id, float]
    execution: Literal['filled', 'unfilled'] = 'filled'

    @model_validator(mode='after')
    def positive_prices(self):
        if not self.asset_returns or any(r <= -1.0 for r in self.asset_returns.values()):
            raise ValueError('Returns must imply positive prices')
        return self


class JointOutcomes(Record):
    scenarios: list[JointOutcome] = Field(min_length=1)

    @model_validator(mode='after')
    def common_space(self):
        require_unique([s.scenario_id for s in self.scenarios], 'scenario ID')
        first = self.scenarios[0]
        if any(s.horizon_ticks != first.horizon_ticks or set(s.asset_returns) != set(first.asset_returns)
               for s in self.scenarios):
            raise ValueError('Joint scenarios require identical assets and horizons')
        if not math.isclose(math.fsum(s.probability for s in self.scenarios), 1.0, rel_tol=0, abs_tol=MASS_TOLERANCE):
            raise ValueError('Probability mass must sum to one; no automatic normalization')
        return self


class CandidateAction(Record):
    action_id: Id
    delta_units: dict[Id, SignedUnits] = Field(default_factory=dict)
    effort: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def economic_key(self):
        return content_hash({k: v for k, v in self.delta_units.items() if v})


class OutcomeWealth(Record):
    scenario_id: Id
    horizon_ticks: Positive
    probability: float
    execution: Literal['filled', 'unfilled']
    cash_minor: NonNegative
    positions: dict[Id, NonNegative]
    fees_minor: NonNegative
    fee_components: dict[Id, dict[Id, NonNegative]]
    future_prices: dict[Id, ExactAmount]
    wealth: ExactAmount


class Projection(Record):
    observation_id: Id
    candidate: CandidateAction
    feasible: bool
    reasons: list[Id]
    assumed_price_ticks: dict[Id, Ticks]
    execution_assumption: Literal['one_fill_per_asset_at_visible_last_quote'] = 'one_fill_per_asset_at_visible_last_quote'
    outcomes: list[OutcomeWealth]


def market_inputs(observation):
    observation = Observation.model_validate(observation)
    if len(observation.account_view) != 1:
        raise ValueError('Exactly one own portfolio account required')
    account = PortfolioAccountView.model_validate(observation.account_view[0].value)
    if account.account_id != observation.actor_id:
        raise ValueError('Account owner mismatch')
    rules = [f for f in observation.market_view if f.fact_id == 'wp05-institution:multiasset']
    quotes = [f for f in observation.market_view if f.fact_id.startswith('quotes:')]
    if len(rules) != 1 or len(quotes) != 1:
        raise ValueError('Visible asset rules and last quotes required')
    assets = [AssetSpec.model_validate(a) for a in rules[0].value['assets']]
    require_unique([a.asset_id for a in assets], 'asset')
    currencies = {a.quote_currency for a in assets}
    if len(currencies) != 1 or not assets or account.cash_scale != rules[0].value['cash_scale']:
        raise ValueError('One common currency and consistent cash units required')
    currency = next(iter(currencies))
    resources = {a.asset_id for a in assets} | {currency}
    if any(set(values) != resources for values in (account.balances, account.available, account.reserved)):
        raise ValueError('Account asset universe mismatch')
    if set(account.locked) - resources or set(quotes[0].value) != {a.asset_id for a in assets}:
        raise ValueError('Unknown account or quote asset')
    if any(account.available[r] != account.balances[r] - account.reserved[r] - account.locked.get(r, 0) for r in resources):
        raise ValueError('Available resources disagree with locks/reservations')
    prices = {a.asset_id: TypeAdapter(Ticks).validate_python(quotes[0].value[a.asset_id]['last_price_ticks'], strict=True)
              for a in assets}
    return account, {a.asset_id:a for a in assets}, prices, FeeSchedule.model_validate(rules[0].value['fees'])


class OutcomeProjector:
    def project(self, observation, candidate, joint_outcomes):
        candidate = CandidateAction.model_validate(candidate)
        joint_outcomes = JointOutcomes.model_validate(joint_outcomes)
        account, assets, prices, fees = market_inputs(observation)
        if set(candidate.delta_units) - set(assets) or set(joint_outcomes.scenarios[0].asset_returns) != set(assets):
            raise ValueError('Unknown or missing scenario/candidate assets')
        currency = next(iter(assets.values())).quote_currency
        changes, components, purchases, reasons = {}, {}, 0, []
        for asset, quantity in sorted(candidate.delta_units.items()):
            if quantity < 0 and -quantity > account.available[asset]:
                reasons.append('unavailable_position:' + asset)
                continue
            raw_value = prices[asset] * assets[asset].price_tick_minor * abs(quantity)
            if quantity > 0 and raw_value > account.available[currency]:
                reasons.append('unavailable_cash')
                continue
            try:
                value = notional(prices[asset], assets[asset].price_tick_minor, abs(quantity))
            except ValueError:
                reasons.append('notional_overflow:' + asset)
                continue
            commission, tax = fee(value, fees.commission), fee(value, fees.tax) if quantity < 0 else 0
            components[asset] = {'commission': commission, 'tax': tax}
            changes[asset] = value * (1 if quantity > 0 else -1)
            if quantity > 0:
                purchases += value + commission
        if purchases > account.available[currency]:
            reasons.append('unavailable_cash')
        rows = []
        if not reasons:
            for scenario in sorted(joint_outcomes.scenarios, key=lambda s:s.scenario_id):
                filled = scenario.execution == 'filled'
                charges = sum(sum(c.values()) for c in components.values()) if filled else 0
                cash = account.balances[currency] - (sum(changes.values()) if filled else 0) - charges
                positions = {a:account.balances[a] + (candidate.delta_units.get(a, 0) if filled else 0) for a in assets}
                future = {a:Fraction(prices[a] * assets[a].price_tick_minor) * (1 + Fraction(str(scenario.asset_returns[a]))) for a in assets}
                wealth = Fraction(cash) + sum(future[a] * positions[a] for a in assets)
                rows.append(OutcomeWealth(scenario_id=scenario.scenario_id, horizon_ticks=scenario.horizon_ticks,
                    probability=scenario.probability, execution=scenario.execution, cash_minor=cash, positions=positions,
                    fees_minor=charges, fee_components=components if filled else {},
                    future_prices={a:ExactAmount.of(v) for a,v in future.items()}, wealth=ExactAmount.of(wealth)))
        return Projection(observation_id=observation.observation_id, candidate=candidate, feasible=not reasons,
            reasons=reasons, assumed_price_ticks=prices, outcomes=rows)
