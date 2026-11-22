"""Durable local registration and launch for a frozen strategy comparison."""
import asyncio
from threading import RLock

from civitas.application.workbench_choices import ChoiceComparisons
from civitas.contracts.base import content_hash
from civitas.experiments.labor_choice_preview import freeze_labor_preview, labor_template_plan, start_labor_preview
from civitas.experiments.withdrawal_preview import (
    freeze_withdrawal_preview, start_withdrawal_preview, withdrawal_template_plan,
)
from civitas.storage.commit_store import CommitStore, atomic_json, read_json


REGISTRATION_TEMPLATES = {
    'withdrawal': (withdrawal_template_plan, freeze_withdrawal_preview, start_withdrawal_preview),
    'labor': (labor_template_plan, freeze_labor_preview, start_labor_preview),
}


class ChoiceRegistration:
    def __init__(self, workspace, kind, config, executor):
        self.kind = kind
        self.path = workspace/f'{kind}-registration.json'
        self.root = workspace/f'{kind}-pair'
        proposal_for, self.freeze, self.start = REGISTRATION_TEMPLATES[kind]
        self.proposal = proposal_for(config)
        self.template_hash = content_hash(self.proposal)
        self.executor = executor
        self.lock = RLock()
        self.comparisons = None
        if self.path.is_symlink() or self.root.is_symlink():
            raise ValueError('Comparison registration paths must be local')
        self.record = {'schema_version': 1, 'template_hash': self.template_hash,
                       'phase': 'empty', 'plan_hash': None, 'archive_hash': None, 'failure': None,
                       'arms': {'rule': 'not_started', 'archive': 'not_started'}}
        if not self.path.exists():
            if self.root.exists():
                raise ValueError('This workspace already contains a comparison archive')
            return
        self.record = read_json(self.path)
        if self.record.get('schema_version') != 1 or self.record.get('template_hash') != self.template_hash:
            raise ValueError('Use the original template and implementation for this comparison registration')
        phase = self.record['phase']
        if phase in ('registering', 'running'):
            self.finish('interrupted', failure=phase)
        elif phase == 'completed':
            self.comparisons = self.validated_comparison()

    def save(self, **changes):
        updated = {**self.record, **changes}
        atomic_json(self.path, updated)
        self.record = updated

    def finish(self, phase, *, failure):
        arms = {side: phase if state == 'running' else state for side, state in self.record['arms'].items()}
        self.save(phase=phase, arms=arms, failure=failure)

    def status(self):
        with self.lock:
            arms = {}
            for side, state in self.record['arms'].items():
                path = self.root/side
                head = None
                if path.is_symlink():
                    raise ValueError('Comparison execution paths must be local directories')
                if (path/'commits'/'00000000.json').is_file():
                    head = CommitStore(path).markers()[-1]
                arms[side] = {'state': state, 'tick': head.tick if head else None,
                              'commit_hash': head.commit_hash if head else None}
            states = list(self.record['arms'].values())
            assigned = 2 if self.record['plan_hash'] else 0
            return {**self.record, 'proposal': self.proposal, 'arms': arms,
                    'denominators': {'assigned_arms': assigned, 'started_arms': 2 - states.count('not_started'),
                                     'completed_arms': states.count('completed'), 'failed_arms': states.count('failed'),
                                     'incomplete_arms': assigned - states.count('completed')}}

    def command(self, raw):
        if not isinstance(raw, dict) or set(raw) != {'action', 'expected_hash'}:
            raise ValueError('Choose registration or launch with its reviewed hash')
        action = raw['action']
        with self.lock:
            phase = self.record['phase']
            if action == 'register':
                expected, allow, pending = self.template_hash, 'empty', 'registering'
            elif action == 'start' and self.record['plan_hash']:
                expected, allow, pending = self.record['plan_hash'], 'ready', 'running'
            else:
                raise ValueError('Register a comparison plan before starting')
            if raw['expected_hash'] != expected:
                raise ValueError('The reviewed comparison configuration has changed')
            if phase == allow:
                self.save(phase=pending)
                self.executor.submit(self.advance, pending)
            return {'phase': self.record['phase'], 'plan_hash': self.record['plan_hash']}

    def progress(self, side, state):
        with self.lock:
            self.save(arms={**self.record['arms'], side: state})

    def validated_comparison(self):
        comparisons = ChoiceComparisons((self.root,))
        report = comparisons.pair(self.kind)['report']
        if report['status'] != 'completed' or report['plan_hash'] != self.record['plan_hash']:
            raise ValueError('Comparison completion requires an audited match to the registered plan')
        return comparisons

    def advance(self, phase):
        try:
            if phase == 'registering':
                plan = asyncio.run(self.freeze(self.proposal['arms']['rule'], self.root))
                if {**plan, 'archive_hash': None} != self.proposal:
                    raise ValueError('Frozen comparison configuration differs from the reviewed proposal')
                with self.lock:
                    self.save(phase='ready', plan_hash=content_hash(plan), archive_hash=plan['archive_hash'])
            else:
                result = asyncio.run(self.start(
                    self.root, self.record['plan_hash'], progress=self.progress))
                with self.lock:
                    if result['status'] == 'completed' and set(self.record['arms'].values()) == {'completed'}:
                        comparisons = self.validated_comparison()
                        self.save(phase='completed')
                        self.comparisons = comparisons
                    else:
                        self.finish('failed', failure='comparison')
        except Exception:
            with self.lock:
                self.finish('failed', failure=phase)
