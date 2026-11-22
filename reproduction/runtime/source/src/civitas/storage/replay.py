"""State replay and execution verification have separate, non-composable paths."""
from .checkpoint import participant_versions


def state_replay(store, *, tick=None):
    return store.load(tick)[1]


async def verification_replay(store, build_world):
    world = build_world()
    if world.commit_store is not None:
        raise ValueError('Verification execution must not write to the source store')
    if (participant_versions(world) != store.identity.participants
            or world.rng._root.model_dump(mode='json') != store.identity.rng):
        raise ValueError('Verification participant/RNG identity mismatch')
    expected = iter(store.states())
    initial, snapshot = next(expected)
    world.restore_snapshot(snapshot)
    compared = 0
    for record, snapshot in expected:
        await world.step()
        compared += 1
        actual_hash = world.snapshot.state_hash
        if actual_hash != record.state_hash:
            left, right = snapshot.model_dump(mode='json'), world.snapshot.model_dump(mode='json')
            components = [key for key in left if key != 'data' and left[key] != right[key]]
            components += ['data.'+key for key in sorted(left['data'].keys() | right['data'].keys())
                           if left['data'].get(key) != right['data'].get(key)]
            return {'status':'different','compared_ticks':compared,'applied_deltas_to_execution':False,
                    'first_divergence':{'tick':record.tick,'commit_hash':record.commit_hash,'components':components,
                                        'expected_hash':record.state_hash,'actual_hash':actual_hash}}
    return {'status':'matched','compared_ticks':compared,'initial_tick':initial.tick,
            'final_tick':world.snapshot.tick,'state_hash':world.snapshot.state_hash,
            'applied_deltas_to_execution':False,'first_divergence':None}
