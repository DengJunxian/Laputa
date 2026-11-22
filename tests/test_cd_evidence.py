import unittest

from civitas.application.closed_economy import build_closed_economy
from civitas.cognition.evidence import retrieve, with_evidence
from civitas.cognition.memory import AgentMemory, MemoryBook
from civitas.cognition.reflection import ReflectionCandidate


class EvidenceTests(unittest.TestCase):
    def test_content_versions_and_owners_control_memory_body_and_notes(self):
        observation = build_closed_economy({'households': 4}).observe('household:0000')
        actor = observation.actor_id
        rows = [{'category': 'memory', 'owner_id': actor, 'public': False, 'visible_at': {'tick': 0, 'phase': 'OBSERVE'},
                 'fact_id': fact, 'value': {'same': 'text'}} for fact in ('memory:b', 'memory:a')]
        own = with_evidence(observation, private_memory_refs=rows)
        book = MemoryBook([actor])
        reflection = ReflectionCandidate(actor_id=actor, observation_id=own.observation_id,
                                         evidence_ids=['memory:a'], semantic=['candidate note'])
        memory = book.accept(own, reflection=reflection, pin_reflection_versions=True)
        self.assertEqual([ep.evidence_id for ep in retrieve(own, memory).episodic], ['memory:a', 'memory:b'])
        self.assertEqual(len(retrieve(own, memory).semantic), 1)
        for content in ({}, {'edited': 'body'}):
            changed = with_evidence(own, private_memory_refs=[rows[0], {**rows[1], 'value': content}])
            memory = book.accept(changed)
            self.assertEqual(retrieve(changed, memory).semantic, [])
            if not content:
                self.assertEqual([ep.evidence_id for ep in retrieve(changed, memory).episodic], ['memory:b'])
        with self.assertRaisesRegex(ValueError, 'Cross-actor'):
            retrieve(own, AgentMemory(actor_id='household:0001'))
        for invalid in ({'owner_id': 'household:0001'}, {'visible_at': {'tick': 1, 'phase': 'OBSERVE'}}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'unauthorized or future'):
                with_evidence(own, private_memory_refs=[{**rows[0], **invalid}])
