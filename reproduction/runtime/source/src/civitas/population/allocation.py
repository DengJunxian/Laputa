"""Largest remainder with stable account-ID ties; every integer unit is allocated."""
from civitas.contracts.population import AllocationEntry
from civitas.resources.fixedpoint import checked


def allocate(total: int, weights: dict[str,int], resource_id: str) -> list[AllocationEntry]:
    checked(total)
    if not weights or any(type(w) is not int or w<=0 for w in weights.values()):
        raise ValueError('Allocation requires positive integer weights')
    denominator=sum(weights.values())
    divided={actor:divmod(total*w,denominator) for actor,w in weights.items()}
    left=total-sum(q for q,r in divided.values())
    awards=set(sorted(weights,key=lambda a:(-divided[a][1],a))[:left])
    return [AllocationEntry(allocation_id=f'initial:{resource_id}:{actor}',account_id=actor,resource_id=resource_id,
        quota_numerator=total*weights[actor],quota_denominator=denominator,floor_units=divided[actor][0],
        remainder_award=int(actor in awards),allocated_units=divided[actor][0]+int(actor in awards))
        for actor in sorted(weights)]
