"""Prepared single-period actor queries over original observe.v1 segments."""
from hashlib import sha256
from pathlib import Path

from civitas.application.inspection import _agent_payload, _cursor, _inspection_document
from civitas.application.research_cursor import _source
from civitas.storage.commit_store import atomic_json, read_json
from civitas.storage.history import publish_directory


def _actor_file(directory, actor):
    return directory/(sha256(actor.encode()).hexdigest()+'.json')


def _observation_source(root, tick, commit):
    store, prefix = _source(root, tick, commit)
    marker = prefix[-1]
    if store.observation_archive != 'observe.v1' or marker.kind != 'tick' or marker.observations is None:
        raise ValueError('A prepared observation cursor requires a committed observe.v1 period')
    binding = {'schema_version': 1, 'run_id': store.identity.run_id, 'cursor': _cursor(store, marker),
               'observation_segment_sha256': marker.observations.sha256}
    return store, prefix, marker, binding


def build_observation_index(root, index, *, tick, commit, actors):
    store, prefix, marker, binding = _observation_source(root, tick, commit)
    index = Path(index)
    if index.is_symlink() or index.resolve().is_relative_to(store.root):
        raise ValueError('Observation index must be outside the source archive')
    expected = store.observation_decision_ids(prefix, marker)
    actors = set(actors)
    if not actors or not actors <= expected.keys():
        raise ValueError('Select actors registered at this commit')
    rows = store.observation_rows(marker)
    if {row.actor_id: row.observation_id for row in rows} != expected:
        raise ValueError('OBSERVE archive does not match the committed decisions')
    with publish_directory(index) as staging:
        staging.chmod(0o700)
        for row in rows:
            if row.actor_id in actors:
                atomic_json(_actor_file(staging, row.actor_id), row.model_dump(mode='json'))
        atomic_json(staging/'manifest.json', binding)
    return {**binding, 'indexed_actors': len(actors)}


class PreparedObservationCursor:
    """Reuse verified decision bindings during a fixed-commit read session."""

    def __init__(self, root, index, *, tick, commit):
        self.store, prefix, self.marker, self.binding = _observation_source(root, tick, commit)
        self.index = Path(index)
        self._check_index()
        self.expected = self.store.observation_decision_ids(prefix, self.marker)

    def _check_index(self):
        manifest = self.index/'manifest.json'
        if self.index.is_symlink() or manifest.is_symlink() or read_json(manifest) != self.binding:
            raise ValueError('Observation index does not match the requested source commit')

    def read(self, actor):
        if actor not in self.expected:
            raise ValueError('Actor is not registered at this commit')
        self._check_index()
        marker = self.store._read_marker(self.store.marker_path(self.marker.tick))
        if marker != self.marker:
            raise ValueError('Selected observation commit changed')
        self.store._segment_bytes(marker.segment)
        self.store._segment_bytes(marker.observations)
        path = _actor_file(self.index, actor)
        if path.is_symlink():
            raise ValueError('Observation index rows must be local')
        if not path.is_file():
            raise FileNotFoundError('No prepared observation for this actor')
        observation = self.store._indexed_row(marker, read_json(path), actor, self.expected[actor])
        view = {'cursor': self.binding['cursor'], 'verification_scope': 'prepared_observation_cursor',
                **_agent_payload(observation)}
        return _inspection_document(self.store, 'agent', actor, [view])


def read_observation_index(root, index, *, tick, commit, actor):
    return PreparedObservationCursor(root, index, tick=tick, commit=commit).read(actor)
