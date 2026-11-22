"""Explicit synthetic endowments, not empirical investor estimates."""
from typing import Any, Literal
from pydantic import Field, model_validator
from .base import Id, Positive, Record, require_unique
from .portfolio import AccountState, AssetSpec, Ticks, Units


class WealthPrior(Record):
    distribution: Literal['uniform_integer_weight'] = 'uniform_integer_weight'
    min_weight: Ticks = 1
    max_weight: Ticks = 1000

    @model_validator(mode='after')
    def interval(self):
        if self.min_weight>self.max_weight:
            raise ValueError('Invalid wealth weight interval')
        return self


class PopulationSpec(Record):
    schema_version: Literal[3] = 3
    population_id: Id
    n_agents: Positive
    cash_scale: Ticks = 1_000_000
    cash_total_minor: Units
    assets: list[AssetSpec] = Field(min_length=1)
    asset_supplies: dict[Id,Units]
    initial_price_ticks: dict[Id,Ticks]
    target_weights: dict[Id,Units]
    wealth_prior: WealthPrior = Field(default_factory=WealthPrior)
    mismatch_policy: Literal['report','reject'] = 'report'

    @model_validator(mode='after')
    def resources(self):
        require_unique([a.asset_id for a in self.assets],'asset ID')
        assets={a.asset_id for a in self.assets}
        currencies={a.quote_currency for a in self.assets}
        if len(currencies)!=1 or currencies & assets:
            raise ValueError('One separate quote currency required')
        if set(self.asset_supplies)!=assets or set(self.initial_price_ticks)!=assets:
            raise ValueError('Supply/price must cover every asset')
        if set(self.target_weights)!=assets|currencies or sum(self.target_weights.values())==0:
            raise ValueError('Nonzero target weights must cover all resources')
        return self


class Profile(Record):
    schema_version: Literal[3] = 3
    profile_id: Id
    actor_id: Id
    archetype: Literal['synthetic_trader'] = 'synthetic_trader'
    data_origin: Literal['synthetic_model_population'] = 'synthetic_model_population'
    wealth_weight: Ticks


class AllocationEntry(Record):
    schema_version: Literal[3] = 3
    allocation_id: Id
    account_id: Id
    resource_id: Id
    quota_numerator: int = Field(ge=0)
    quota_denominator: int = Field(gt=0)
    floor_units: Units
    remainder_award: Literal[0,1]
    allocated_units: Units


class PopulationInitialization(Record):
    schema_version: Literal[3] = 3
    initialization_id: Id
    data_origin: Literal['synthetic_model_population'] = 'synthetic_model_population'
    profiles: list[Profile]
    accounts: list[AccountState]
    cognition: dict[Id,dict[str,Any]]
    journal: list[AllocationEntry]
    statistics: dict[str,Any]
