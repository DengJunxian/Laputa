"""Reuse WP01's sole permission filter and validate cognition at its public boundary."""
from civitas.core.observation import ObservationBuilder
from civitas.contracts.observations import Observation, ObservationFact
from civitas.contracts.time import SimTime
from civitas.contracts.portfolio import FeeSchedule


class CognitionObservationBuilder(ObservationBuilder):
    """Add public event/rule candidates before the unchanged final permission filter."""

    def observe(self, actor_id, snapshot, candidates):
        extra = []
        market = snapshot.data.get('multiasset_market')
        for event in snapshot.events:
            global_fee = (snapshot.schema_version == 3 and market is not None and event.kind == 'fee_change'
                          and not event.actors and set(event.payload) == {'fees'})
            if global_fee:
                FeeSchedule.model_validate(event.payload['fees'])
            if event.payload.get('public') is True or global_fee:
                extra.append(ObservationFact(fact_id='wp05-event:' + event.event_id, category='market', public=True,
                    visible_at=event.visible_at, value={'event_id': event.event_id, 'event_kind': event.kind,
                                                       'root_event_id': event.root_event_id, 'payload': event.payload}))
        if market is not None:
            ledger = market['ledger']
            extra.append(ObservationFact(fact_id='wp05-institution:multiasset', category='market', public=True,
                visible_at=SimTime(tick=snapshot.tick, phase='OBSERVE'),
                value={k: ledger[k] for k in ('fees', 'assets', 'cash_scale')}))
        return super().observe(actor_id, snapshot, [*candidates, *extra])


def validate_bundle(bundle, observation: Observation, policy_id, policy_version):
    for key in ('actor_id', 'observation_id', 'batch_id', 'observation_version', 'world_version'):
        if getattr(bundle, key) != getattr(observation, key):
            raise ValueError('Decision observation identity mismatch: ' + key)
    if (bundle.policy_id, bundle.policy_version) != (policy_id, policy_version):
        raise ValueError('Decision policy identity mismatch')
    if not set(bundle.evidence_ids) <= observation.evidence_ids:
        raise ValueError('Decision cites invisible evidence')
    if hasattr(bundle, 'tick') and bundle.tick != observation.visible_until.tick:
        raise ValueError('Decision tick mismatch')
    return bundle
