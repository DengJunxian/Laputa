"""Frozen household inputs; demographic attributes carry no latent behavior inference."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from civitas.contracts.base import Id, Positive, Record, require_unique
from civitas.contracts.portfolio import MAX_INT


BasisPoints = Annotated[int, Field(ge=0, le=10000)]
Cash = Annotated[int, Field(ge=0, le=MAX_INT)]
DataOrigin = Literal['synthetic', 'calibrated_seed']


class HouseholdBehavior(Record):
    """Synthetic policy parameters with explicit economic and social meanings."""
    regular_wage_minor: Annotated[int, Field(gt=0, le=MAX_INT)]
    prudence_buffer_minor: Cash
    supplier_priors_bps: dict[Id, BasisPoints]
    contact_ids: list[Id]

    @model_validator(mode='after')
    def unique_contacts(self):
        require_unique(self.contact_ids, 'household contact')
        return self


class HouseholdProfile(Record):
    profile_id: Id
    initial_cash_minor: Cash
    target_units: Positive
    basket_share_bps: BasisPoints
    cash_reserve_minor: Cash
    attributes: dict[Id, Id] = Field(default_factory=dict)
    behavior: HouseholdBehavior | None = Field(default=None, exclude_if=lambda value: value is None)


class HouseholdPopulation(Record):
    schema_version: Literal[1] = 1
    source_id: Id
    data_origin: DataOrigin
    household_profiles: list[HouseholdProfile] = Field(min_length=2, max_length=10000)

    @model_validator(mode='after')
    def unique_rows(self):
        require_unique([p.profile_id for p in self.household_profiles], 'household profile')
        return self
