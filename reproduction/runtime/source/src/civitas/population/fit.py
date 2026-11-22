"""Offline IPF for weighted joint household cells, using the pinned ipfn NumPy backend."""
from fractions import Fraction
from math import prod

from pydantic import Field, model_validator

from civitas.contracts.base import Id, NonNegative, Positive, Record, content_hash, require_unique
from civitas.contracts.households import DataOrigin, HouseholdPopulation, HouseholdProfile
from .allocation import allocate


FIT_VERSION = 'ipfn-1.4.4-fa57b905'


class SeedCell(Record):
    coordinates: list[Id]
    weight: Positive
    profile: HouseholdProfile


class Margin(Record):
    axes: list[Id] = Field(min_length=1)
    counts: list[NonNegative] = Field(min_length=1)


class HouseholdFit(Record):
    source_id: Id
    data_origin: DataOrigin
    axes: dict[Id, list[Id]]
    households: int = Field(ge=2, le=10000)
    seed: list[SeedCell] = Field(min_length=1)
    controls: list[Margin] = Field(min_length=1)
    holdout: list[Margin] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_design(self):
        if not 1 <= len(self.axes) <= 6 or not all(self.axes.values()) or prod(map(len, self.axes.values())) > 10000:
            raise ValueError('Expected one to six nonempty axes covering at most 10000 joint cells')
        for levels in self.axes.values():
            require_unique(levels, 'population axis level')
        require_unique([tuple(c.coordinates) for c in self.seed], 'joint seed cell')
        require_unique([c.profile.profile_id for c in self.seed], 'seed profile ID')
        for cell in self.seed:
            pairs = list(zip(self.axes.items(), cell.coordinates))
            if (len(cell.coordinates) != len(self.axes) or any(level not in levels for (_, levels), level in pairs)
                    or any(key in cell.profile.attributes and cell.profile.attributes[key] != level
                           for (key, _), level in pairs)):
                raise ValueError('Seed coordinates and demographic attributes must agree')
        require_unique([tuple(m.axes) for m in self.controls + self.holdout], 'fit or holdout axes')
        for margin in self.controls + self.holdout:
            if (margin.axes != [key for key in self.axes if key in margin.axes]
                    or len(margin.counts) != prod(len(self.axes[key]) for key in margin.axes)
                    or sum(margin.counts) != self.households):
                raise ValueError('Margins must match ordered domain and household total')
        return self


def fit_households(spec):
    import numpy as np
    from ipfn.ipfn import ipfn

    spec = HouseholdFit.model_validate(spec)
    # JSON identities have unordered dictionary keys. Preserve the declared axis sequence in the fit identity.
    axes = list(spec.axes)
    shape = tuple(len(spec.axes[a]) for a in axes)
    positions = {tuple(spec.axes[key].index(value) for key, value in zip(axes, c.coordinates)): c for c in spec.seed}
    seed = np.zeros(shape, dtype=float)
    for pos, cell in positions.items():
        seed[pos] = cell.weight
    if not np.isfinite(seed).all():
        raise ValueError('Seed weights exceed fitting precision')

    def dimensions(margin):
        return [axes.index(key) for key in margin.axes]

    def marginal(table, dim):
        return table.sum(axis=tuple(i for i in range(table.ndim) if i not in dim))

    dims = [dimensions(m) for m in spec.controls]
    targets = [np.array(m.counts).reshape(tuple(shape[i] for i in dim))
               for m, dim in zip(spec.controls, dims)]
    support = seed > 0
    for target, dim in zip(targets, dims):
        support &= target.reshape(tuple(n if i in dim else 1 for i, n in enumerate(shape))) > 0

    report = {'fit_version': FIT_VERSION, 'input_hash': content_hash({'axes': axes, 'spec': spec.model_dump()}),
              'axes': spec.axes, 'axis_order': axes, 'households': spec.households,
              'structural_zero_cells': int(np.count_nonzero(seed == 0)),
              'excluded_by_zero_controls': int(np.count_nonzero((seed > 0) & ~support)),
              'unsupported_controls': [], 'population_input': None}
    for control, target, dim in zip(spec.controls, targets, dims):
        missing = np.flatnonzero((target > 0) & (marginal(support, dim) == 0))
        if missing.size:
            report['unsupported_controls'].append({'axes': control.axes, 'cell_indices': missing.tolist()})
    if report['unsupported_controls']:
        return {**report, 'status': 'infeasible_support'}

    # ipfn's relative convergence divides by zero targets. Inspect absolute residuals independently.
    with np.errstate(divide='ignore', invalid='ignore'):
        fitted = ipfn(np.where(support, seed, 0.), targets, dims,
                      convergence_rate=1e-10, rate_tolerance=0, max_iteration=1000).iteration()

    def details(margins, integer=None):
        result = []
        for margin in margins:
            dim = dimensions(margin)
            predicted = marginal(fitted, dim).ravel()
            rounded = marginal(integer, dim).ravel() if integer is not None else None
            residual = predicted - margin.counts
            result.append({'axes': margin.axes, 'expected': margin.counts, 'weighted': predicted.tolist(),
                           'weighted_residual': residual.tolist(),
                           'max_abs_weighted_residual': float(abs(residual).max()),
                           'integer': rounded.tolist() if rounded is not None else None,
                           'integer_residual': (rounded - margin.counts).tolist() if rounded is not None else None})
        return result

    if not np.isfinite(fitted).all() or (fitted < 0).any() or fitted[~support].any():
        raise ArithmeticError('IPF produced invalid joint weights')
    report['weighted_cells'] = fitted.ravel().tolist()
    report['weighted_total'] = float(fitted.sum())
    report['controls'], report['holdout'] = details(spec.controls), details(spec.holdout)
    if (abs(report['weighted_total'] - spec.households) > 1e-5
            or any(m['max_abs_weighted_residual'] > 1e-5 for m in report['controls'])):
        return {**report, 'status': 'residual_exceeds_tolerance'}

    # A float's exact denominator is a power of two; keep allocation ties deterministic.
    shares = {str(i).zfill(5): Fraction(float(w)) for i, w in enumerate(fitted.ravel()) if w > 0}
    scale = max(w.denominator for w in shares.values())
    allocation = allocate(spec.households, {k: int(w * scale) for k, w in shares.items()}, 'households')
    integer = np.zeros(shape, dtype=int)
    profiles = []
    for entry in allocation:
        pos = np.unravel_index(int(entry.account_id), shape)
        cell = positions[pos]
        integer[pos] = entry.allocated_units
        attributes = {**cell.profile.attributes, **dict(zip(axes, cell.coordinates))}
        for i in range(entry.allocated_units):
            profiles.append(cell.profile.model_copy(update={
                'profile_id': f'{cell.profile.profile_id}:{i:04d}', 'attributes': attributes}))
    population = HouseholdPopulation(source_id=spec.source_id, data_origin=spec.data_origin, household_profiles=profiles)
    return {**report, 'status': 'fitted', 'integer_cells': integer.ravel().tolist(),
            'controls': details(spec.controls, integer), 'holdout': details(spec.holdout, integer),
            'population_input': population.model_dump(mode='json')}
