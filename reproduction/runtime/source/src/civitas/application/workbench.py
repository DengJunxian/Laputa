"""Local fixed-horizon controls over the economic runner and its committed markers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock

from civitas.application import economic_run
from civitas.application.comparison_inspection import _metrics, joint_pilot_comparison, joint_timeline
from civitas.application.inspection import (_cursor, _frame, _indexed_agent_frame, _inspection_document,
                                            joint_actor_settlement, joint_relation_layers)
from civitas.application.research_cursor import build_research_index, research_page
from civitas.application.workbench_choices import ChoiceComparisons
from civitas.application.choice_registration import ChoiceRegistration, REGISTRATION_TEMPLATES
from civitas.contracts.base import content_hash
from civitas.experiments.registry import history, run_lock
from civitas.experiments.intervention_preview import catalog, evolution, recipe, registration
from civitas.experiments.supplier_preview import (PERIOD, load_supplier_preview, summarize_supplier_arm,
                                                 supplier_comparison, supplier_side)
from civitas.scenarios.closed_economy_relations import SOCIAL_FIELD, materialize_social
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from civitas.storage.history import parent_reference, publish_directory


ACTIVE = {'running', 'pausing', 'cancelling'}
RESUMABLE = {'paused', 'interrupted', 'failed'}
COMMAND_FIELDS = {'freeze': {'label', 'config'}, 'freeze-joint-template': {'expected_hash'},
                  'freeze-intervention': {'kind', 'side'}, 'fork': {'label', 'job', 'tick', 'intervention'},
                  'start': {'job', 'until'}, 'resume': {'job', 'until'},
                  'pause': {'job'}, 'cancel': {'job'}}


class ControlConflict(ValueError):
    pass


def job_key(value):
    if not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{32}', value) is None:
        raise ValueError('Expected a local workbench identifier')
    return value


@contextmanager
def open_workspace(root, template, *, supplier_preview=None, choice_previews=(), withdrawal_template=None,
                   labor_template=None, joint_template=None, joint_pilot=None):
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with run_lock(root):
        workbench = Workbench(root, template, supplier_preview=supplier_preview, choice_previews=choice_previews,
                              withdrawal_template=withdrawal_template, labor_template=labor_template,
                              joint_template=joint_template, joint_pilot=joint_pilot)
        try:
            yield workbench
        finally:
            workbench.close()


class Workbench:
    def __init__(self, root, template, *, supplier_preview=None, choice_previews=(), withdrawal_template=None,
                 labor_template=None, joint_template=None, joint_pilot=None):
        self.jobs = root / 'jobs'
        self.runs = root / 'runs'
        self.research_indexes = root / 'research-index'
        for directory in (self.jobs, self.runs, self.research_indexes):
            if directory.is_symlink():
                raise ValueError('Workspace directories must be local directories')
            directory.mkdir(mode=0o700, exist_ok=True)
        self.lock = RLock()
        self.closing = False
        self.implementation = economic_run.current_implementation()
        self.registration = None
        templates = {'withdrawal': withdrawal_template, 'labor': labor_template}
        chosen = [(kind, raw) for kind, raw in templates.items() if raw is not None]
        if len(chosen) + bool(supplier_preview) + bool(choice_previews) > 1:
            raise ValueError('Choose one workbench preview mode')
        for kind in REGISTRATION_TEMPLATES:
            if templates[kind] is None and any(path.exists() or path.is_symlink() for path in (
                root/f'{kind}-registration.json', root/f'{kind}-pair',
            )):
                raise ValueError('This workspace is dedicated to a comparison registration')
        self._choices = ChoiceComparisons(choice_previews) if choice_previews else None
        self.preview = load_supplier_preview(supplier_preview) if supplier_preview is not None else None
        self.preview_hash = content_hash(self.preview[0]) if self.preview else None
        self.intervention_templates = catalog() if not (self.preview or self._choices or chosen) else []
        template = self.preview[0]['configs']['rule'] if self.preview else template
        config = self.config(template)
        self.template = json.dumps(template, ensure_ascii=False, indent=2)
        self.joint_template = None
        self.joint_config = None
        self.joint_pilot = None
        if not (self.preview or self._choices or chosen):
            candidate = self.config(joint_template) if joint_template is not None else config
            if candidate.security_feedback is not None and candidate.archive_observations:
                self.joint_config = candidate.model_dump(mode='json')
                self.joint_template = {'id': 'joint-security-feedback.v1', 'label': '联合世界 · 证券冲击',
                                       'config_hash': content_hash(self.joint_config), 'ticks': candidate.ticks}
            self.joint_pilot = joint_pilot
        jobs = [self.load(path.stem) for path in sorted(self.jobs.glob('*.json'))
                if re.fullmatch(r'[a-f0-9]{32}', path.stem)]
        if (self.choices or chosen) and jobs:
            raise ValueError('Use a fresh workspace for the frozen comparisons')
        if any(job.get('supplier_preview_hash') != self.preview_hash for job in jobs):
            raise ValueError('Use a workspace dedicated to this supplier registration')
        for job in jobs:
            if job['status'] in ACTIVE:
                job['status'] = 'cancelled' if job['status'] == 'cancelling' else 'interrupted'
                self.save(job)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ecosim-workbench')
        if chosen:
            kind, raw = chosen[0]
            self.registration = ChoiceRegistration(root, kind, raw, self.executor)

    @property
    def choices(self):
        return self.registration.comparisons if self.registration else self._choices

    def config(self, raw):
        config = economic_run.parse_config(raw)
        if self.preview:
            supplier_side(self.preview[0], config.model_dump(mode='json'))
        elif config.cognition_mode != 'rule' or config.stopping_hazard_bps is not None:
            raise ValueError('Local workbench runs require a fixed horizon and rule decisions')
        return config

    def load(self, key):
        path = self.jobs / (job_key(key) + '.json')
        if not path.exists():
            raise FileNotFoundError('Unknown workbench run')
        job = read_json(path)
        if job.get('schema_version') != 1 or job.get('id') != key:
            raise ValueError('Invalid workbench run record')
        return job

    def save(self, job):
        atomic_json(self.jobs / (job['id'] + '.json'), job)

    def run_path(self, key):
        if self.choices:
            return self.choices.arm(key)['path']
        path = self.runs / job_key(key)
        if path.is_symlink():
            raise ValueError('Unsafe workbench archive path')
        return path

    def head(self, key):
        path = self.run_path(key)
        return CommitStore(path).markers()[-1] if path.exists() else None

    def summary(self, job, *, detail):
        head = self.head(job['id'])
        result = {key: job[key] for key in ('id', 'label', 'status', 'terminal_tick')}
        result['tick'] = head.tick if head is not None else None
        if detail:
            result.update(first_tick=job['parent']['tick'] if job['parent'] else 0,
                          commit_hash=head.commit_hash if head is not None else None,
                          config_hash=job['config_hash'], parent=job['parent'], intervention=job['intervention'],
                          error=job['error'])
        return result

    def status(self, scope):
        if self.registration and scope is None:
            with self.registration.lock:
                result = self.choices.status(scope) if self.choices else {'role': 'researcher', 'choice_archive': True}
                return {**result, f'{self.registration.kind}_registration': self.registration.status()}
        if self.choices:
            return self.choices.status(scope)
        if scope is not None:
            key, actor = scope
            return {'role': 'observer', 'actor': actor, 'supplier_preview': bool(self.preview),
                    'jobs': [self.summary(self.load(key), detail=False)]}
        jobs = [self.load(path.stem) for path in sorted(self.jobs.glob('*.json'))
                if re.fullmatch(r'[a-f0-9]{32}', path.stem)]
        return {'role': 'researcher', 'template': self.template, 'supplier_preview': bool(self.preview),
                'joint_template': self.joint_template, 'joint_pilot': self.joint_pilot is not None,
                'intervention_templates': self.intervention_templates,
                'jobs': [self.summary(job, detail=True) for job in sorted(jobs, key=lambda row: row['created_at'])]}

    def command(self, raw):
        action = raw.get('action') if isinstance(raw, dict) else None
        if not isinstance(action, str) or action not in COMMAND_FIELDS or set(raw) != COMMAND_FIELDS[action] | {'action', 'request_id'}:
            raise ValueError('Invalid workbench command or fields')
        request = job_key(raw['request_id'])
        creating = action in ('freeze', 'fork', 'freeze-intervention', 'freeze-joint-template')
        key = request if creating else job_key(raw['job'])
        digest = content_hash(raw)
        with self.lock:
            try:
                job = self.load(key)
            except FileNotFoundError:
                if not creating:
                    raise
                job = self.freeze(key, raw)
            previous = job['requests'].get(request)
            if previous is not None:
                if previous['hash'] != digest:
                    raise ControlConflict('Request identifier was already used for different inputs')
                return previous['receipt']
            if creating and job['requests']:
                raise ControlConflict('This frozen run already exists')
            if not creating:
                self.move(job, raw)
            receipt = {'job': key, 'action': action, 'accepted_status': job['status']}
            job['requests'][request] = {'hash': digest, 'receipt': receipt,
                                        'recorded_at': datetime.now(timezone.utc).isoformat()}
            self.save(job)
            if action in ('start', 'resume'):
                self.executor.submit(self.advance, key)
            return receipt

    def freeze(self, key, raw):
        design = None
        joint = raw['action'] == 'freeze-joint-template'
        if joint and (self.joint_template is None or raw['expected_hash'] != self.joint_template['config_hash']):
            raise ValueError('Choose the registered joint world template and its full configuration hash')
        if raw['action'] == 'freeze-intervention':
            if not self.intervention_templates:
                raise ValueError('Intervention templates require a normal research workspace')
            design = registration(raw['kind'], raw['side'])
        label = (self.joint_template['label'] if joint else design['label'] if design else raw['label'])
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError('Choose a run name of 1–80 characters')
        parent = intervention = source_job = None
        if raw['action'] == 'fork':
            if self.preview:
                raise ValueError('This registered supplier preview starts each arm from its initial state')
            source_job = job_key(raw['job'])
            if self.load(source_job)['status'] not in {'paused', 'completed'}:
                raise ControlConflict('Pause or finish the parent before freezing a branch')
            source, marker, _, intervention = economic_run.prepare_branch(
                self.run_path(source_job), tick=raw['tick'], intervention=raw['intervention'])
            parent = parent_reference(source, marker)
            config = self.config(source.identity.config)
        elif joint:
            config = self.config(self.joint_config)
        else:
            config = self.config(recipe(raw['kind'], raw['side']) if design else raw['config'])
        frozen = config.model_dump(mode='json')
        return {'schema_version': 1, 'id': key, 'label': label.strip(), 'status': 'ready',
                'created_at': datetime.now(timezone.utc).isoformat(), 'config': frozen,
                'config_hash': content_hash(frozen), 'terminal_tick': config.ticks,
                'source_hash': self.implementation['source_hash'], 'dependency_hash': self.implementation['dependency_hash'],
                'parent': parent, 'source_job': source_job, 'intervention': intervention,
                'until': None, 'requests': {}, 'error': None,
                **({'supplier_preview_hash': self.preview_hash} if self.preview else {}),
                **({'registered_template': self.joint_template['id']} if joint else {}),
                **({'intervention_design': design} if design else {})}

    def move(self, job, raw):
        action, state = raw['action'], job['status']
        if action in ('start', 'resume'):
            valid = {'ready'} if action == 'start' else RESUMABLE
            if self.closing or state not in valid:
                raise ControlConflict('This run cannot be started or resumed in its current state')
            head = self.head(job['id'])
            first = head.tick if head is not None else (job['parent']['tick'] if job['parent'] else 0)
            until = job['terminal_tick'] if raw['until'] is None else raw['until']
            if type(until) is not int or not first <= until <= job['terminal_tick']:
                raise ValueError('Select a target from the last commit through the frozen horizon')
            job.update(status='running', until=until, error=None)
        elif action == 'pause' and state == 'running':
            job['status'] = 'pausing'
        elif action == 'cancel' and state in ACTIVE | RESUMABLE | {'ready'} and state != 'cancelling':
            job['status'] = 'cancelling' if state in ACTIVE else 'cancelled'
        else:
            raise ControlConflict('This action is unavailable in the current state')

    def initialize(self, job, path):
        if content_hash(job['config']) != job['config_hash']:
            raise ValueError('Frozen workbench configuration changed')
        implementation = economic_run.current_implementation()
        if any(job[k] != implementation[k] for k in ('source_hash', 'dependency_hash')):
            raise ValueError('Frozen execution version changed')
        if job['parent'] is not None:
            source_path = self.run_path(job['source_job'])
            store = CommitStore(source_path)
            tick = job['parent']['tick']
            marker = next((row for row in store.markers() if row.tick == tick), None)
            if marker is None or parent_reference(store, marker) != job['parent']:
                raise ValueError('Frozen parent reference changed')
            intervention = job['intervention']
            economic_run.branch_run(source_path, tick=tick, destination=path,
                                    intervention=None if intervention == economic_run.NO_INTERVENTION else intervention)
        elif self.preview and supplier_side(self.preview[0], job['config']) == 'archive':
            _, store = economic_run.create_run(job['config'], path, run_id=job['id'],
                                               response_archive=self.preview[1].model_dump(mode='json'))
            self.verify_plan(job, store)
        else:
            with publish_directory(path, create_staging=False) as staging:
                _, store = economic_run.create_run(job['config'], staging, run_id=job['id'])
                self.verify_plan(job, store)

    @staticmethod
    def verify_plan(job, store):
        identity = store.identity
        if (identity.config_hash != job['config_hash'] or identity.code_hash != job['source_hash']
                or identity.metadata['implementation']['dependency_hash'] != job['dependency_hash']
                or identity.metadata.get('parent') != job['parent']
                or identity.metadata.get('intervention') != job['intervention']):
            raise ValueError('The run archive differs from the frozen workbench plan')

    def boundary_stop(self, key):
        with self.lock:
            state = self.load(key)['status']
            return {'pausing': 'paused', 'cancelling': 'cancelled'}.get(state) or ('interrupted' if self.closing else None)

    def advance(self, key):
        try:
            job = self.load(key)
            path = self.run_path(key)
            if not path.exists():
                self.initialize(job, path)
            with run_lock(path):
                world, store, _ = economic_run.open_run(path)
                self.verify_plan(job, store)
                try:
                    store.update_index(verify_prefix=True)
                except sqlite3.DatabaseError:
                    store.repair_index()
                result = asyncio.run(economic_run.advance(world, store, until=job['until'],
                                                           stop_at_boundary=lambda: self.boundary_stop(key)))
            state, error = result['status'], None
        except Exception as exc:
            state, error = 'failed', f'{type(exc).__name__}: {exc}'
        with self.lock:
            job = self.load(key)
            if state in ('paused', 'checkpointed', 'interrupted') and job['status'] == 'cancelling':
                state = 'cancelled'
            job.update(status='paused' if state == 'checkpointed' else state, error=error)
            self.save(job)

    def inspect(self, key, tick, actor, *, include_actor_settlement=False):
        if self.choices:
            return self.choices.inspect(key, tick, actor)
        job = self.load(key)
        path = self.run_path(key)
        if not path.exists():
            return {'status': 'uncommitted', 'tick': tick}
        store = CommitStore(path)
        self.verify_plan(job, store)
        if type(tick) is not int or tick < 0:
            raise ValueError('Choose a committed period')
        records = store.markers()
        selected = next((marker for marker in records if marker.tick == tick), None)
        if selected is None:
            return {'status': 'uncommitted', 'tick': tick}
        joint = bool(store.identity.config.get('security_feedback'))
        if actor is not None and store.observation_archive == 'observe.v2' and selected.kind == 'tick':
            view = _indexed_agent_frame(store, records, selected, actor)
            if joint and include_actor_settlement:
                marker, snapshot = store.load_state_chain(tick)
                if marker != selected:
                    raise ValueError('Settlement commit differs from the selected observation')
                view['researcher_settlement'] = joint_actor_settlement(snapshot, actor)
            return {'status': 'available', **_inspection_document(store, 'agent', actor, [view])}
        marker, snapshot = store.load(tick)
        if self.preview and actor is None:
            view = {'cursor': _cursor(store, marker), 'actors': snapshot.actors, 'sections': [],
                    'supplier_preview': True, 'comparison_metrics': {}}
            if tick == PERIOD:
                arm, _ = summarize_supplier_arm(store, *self.preview)
                choice = arm['choice']
                view['comparison_metrics'] = {**choice, **{name: arm[name] for name in (
                    'attempts', 'successful_attempts', 'failed_attempts', 'fallbacks', 'external_api_calls')}}
                view['sections'] = [{'label':'授权输入与归档摘要', 'columns':['证据','值'], 'rows':[
                    ['授权观察散列', arm['authorized_observation_hash']],
                    ['响应来源', '固定合成响应' if arm['response_origin'] else '规则'],
                    ['响应归档散列', arm['archive_hash']]]}]
            return {'status': 'available', **_inspection_document(store, 'researcher', None, [view])}
        if actor is not None:
            if actor not in snapshot.actors:
                raise ValueError('Actor is not registered at this commit')
            if marker.observations is None:
                return {'status': 'unrecorded_observe', 'tick': tick}
        audience = 'agent' if actor is not None else 'researcher'
        view = _frame(store, marker, snapshot, audience, actor)
        if actor is not None and joint and include_actor_settlement:
            view['researcher_settlement'] = joint_actor_settlement(snapshot, actor)
        if actor is None:
            view['comparison_metrics'] = _metrics(snapshot, tick)
            view['actors'] = snapshot.actors
            design = job.get('intervention_design')
            if design:
                view['intervention_evolution'] = evolution(store, design['kind'], design['arm'], tick)
                if view['intervention_evolution']['cursor'] != view['cursor']:
                    raise ValueError('Intervention evidence differs from the selected commit')
                view['comparison_metrics'] = view['intervention_evolution']['metrics']
            if joint:
                view['joint_world'] = True
                view['relation_layers'] = joint_relation_layers(snapshot)
            elif SOCIAL_FIELD in snapshot.data:
                state = materialize_social(snapshot.data[SOCIAL_FIELD], tick)
                view['communication_edges'] = [[actor, peer] for actor, peers in state.outgoing_contacts().items()
                                               for peer in peers]
            view['attempts'] = [{'status': row['status'], 'tick': row.get('tick')}
                                for row in history(path) if row.get('tick') is not None and row['tick'] <= tick]
        return {'status': 'available', **_inspection_document(store, audience, actor, [view])}

    def timeline(self, key):
        if self.preview or self.choices:
            raise PermissionError('Timelines require a normal research workspace')
        job = self.load(key)
        store = CommitStore(self.run_path(key))
        self.verify_plan(job, store)
        return {'audience': 'researcher', 'authority': False, 'periods': joint_timeline(store)}

    def pilot_comparison(self):
        if self.joint_pilot is None:
            raise FileNotFoundError('No joint shock pilot archive was opened')
        try:
            return joint_pilot_comparison(self.joint_pilot)
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError('Invalid joint shock pilot archive metadata') from exc

    def research(self, key, tick, commit, page, *, build=False):
        if self.preview or self.choices:
            raise PermissionError('This preview only exposes the registered summary')
        job = self.load(key)
        root = self.run_path(key)
        self.verify_plan(job, CommitStore(root))
        index = self.research_indexes / (key + '-' + commit)
        if build:
            try:
                build_research_index(root, index, tick=tick, commit=commit)
            except FileExistsError:
                pass  # A concurrent request can publish the same immutable index.
        return research_page(root, index, tick=tick, commit=commit, page=page)

    def pair_export(self, left, right, tick):
        if self.choices:
            raise PermissionError('Use the frozen comparison download')
        if left == right:
            raise ValueError('Choose two distinct runs')
        if self.preview:
            if tick != PERIOD:
                raise ValueError('Choose the registered supplier comparison period')
            stores = []
            for key in (left, right):
                store = CommitStore(self.run_path(key))
                self.verify_plan(self.load(key), store)
                stores.append(store)
            return supplier_comparison(*self.preview, stores)
        arms = []
        for key in (left, right):
            job = self.load(key)
            plan = {field: job[field] for field in ('config', 'config_hash', 'source_hash', 'dependency_hash',
                                                   'terminal_tick', 'intervention')}
            plan['parent'] = ({field: value for field, value in job['parent'].items() if field != 'path'}
                              if job['parent'] else None)
            arms.append({'job': key, 'label': job['label'], 'plan': plan, 'report': self.inspect(key, tick, None)})
        if all(arm['report']['status'] != 'available' for arm in arms):
            raise FileNotFoundError('No export is available for either run at this period')
        first, second = (arm['plan']['parent'] for arm in arms)
        return {'schema_version': 1, 'audience': 'researcher', 'authority': False, 'tick': tick,
                'shared_parent_commit': first['commit_hash'] if first and first == second else None, 'arms': arms}

    def close(self):
        with self.lock:
            self.closing = True
        self.executor.shutdown(wait=True)
