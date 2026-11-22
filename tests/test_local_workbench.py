import asyncio
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
import uuid

from civitas.application import economic_run
from civitas.application.workbench import open_workspace
from civitas.application.workbench_http import WorkbenchServer
from civitas.core.world import WorldKernel
from civitas.experiments.labor_choice_preview import audit_labor_choice, run_labor_choice
from civitas.experiments.registry import history
from civitas.experiments.withdrawal_preview import audit_withdrawal_preview, run_withdrawal_preview
from civitas.experiments.herding_pair import audit_pair, freeze_pair, pack_pair
from civitas.experiments.supplier_preview import SETTINGS, audit_supplier_preview, freeze_supplier_preview
from civitas.storage.commit_store import CommitStore, atomic_json, read_json


CONFIG = {'households': 4, 'firms': 2, 'ticks': 5, 'archive_observations': True, 'fault_tick': None}


@contextmanager
def service(root, *, supplier_preview=None, choice_previews=(), withdrawal_template=None, labor_template=None,
            previews=None):
    with (
        open_workspace(root, CONFIG, supplier_preview=supplier_preview, choice_previews=choice_previews,
                       withdrawal_template=withdrawal_template, labor_template=labor_template) as workbench,
        WorkbenchServer(workbench, 0, previews=previews) as server,
    ):
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def request(server, path, body=None, *, token=None, headers=None):
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=20)
    headers = {'Authorization': 'Bearer ' + (server.researcher if token is None else token), **(headers or {})}
    try:
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection.request('GET' if body is None else 'POST', path, headers=headers,
                           body=None if body is None else json.dumps(body))
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def command(action, **params):
    return {'request_id': uuid.uuid4().hex, 'action': action, **params}


def wait_state(server, key, expected):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        _, status = request(server, '/api/status')
        row = next(row for row in status['jobs'] if row['id'] == key)
        if row['status'] == expected:
            return row
        if row['status'] == 'failed':
            raise AssertionError(row)
        time.sleep(.025)
    raise AssertionError(f'{key} did not reach {expected}')


WITHDRAWAL = Path(__file__).resolve().parents[1]/'examples'/'closed-economy-bank-notice.json'
REGISTRATION = '/api/withdrawal-registration'
WITHDRAWAL_DOWNLOAD = '/api/choice-download?kind=withdrawal&tick=5'


def wait_registration(server, expected, *, kind='withdrawal'):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        code, status = request(server, '/api/status')
        if code != 200:
            raise AssertionError(status)
        row = status[f'{kind}_registration']
        if row['phase'] == expected:
            return row
        if row['phase'] in ('completed', 'failed', 'interrupted'):
            raise AssertionError(row)
        time.sleep(.025)
    raise AssertionError(f'Registration did not reach {expected}')


