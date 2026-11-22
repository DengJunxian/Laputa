"""Addressed synthetic populations for the two Goal 03 scale presets."""
from civitas.core.rng import AddressedRNG
from civitas.scenarios.closed_economy_config import ClosedEconomyConfig


def synthetic_population_config(households, seed):
    if households not in (32, 1000) or type(seed) is not int or seed < 0:
        raise ValueError('Generate 32 or 1000 households from a nonnegative integer seed')
    firms = 4 if households == 32 else 40
    rng = AddressedRNG(seed, 0)
    rows = []
    for i in range(households):
        actor = f'household:{i:04d}'

        def draw(slot, span):
            return rng.u64('goal03.synthetic-population.v1', actor, 0, slot) % span

        cautious = draw('prudence', 2) == 1
        rows.append({
            'profile_id': f'synthetic:{actor}',
            'initial_cash_minor': 12 + draw('initial-cash', 69),
            'target_units': 1 + draw('target-units', 3),
            'basket_share_bps': 4000 + draw('basket-share', 6001),
            'cash_reserve_minor': 4,
            'attributes': {'synthetic_trait': 'cautious' if cautious else 'flexible'},
            'behavior': {
                'regular_wage_minor': 12 + 4 * draw('regular-wage', 4),
                'prudence_buffer_minor': 10 if cautious else 0,
                'supplier_priors_bps': {f'firm:{firm:02d}': 6500 + draw(f'prior:{firm}', 3501)
                                        for firm in range(firms)},
                'contact_ids': [f'household:{(i + 1) % households:04d}',
                                f'household:{(i + 2 + draw("second-contact", households - 2)) % households:04d}'],
            },
        })
    return ClosedEconomyConfig.model_validate({
        'ticks': 4, 'households': households, 'firms': firms, 'root_seed': seed,
        'fault_tick': None, 'initial_firm_cash_minor': 5000, 'firm_price_gap_minor': 0,
        'archive_observations': True,
        'population_input': {'source_id': f'goal03:synthetic:v1:{households}:{seed}', 'data_origin': 'synthetic',
                             'household_profiles': rows},
    })
