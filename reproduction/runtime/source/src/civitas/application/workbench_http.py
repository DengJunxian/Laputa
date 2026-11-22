"""Loopback-only research workbench with independent observer capabilities."""
from contextlib import ExitStack
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from pathlib import Path
import secrets
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from core.experiments.strict_types import strict_json_loads
from civitas.application.inspection_html import _browser_integers
from civitas.application.workbench import ControlConflict, job_key, open_workspace
from civitas.contracts.base import content_hash
from civitas.storage.commit_store import CommitStore, json_bytes


ASSETS = Path(__file__).with_name('workbench_assets')
PAGES = {'/': ('index.html', 'text/html'), '/observe': ('index.html', 'text/html'),
         '/workbench.js': ('workbench.js', 'text/javascript'), '/workbench.css': ('workbench.css', 'text/css')}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class WorkbenchServer(ThreadingHTTPServer):
    def __init__(self, workbench, port, *, previews=None):
        self.workbenches = {'': workbench, **(previews or {})}
        self.researcher = secrets.token_urlsafe(32)
        self.observers = {}
        self.grants_lock = Lock()
        super().__init__(('127.0.0.1', port), WorkbenchHandler)
        self.host_header = f'127.0.0.1:{self.server_port}'
        self.origin = 'http://' + self.host_header

    def authorize(self, authorization, prefix):
        if not authorization.startswith('Bearer '):
            raise PermissionError('A local workbench capability is required')
        token = authorization.removeprefix('Bearer ')
        if hmac.compare_digest(token.encode(), self.researcher.encode()):
            return None
        with self.grants_lock:
            grant = self.observers.get(token)
            if grant is None or grant[0] != prefix:
                raise PermissionError('This capability does not grant access to this workspace')
            return grant[1:]

    def grant(self, raw, prefix):
        if not isinstance(raw, dict) or set(raw) != {'job', 'actor'}:
            raise ValueError('Select one run and actor')
        key = job_key(raw['job'])
        workbench = self.workbenches[prefix]
        if workbench.choices:
            if raw['actor'] != workbench.choices.arm(key)['actor']:
                raise PermissionError('Only the registered actor can be granted in this comparison')
        else:
            workbench.load(key)
        _, snapshot = CommitStore(workbench.run_path(key)).load()
        if not isinstance(raw['actor'], str) or raw['actor'] not in snapshot.actors:
            raise ValueError('Actor is not registered in this run')
        token = secrets.token_urlsafe(32)
        with self.grants_lock:
            self.observers[token] = prefix, key, raw['actor']
        return {'url': f'{self.origin}{prefix}/observe#{token}'}


class WorkbenchHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Browser capabilities are never written to access logs.
        pass

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        try:
            body, media, filename = self.route(method)
            status = HTTPStatus.OK
        except PermissionError as exc:
            status, body, media, filename = HTTPStatus.FORBIDDEN, json_bytes({'error': str(exc)}), 'application/json', None
        except ControlConflict as exc:
            status, body, media, filename = HTTPStatus.CONFLICT, json_bytes({'error': str(exc)}), 'application/json', None
        except FileNotFoundError as exc:
            status, body, media, filename = HTTPStatus.NOT_FOUND, json_bytes({'error': str(exc)}), 'application/json', None
        except ValueError as exc:
            status, body, media, filename = HTTPStatus.BAD_REQUEST, json_bytes({'error': str(exc)}), 'application/json', None
        except OSError:
            status, body, media, filename = (HTTPStatus.INTERNAL_SERVER_ERROR,
                json_bytes({'error': 'Unable to read or write the local workspace'}), 'application/json', None)
        self.send_response(status)
        self.send_header('Content-Type', media + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', CSP)
        if filename is not None:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def route(self, method):
        server = self.server
        if (self.headers.get('Host') != server.host_header
                or self.headers.get('Origin', server.origin) != server.origin):
            raise PermissionError('Use the local workbench origin')
        url = urlsplit(self.path)
        prefix = next((key for key in server.workbenches if key and url.path.startswith(key+'/')), '')
        workbench = server.workbenches[prefix]
        route = url.path.removeprefix(prefix)
        if method == 'GET' and route in PAGES:
            name, media = PAGES[route]
            return (ASSETS/name).read_bytes(), media, None
        scope = server.authorize(self.headers.get('Authorization', ''), prefix)
        registration = workbench.registration
        if method == 'POST':
            if scope is not None:
                raise PermissionError('This observer capability is read-only')
            raw = self.json_body()
            if registration and route == f'/api/{registration.kind}-registration':
                data = registration.command(raw)
                return json_bytes(data), 'application/json', None
            if workbench.choices:
                if route != '/api/choice-grant':
                    raise PermissionError('Frozen comparisons are read-only')
                if not isinstance(raw, dict) or set(raw) != {'kind', 'side'} or not all(
                        isinstance(value, str) for value in raw.values()):
                    raise ValueError('Choose a frozen comparison and arm')
                grant = server.grant(workbench.choices.grant_scope(raw['kind'], raw['side']), prefix)
                return json_bytes(grant), 'application/json', None
            if registration:
                raise PermissionError('This comparison workspace only accepts registered comparison commands')
            if route == '/api/validate':
                config = workbench.config(raw)
                data = {'config_hash': content_hash(config.model_dump(mode='json')), 'ticks': config.ticks}
            elif route == '/api/control':
                data = workbench.command(raw)
            elif route == '/api/grant':
                data = server.grant(raw, prefix)
            elif route == '/api/research-index':
                if not isinstance(raw, dict) or set(raw) != {'job', 'tick', 'commit'} or not isinstance(raw['commit'], str):
                    raise ValueError('Select a run, period and its full commit hash')
                data = workbench.research(job_key(raw['job']), raw['tick'], raw['commit'], 1, build=True)
                data = _browser_integers(data)
            else:
                raise FileNotFoundError('Unknown workbench route')
            return json_bytes(data), 'application/json', None
        if route == '/api/status':
            data = workbench.status(scope)
            if scope is None and len(server.workbenches) > 1:
                data['research_preview'] = True
            return json_bytes(_browser_integers(data)), 'application/json', None
        if route in ('/api/choice-view', '/api/choice-download'):
            if scope is not None:
                raise PermissionError('Frozen comparisons require researcher access')
            choices = workbench.choices
            if choices is None:
                raise FileNotFoundError('No frozen choice comparisons are open')
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=2)
            if set(query) != {'kind','tick'} or any(len(rows) != 1 for rows in query.values()):
                raise ValueError('Choose a frozen comparison and period')
            view = choices.view(query['kind'][0], int(query['tick'][0]))
            if route == '/api/choice-download':
                return json_bytes(view['report']), 'application/json', 'ecosim-'+view['kind']+'-comparison.json'
            return json_bytes(_browser_integers(view)), 'application/json', None
        if registration and route != '/api/view':
            raise PermissionError('Use the audited comparison routes in this comparison workspace')
        if route == '/api/research-page':
            if scope is not None:
                raise PermissionError('Research account pages require researcher access')
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=4)
            if set(query) != {'job', 'tick', 'commit', 'page'} or any(len(values) != 1 for values in query.values()):
                raise ValueError('Select a run, period, commit and page')
            data = workbench.research(job_key(query['job'][0]), int(query['tick'][0]),
                                             query['commit'][0], int(query['page'][0]))
            return json_bytes(_browser_integers(data)), 'application/json', None
        if route == '/api/pair-download':
            if scope is not None:
                raise PermissionError('Paired research exports require researcher access')
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=3)
            if set(query) != {'job', 'compare', 'tick'} or any(len(values) != 1 for values in query.values()):
                raise ValueError('Choose two runs and one period')
            tick = int(query['tick'][0])
            data = workbench.pair_export(job_key(query['job'][0]), job_key(query['compare'][0]), tick)
            return json_bytes(data), 'application/json', f'ecosim-pair-{tick}.json'
        if route == '/api/download' and (workbench.preview or workbench.choices):
            label = 'supplier ' if workbench.preview else ''
            raise PermissionError(f'Only the shareable {label}comparison can be downloaded in this preview')
        if route not in ('/api/view', '/api/download'):
            raise FileNotFoundError('Unknown workbench route')
        query = parse_qs(url.query, keep_blank_values=True, max_num_fields=3)
        if set(query) not in ({'job', 'tick'}, {'job', 'tick', 'actor'}) or any(len(v) != 1 for v in query.values()):
            raise ValueError('Choose one run and period')
        key, tick = job_key(query['job'][0]), int(query['tick'][0])
        actor = query.get('actor', [None])[0]
        if scope is not None:
            if key != scope[0] or actor not in (None, scope[1]):
                raise PermissionError('This capability only grants the registered run and actor')
            actor = scope[1]
        data = workbench.inspect(key, tick, actor)
        if route == '/api/download':
            if data['status'] != 'available':
                raise FileNotFoundError('No export is available for this period')
            return json_bytes(data), 'application/json', f'ecosim-{key[:8]}-{tick}.json'
        return json_bytes(_browser_integers(data)), 'application/json', None

    def json_body(self):
        size = int(self.headers.get('Content-Length', '0'))
        if self.headers.get_content_type() != 'application/json' or not 0 < size <= 2_000_000:
            raise ValueError('Expected a bounded JSON request')
        return strict_json_loads(self.rfile.read(size).decode())


def serve(root, template, port, *, supplier_preview=None, choice_previews=(), withdrawal_template=None,
          labor_template=None, research_preview=None):
    with ExitStack() as stack:
        workbench = stack.enter_context(open_workspace(
            root, template, supplier_preview=supplier_preview, choice_previews=choice_previews,
            withdrawal_template=withdrawal_template, labor_template=labor_template))
        previews = {}
        for kind, config in (research_preview or {}).items():
            path = root/kind
            if path.is_symlink():
                raise ValueError('Research preview workspaces must be local directories')
            previews['/'+kind] = stack.enter_context(open_workspace(path, template, **{kind+'_template': config}))
        server = stack.enter_context(WorkbenchServer(workbench, port, previews=previews))
        print(f'{server.origin}/#{server.researcher}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