class LocalWorkbenchTests(unittest.TestCase):


    def test_research_preview_capability_stays_in_its_workspace(self):
        template = read_json(WITHDRAWAL.with_name('closed-economy-labor-choice-preview.json'))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_workspace(root/'labor', CONFIG, labor_template=template) as labor, service(
                root/'joint', previews={'/labor': labor},
            ) as server:
                code, status = request(server, '/labor/api/status')
                self.assertEqual(code, 200)
                self.assertTrue(status['research_preview'])
                self.assertIn('labor_registration', status)
                self.assertNotIn('labor_registration', request(server, '/api/status')[1])
                _, result = request(server, '/api/control', command('freeze', label='联合世界', config=CONFIG))
                key = result['job']
                request(server, '/api/control', command('start', job=key, until=1))
                wait_state(server, key, 'paused')
                _, grant = request(server, '/api/grant', {'job':key,'actor':'household:0000'})
                token = grant['url'].split('#')[1]
                code, own = request(server, '/api/status', token=token)
                self.assertEqual(code, 200)
                self.assertNotIn('research_preview', own)
                self.assertEqual(request(server, '/labor/api/status', token=token)[0], 403)
                self.assertEqual(request(server, '/labor/api/labor-registration',
                    {'action':'register','expected_hash':status['labor_registration']['template_hash']},
                    token=token)[0], 403)

    def test_labor_registration_audits_both_wage_endpoints_and_preserves_failure(self):
        template = read_json(WITHDRAWAL.with_name('closed-economy-labor-choice-preview.json'))
        action = '/api/labor-registration'
        download = '/api/choice-download?kind=labor&tick=8'
        for outcome in ('completed', 'failed'):
            with self.subTest(outcome=outcome), TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                pair = workspace/'labor-pair'
                with service(workspace, labor_template=template) as server:
                    review = wait_registration(server, 'empty', kind='labor')
                    self.assertEqual(review['proposal']['offer_tick'], 4)
                    register = {'action':'register','expected_hash':review['template_hash']}
                    self.assertEqual(request(server, action, register)[0], 200)
                    frozen = wait_registration(server, 'ready', kind='labor')
                    self.assertEqual(frozen['denominators']['started_arms'], 0)
                    self.assertEqual(request(server, download)[0], 404)
                    response = pair/'response-archive.json'
                    if outcome == 'failed':
                        response.rename(pair/'test-held.json')
                    start = {'action':'start','expected_hash':frozen['plan_hash']}
                    self.assertEqual(request(server, action, start)[0], 200)
                    done = wait_registration(server, outcome, kind='labor')
                    if outcome == 'failed':
                        self.assertEqual(done['denominators']['incomplete_arms'], 2)
                        self.assertEqual(done['denominators']['started_arms'], 0)
                        self.assertEqual(request(server, download)[0], 404)
                        continue
                    report = audit_labor_choice(pair)
                    self.assertEqual(request(server, download), (200, report))
                    for side, contract, total in (('rule',24,24),('archive',0,48)):
                        arm = report['arms'][side]
                        self.assertEqual((arm['paid_contract_wages_minor'],arm['total_seat_wages_after_offer_minor']),
                                         (contract,total))
                        self.assertEqual(sum(row['status']=='running' for row in history(pair/side)), 1)
                    _, grant = request(server, '/api/choice-grant', {'kind':'labor','side':'archive'})
                    token = grant['url'].split('#')[1]
                    observer = request(server, '/api/status', token=token)[1]
                    self.assertNotIn('labor_registration', observer)
                    self.assertEqual(request(server, download, token=token)[0], 403)
                    self.assertEqual(request(server, action, start, token=token)[0], 403)

    def test_register_and_start_withdrawal_only_once_then_reopen(self):
        config = read_json(WITHDRAWAL)
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pair = workspace/'withdrawal-pair'
            with service(workspace, withdrawal_template=config) as server:
                review = wait_registration(server, 'empty')
                proposal = review['proposal']
                self.assertEqual((proposal['root_seed'], proposal['notice_tick'], proposal['final_tick']), (7, 3, 5))
                register = {'action':'register', 'expected_hash':review['template_hash']}
                self.assertEqual(request(server, REGISTRATION, register, token='')[0], 403)
                self.assertEqual(request(server, REGISTRATION, {**register,'expected_hash':'incorrect'})[0], 400)
                self.assertEqual(request(server, REGISTRATION, {'action':'start','expected_hash':None})[0], 400)
                self.assertEqual(request(server, WITHDRAWAL_DOWNLOAD)[0], 404)
                self.assertEqual(request(server, '/api/control', command('freeze', label='blocked', config=CONFIG))[0], 403)
                self.assertEqual(request(server, REGISTRATION, register)[0], 200)
                self.assertEqual(request(server, REGISTRATION, register)[0], 200)
                frozen = wait_registration(server, 'ready')
                self.assertEqual(frozen['denominators'], {'assigned_arms':2,'started_arms':0,
                    'completed_arms':0,'failed_arms':0,'incomplete_arms':2})
                self.assertFalse((pair/'rule').exists())
                self.assertFalse((pair/'archive').exists())
                self.assertEqual(read_json(pair/'plan.json')['plan']['arms'], proposal['arms'])
                start = {'action':'start', 'expected_hash':frozen['plan_hash']}
                self.assertEqual(request(server, REGISTRATION, {**start,'expected_hash':review['template_hash']})[0], 400)
                self.assertEqual(request(server, REGISTRATION, start)[1]['phase'], 'running')
                self.assertEqual(request(server, REGISTRATION, start)[0], 200)
                done = wait_registration(server, 'completed')
                self.assertEqual(done['denominators'], {'assigned_arms':2,'started_arms':2,
                    'completed_arms':2,'failed_arms':0,'incomplete_arms':0})
                report = audit_withdrawal_preview(pair)
                self.assertEqual(report['difference']['paid_minor'], 0)
                for side, arm in report['arms'].items():
                    self.assertEqual(arm['paid_minor'], 3)
                    self.assertEqual(done['arms'][side]['commit_hash'], arm['commits'][-1]['source_commit_hash'])
                    self.assertEqual(sum(row['status']=='running' for row in history(pair/side)),1)
                self.assertEqual(request(server, WITHDRAWAL_DOWNLOAD), (200,report))
                _, grant = request(server, '/api/choice-grant', {'kind':'withdrawal','side':'archive'})
                observer = grant['url'].split('#')[1]
                _, data = request(server, '/api/status', token=observer)
                self.assertNotIn('withdrawal_registration', data)
                self.assertEqual(request(server, WITHDRAWAL_DOWNLOAD, token=observer)[0], 403)
                self.assertEqual(request(server, REGISTRATION, start, token=observer)[0], 403)
                self.assertEqual(request(server, '/api/download?job='+data['jobs'][0]['id']+'&tick=5')[0], 403)
            recorded = (workspace/'withdrawal-registration.json').read_bytes()
            with service(workspace, withdrawal_template=config) as server:
                self.assertEqual(request(server, REGISTRATION, start)[1]['phase'], 'completed')
                self.assertEqual(request(server, WITHDRAWAL_DOWNLOAD), (200, report))
            self.assertEqual(recorded, (workspace/'withdrawal-registration.json').read_bytes())
            with self.assertRaisesRegex(ValueError, 'original template'):
                with open_workspace(workspace, CONFIG, withdrawal_template={**config,'root_seed':8}):
                    pass

    def test_withdrawal_failure_and_interruption_preserve_assigned_arms(self):
        config = read_json(WITHDRAWAL)
        run_new = economic_run.run_new
        for mode in ('failed', 'interrupted', 'incomplete'):
            with self.subTest(mode=mode), TemporaryDirectory() as tmp:
                workspace = Path(tmp)

                async def fail_archive(config, path, **kwargs):
                    if path.name == 'archive':
                        if mode == 'incomplete':
                            return await run_new(config, path, until=4, **kwargs)
                        raise OSError('private-failure-sentinel')
                    return await run_new(config, path, **kwargs)

                with service(workspace, withdrawal_template=config) as server:
                    review = wait_registration(server, 'empty')
                    request(server, REGISTRATION, {'action':'register','expected_hash':review['template_hash']})
                    frozen = wait_registration(server, 'ready')
                    start = {'action':'start','expected_hash':frozen['plan_hash']}
                    if mode != 'interrupted':
                        with patch.object(economic_run, 'run_new', side_effect=fail_archive):
                            self.assertEqual(request(server, REGISTRATION, start)[0], 200)
                            status = wait_registration(server, 'failed')
                        self.assertEqual(status['denominators'], {'assigned_arms':2,'started_arms':2,
                            'completed_arms':1,'failed_arms':int(mode=='failed'),'incomplete_arms':1})
                        self.assertNotIn('private-failure-sentinel', json.dumps(status))
                        self.assertEqual(request(server, WITHDRAWAL_DOWNLOAD)[0], 404)
                record_path = workspace/'withdrawal-registration.json'
                if mode == 'interrupted':
                    atomic_json(record_path, {**read_json(record_path), 'phase':'running',
                                              'arms':{'rule':'running','archive':'not_started'}})
                with service(workspace, withdrawal_template=config) as server:
                    self.assertEqual(request(server, REGISTRATION, start)[1]['phase'],
                                     'interrupted' if mode == 'interrupted' else 'failed')
                    self.assertEqual(request(server, WITHDRAWAL_DOWNLOAD)[0], 404)
                    if mode == 'interrupted':
                        status = wait_registration(server, 'interrupted')
                        self.assertEqual(status['denominators'], {'assigned_arms':2,'started_arms':1,
                            'completed_arms':0,'failed_arms':0,'incomplete_arms':2})
                        self.assertEqual(status['arms']['rule']['state'], 'interrupted')
                        self.assertFalse((workspace/'withdrawal-pair'/'rule').exists())

    def test_frozen_choice_comparisons_reuse_audit_and_observer_authorization(self):
        examples = Path(__file__).resolve().parents[1]/'examples'
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            withdrawal = asyncio.run(run_withdrawal_preview(
                read_json(examples/'closed-economy-bank-notice.json'), root/'withdrawal'))
            labor = asyncio.run(run_labor_choice(
                read_json(examples/'closed-economy-labor-choice-preview.json'), 3, root/'labor'))
            pairs = (root/'withdrawal', root/'labor')
            with service(root/'workbench',choice_previews=pairs) as server:
                status, catalog = request(server,'/api/status')
                self.assertEqual(status,200)
                self.assertEqual([row['kind'] for row in catalog['choice_previews']],['withdrawal','labor'])
                for kind,tick,report in [('withdrawal',5,withdrawal),('labor',6,labor)]:
                    query = f'kind={kind}&tick={tick}'
                    self.assertEqual(request(server,f'/api/choice-view?{query}'),(
                        200,{'kind':kind,'tick':tick,'report':report}))
                    self.assertEqual(request(server,f'/api/choice-download?{query}'),(200,report))
                self.assertEqual(request(server,'/api/choice-download?kind=withdrawal&tick=8')[0],400)
                self.assertEqual(request(server,'/api/choice-view?kind=other&tick=4')[0],404)
                self.assertEqual(request(server,'/api/control',command('freeze',label='blocked',config=CONFIG))[0],403)

                _, recipient = request(server,'/api/choice-grant',{'kind':'withdrawal','side':'archive'})
                _, other = request(server,'/api/choice-grant',{'kind':'labor','side':'archive'})
                token = recipient['url'].split('#')[1]
                other_token = other['url'].split('#')[1]
                _, observer = request(server,'/api/status',token=token)
                _, other_observer = request(server,'/api/status',token=other_token)
                self.assertNotIn('choice_previews',observer)
                key = observer['jobs'][0]['id']
                own = f'/api/view?job={key}&tick=3'
                code, observation = request(server,own,token=token)
                self.assertEqual((code,observation['actor_id']),(200,'household:0001'))
                self.assertIn('verified_withdrawal_delay',json.dumps(observation))
                self.assertEqual(request(server,own+'&actor=household:0000',token=token)[0],403)
                self.assertEqual(request(server,own,token=other_token)[0],403)
                self.assertEqual(request(server,own,token='')[0],403)
                self.assertEqual(request(server,own)[0],403)
                self.assertEqual(request(server,f'/api/download?job={key}&tick=3',token=token)[0],403)
                for route in ('choice-view','choice-download'):
                    self.assertEqual(request(server,f'/api/{route}?kind=labor&tick=4',token=token)[0],403)
                self.assertEqual(other_observer['actor'],'household:0003')
            tampered = {**withdrawal,'status':'tampered'}
            atomic_json(pairs[0]/'shareable-comparison.json',tampered)
            with self.assertRaisesRegex(ValueError,'independent audit'):
                with open_workspace(root/'unverified',CONFIG,choice_previews=pairs):
                    pass

    def test_registered_supplier_preview_exports_only_recomputable_summary(self):
        config = read_json(Path(__file__).resolve().parents[1]/'examples/closed-economy-joint-cognition.json')
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            author = root/'synthetic'
            asyncio.run(economic_run.run_new({**config, 'supplier_cognition': {
                **SETTINGS, 'source': 'synthetic_test'}}, author, until=3))
            row, = CommitStore(author).archived_records()
            tape = {'schema_version': 1, 'records': [{
                'request': row['request'], 'response': {**row['response'], 'ok': False, 'text': '', 'error_code': 'refusal'}}]}
            registration = root/'registration'
            freeze_supplier_preview(config, tape, registration)
            workspace = root/'workbench'
            with service(workspace, supplier_preview=registration) as server:
                changed = {**config, 'root_seed':config['root_seed'] + 1}
                self.assertEqual(request(server, '/api/validate', changed)[0], 400)
                jobs = []
                for side in ('rule', 'archive'):
                    _, run = request(server, '/api/control', command(
                        'freeze', label='private-label-sentinel', config=read_json(registration/f'{side}.json')))
                    jobs.append(run['job'])
                    self.assertEqual(request(server, '/api/control', command('start',job=run['job'],until=2))[0], 200)
                    wait_state(server, run['job'], 'paused')
                path = f'/api/pair-download?job={jobs[0]}&compare={jobs[1]}&tick='
                status, partial = request(server, path+'3')
                self.assertEqual(status, 200)
                self.assertEqual(partial['status'], 'incomplete')
                self.assertEqual(partial['denominators']['paired_opportunities'], 0)
                self.assertIsNone(partial['difference'])
                for key in jobs:
                    self.assertEqual(request(server, '/api/control', command('resume',job=key,until=None))[0], 200)
                    wait_state(server, key, 'completed')
                self.assertEqual(request(server, path+'2')[0], 400)
                _, grant = request(server,'/api/grant',{'job':jobs[1], 'actor':'household:0000'})
                scope = grant['url'].split('#')[1]
                own_view = f'/api/view?job={jobs[1]}&tick=3'
                self.assertEqual(request(server, own_view, token=scope)[1]['actor_id'], 'household:0000')
                self.assertEqual(request(server, own_view+'&actor=household:0001',token=scope)[0], 403)
                self.assertEqual(request(server, path+'3', token=scope)[0], 403)
                self.assertEqual(request(server, f'/api/download?job={jobs[1]}&tick=3')[0], 403)
                status, export = request(server, path+'3')
                self.assertEqual(status, 200)
                self.assertEqual(export['status'], 'completed')
                self.assertEqual(export['denominators']['rule_fallbacks'], 1)
                self.assertEqual(export['denominators']['failed_model_attempts'], 1)
                self.assertEqual(export['denominators']['model_attempts'], 1)
                self.assertFalse(export['difference']['supplier_changed'])
                self.assertEqual(export['arms']['archive']['choice']['buyer_debit_minor'], 16)
                self.assertEqual(len(export['arms']['archive']['commits']), 6)
                wire = json.dumps(export)
                for token in ('private-label-sentinel', 'household:', 'firm:', 'Bearer ', '"memory"', '"text"', '"request"'):
                    self.assertNotIn(token, wire)
                self.assertEqual(request(server, own_view)[1]['views'][0]['comparison_metrics']['failed_attempts'], 1)
            with self.assertRaisesRegex(ValueError, 'workspace dedicated'):
                with open_workspace(workspace, CONFIG):
                    pass
            output = root/'export.json'
            atomic_json(output, export)
            self.assertEqual(audit_supplier_preview(registration, output, workspace), export)
            export['arms']['archive']['commits'][3]['state_hash'] = '0'*64
            atomic_json(output, export)
            with self.assertRaisesRegex(ValueError, 'independent purchase audit'):
                audit_supplier_preview(registration, output, workspace)
    def test_herding_pair_binds_researcher_download_and_archives(self):
        with TemporaryDirectory() as tmp, service(tmp) as server:
            root = Path(tmp)
            registered = root/'registered'
            freeze_pair({**CONFIG, 'root_seed':410001, 'securities':{'shock_tick':2}}, registered)
            jobs = {}
            original = WorldKernel.step

            async def step(world):
                if not world.scenario.settings.herding and world.snapshot.tick == 2:
                    raise OSError('injected failure after first shock-period commit')
                return await original(world)

            with patch.object(WorldKernel, 'step', step):
                for side in ('on', 'off'):
                    _, run = request(server, '/api/control', command(
                        'freeze', label=side, config=read_json(registered/f'{side}.json')))
                    jobs[side] = run['job']
                    self.assertEqual(request(server, '/api/control', command(
                        'start', job=run['job'], until=None))[0], 200)
                    wait_state(server, run['job'], 'failed' if side == 'off' else 'completed')
            query = f"/api/pair-download?job={jobs['on']}&compare={jobs['off']}&tick="
            download_path = root/'download.json'
            response, incomplete = request(server, query+'5')
            self.assertEqual(response, 200)
            atomic_json(download_path, incomplete)
            finished = pack_pair(registered/'plan.json', download_path, root, root/'incomplete')
            self.assertEqual(finished['status'], 'failed')
            self.assertEqual(finished['denominators']['completed_worlds'], 1)
            self.assertEqual(finished['denominators']['incomplete_worlds'], 1)
            self.assertEqual(finished['denominators']['worlds_with_terminal_execution_failure'], 1)
            self.assertEqual(finished, audit_pair(root/'incomplete'))

            self.assertEqual(request(server, '/api/control', command('resume',job=jobs['off'],until=None))[0], 200)
            wait_state(server, jobs['off'], 'completed')
            status, grant = request(server, '/api/grant', {'job':jobs['on'], 'actor':'investor:0000'})
            self.assertEqual(status, 200)
            scope = grant['url'].split('#')[1]
            self.assertEqual(request(server, query+'5', token=scope)[0], 403)
            response, earlier = request(server, query+'4')
            self.assertEqual(response, 200)
            atomic_json(download_path, earlier)
            with self.assertRaisesRegex(ValueError, 'registered final tick'):
                pack_pair(registered/'plan.json', download_path, root, root/'wrong-period')
            response, complete = request(server, query+'5')
            self.assertEqual(response, 200)
            atomic_json(download_path, complete)
            bundle = root/'bundle'
            result = pack_pair(registered/'plan.json', download_path, root, bundle)
            self.assertEqual(result['status'], 'completed')
            self.assertTrue(result['manipulation_passed'])
            self.assertEqual(result['denominators']['failed_attempts'], 1)
            self.assertEqual(result['denominators']['worlds_with_terminal_execution_failure'], 0)
            self.assertEqual(result['denominators']['matched_signal_opportunities'], 16)
            self.assertGreater(result['different_sell_limits'], 0)
            self.assertEqual(result['arms']['off']['nonzero_applications'], 0)
            self.assertEqual(result, audit_pair(bundle))

            input_path = bundle/'comparison.json'
            view = complete['arms'][0]['report']['views'][0]
            view['cursor']['state_hash'] = '0'*64
            atomic_json(input_path, complete)
            with self.assertRaisesRegex(ValueError, 'Downloaded cursor'):
                audit_pair(bundle)
            atomic_json(input_path, read_json(download_path))
            store = CommitStore(bundle/'off')
            observation = store.segment_path(store.markers()[-1].observations)
            observation.rename(observation.with_suffix('.missing'))
            with self.assertRaisesRegex(ValueError, 'Missing referenced segment'):
                audit_pair(bundle)

    def test_herding_pair_does_not_pass_when_all_delivered_signals_are_neutral(self):
        with TemporaryDirectory() as tmp, service(tmp) as server:
            root = Path(tmp)
            registered = root/'registered'
            freeze_pair({**CONFIG, 'households':2, 'network_neighbors':1, 'ticks':3, 'root_seed':57,
                         'securities':{'shock_tick':2, 'selective_exposure':False}}, registered)
            jobs = []
            for side in ('on', 'off'):
                _, run = request(server, '/api/control', command(
                    'freeze',label=side,config=read_json(registered/f'{side}.json')))
                jobs.append(run['job'])
                request(server, '/api/control', command('start',job=run['job'],until=None))
                wait_state(server, run['job'], 'completed')
            _, download = request(server, f'/api/pair-download?job={jobs[0]}&compare={jobs[1]}&tick=3')
            atomic_json(root/'download.json', download)
            result = pack_pair(registered/'plan.json', root/'download.json', root, root/'bundle')
            self.assertEqual(result['denominators']['completed_worlds'], 2)
            self.assertEqual(result['denominators']['matched_signal_opportunities'], 4)
            self.assertEqual(result['different_sell_limits'], 0)
            self.assertFalse(result['manipulation_passed'])
            self.assertIsNone(result['drawdown_on_minus_off'])
            self.assertEqual(result['status'], 'failed')

    def test_browser_research_pages_preserve_large_account_amounts(self):
        amount = (1 << 53) + 10001
        with TemporaryDirectory() as tmp, service(tmp) as server:
            _, job = request(server, '/api/control', command(
                'freeze', label='大额余额', config={**CONFIG, 'initial_firm_cash_minor': amount}))
            key = job['job']
            self.assertEqual(request(server, '/api/control', command('start',job=key,until=1))[0], 200)
            wait_state(server,key,'paused')
            marker = CommitStore(Path(tmp)/'runs'/key).markers()[0]
            cursor = {'job': key, 'tick': 0, 'commit': marker.commit_hash}
            built = request(server, '/api/research-index', cursor)
            queried = request(server, f'/api/research-page?job={key}&tick=0&commit={marker.commit_hash}&page=1')
            self.assertEqual(built, queried)
            self.assertEqual(built[0], 200)
            firm, = [row for row in built[1]['accounts']['rows'] if row['account_id']=='firm:00']
            self.assertEqual(firm['cash_minor'], str(amount))

    def test_browser_routes_are_scoped_and_control_retries_reuse_the_receipt(self):
        with TemporaryDirectory() as tmp, service(tmp) as server:
            self.assertEqual(request(server, '/api/status', token='')[0], 403)
            self.assertEqual(request(server, '/api/status', headers={'Origin': 'https://example.test'})[0], 403)
            self.assertEqual(request(server, '/api/status', headers={'Host': 'example.test'})[0], 403)
            self.assertEqual(request(server, '/api/validate', {**CONFIG, 'households': 0})[0], 400)
            freeze = command('freeze', label='父世界', config={**CONFIG, 'observation_archive_format': 'observe.v2'})
            first = request(server, '/api/control', freeze)
            self.assertEqual(first, request(server, '/api/control', freeze))
            self.assertEqual(request(server, '/api/control', {**freeze, 'label':'改名'})[0], 409)
            key = first[1]['job']
            self.assertEqual(request(server,'/api/view?job=../../outside&tick=0')[0], 400)
            start = command('start', job=key, until=1)
            self.assertEqual(request(server, '/api/control', start), request(server, '/api/control', start))
            head = wait_state(server,key,'paused')
            self.assertEqual(head['tick'], 1)
            status, grant = request(server,'/api/grant',{'job':key,'actor':'household:0000'})
            self.assertEqual(status, 200)
            scope = grant['url'].split('#')[1]
            _, agent = request(server,f'/api/view?job={key}&tick=1',token=scope)
            self.assertEqual(agent['actor_id'], 'household:0000')
            self.assertEqual(agent['views'][0]['verification_scope'], 'indexed_observation_cursor')
            self.assertNotIn('comparison_metrics', agent['views'][0])
            self.assertEqual(request(server, f'/api/download?job={key}&tick=1', token=scope)[1], agent)
            self.assertEqual(request(server,f'/api/view?job={key}&tick=0',token=scope)[1]['status'], 'unrecorded_observe')
            self.assertEqual(request(server,f'/api/view?job={key}&tick=2',token=scope)[1]['status'], 'uncommitted')
            self.assertEqual(request(server,f'/api/view?job={key}&tick=1&actor=household:0001',token=scope)[0], 403)
            self.assertEqual(request(server,f'/api/view?job={uuid.uuid4().hex}&tick=1',token=scope)[0], 403)
            self.assertEqual(request(server,f'/api/pair-download?job={key}&compare={key}&tick=1',token=scope)[0], 403)
            commit = head['commit_hash']
            indexed = {'job': key, 'tick': 1, 'commit': commit}
            route = f'/api/research-page?job={key}&tick=1&commit={commit}&page=1'
            self.assertEqual(request(server, '/api/research-index', indexed, token=scope)[0], 403)
            self.assertEqual(request(server, route, token=scope)[0], 403)
            self.assertEqual(request(server, route)[0], 404)
            built = request(server, '/api/research-index', indexed)
            self.assertEqual(built[0], 200)
            self.assertEqual(built, request(server, route))
            self.assertEqual(built, request(server, '/api/research-index', indexed))
            self.assertEqual(built[1]['cursor']['source_commit_hash'], commit)
            for action in ('resume','pause','cancel'):
                self.assertEqual(request(server,'/api/control',command(action,job=key),token=scope)[0], 403)
            self.assertEqual(request(server,'/api/grant',{'job':key,'actor':'household:0000'},token=scope)[0], 403)
            ledger = server.workbenches[''].load(key)
            self.assertEqual(len(ledger['requests']), 2)

    def test_pause_cancel_and_restart_preserve_committed_boundaries(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            next_commit, release = Event(), Event()
            original = WorldKernel.step

            async def step(world):
                await original(world)
                if world.snapshot.tick <= 2:
                    next_commit.set()
                    if not await asyncio.to_thread(release.wait, 10):
                        raise RuntimeError('Test did not release the period boundary')

            with service(root) as server, patch.object(WorldKernel, 'step', step):
                _, first = request(server,'/api/control',command('freeze',label='故障复现',config=CONFIG))
                key = first['job']
                request(server,'/api/control',command('start',job=key,until=None))
                self.assertTrue(next_commit.wait(10))
                request(server,'/api/control',command('pause',job=key))
                release.set()
                head = wait_state(server,key,'paused')
                self.assertEqual(head['tick'],1)
                request(server,'/api/control',command('resume',job=key,until=1))
                self.assertEqual(wait_state(server,key,'paused')['commit_hash'],head['commit_hash'])
                next_commit.clear()
                release.clear()
                request(server,'/api/control',command('resume',job=key,until=None))
                self.assertTrue(next_commit.wait(10))
                request(server,'/api/control',command('cancel',job=key))
                release.set()
                self.assertEqual(wait_state(server,key,'cancelled')['tick'],2)
                self.assertEqual(request(server,'/api/control',command('resume',job=key,until=None))[0],409)
                _, interrupted = request(server,'/api/control',command('freeze',label='中断',config=CONFIG))
                interrupted = interrupted['job']
                request(server,'/api/control',command('start',job=interrupted,until=1))
                interrupted_head = wait_state(server,interrupted,'paused')
                stale = server.workbenches[''].load(interrupted)
            stale['status'] = 'running'
            atomic_json(root/'jobs'/(interrupted+'.json'),stale)
            with service(root) as resumed:
                self.assertEqual(wait_state(resumed,interrupted,'interrupted')['commit_hash'],interrupted_head['commit_hash'])
                request(resumed,'/api/control',command('resume',job=interrupted,until=None))
                self.assertEqual(wait_state(resumed,interrupted,'completed')['tick'],5)
                receipt = CommitStore(root/'runs'/interrupted).markers()
                self.assertEqual([marker.tick for marker in receipt], list(range(6)))
