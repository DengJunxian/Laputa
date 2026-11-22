"""External, durable invocation facts. This database is never a world participant."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import math
import re

from core.experiments.strict_types import strict_json_loads
from civitas.contracts.base import content_hash


def safe_value(value):
    """Retain response structure while removing credential fields and token strings."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if re.search(r'authorization|api.?key|token$|secret|password|headers', str(k), re.I)
                and str(k) not in {'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens'}
                else safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'(?i)\b(?:sk-[a-z0-9_\-]{8,}|bearer\s+[^\s,;]+)', '[REDACTED]', value)
    if isinstance(value, float) and not math.isfinite(value):
        return {'non_finite_number': str(value)}
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class ModelInvocationStore:
    def __init__(self, path, *, read_only=False, existing=False):
        self.path = Path(path).resolve()
        self.read_only = read_only
        self.existing = existing or read_only
        if self.existing:
            if not self.path.is_file():
                raise ValueError('Missing invocation ledger')
            required = {'contracts':{'name','body'},'requests':{'request_id','cache_key','body','success_attempt'},
                'attempts':{'attempt_id','request_id','number','response','parsed','error','latency_ms','state'},
                'reservations':{'attempt_id','run_id','currency','input_cap','output_cap','input_price','output_price',
                                'reserved','accounted','incoming','outgoing','settled'}}
            try:
                with sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True) as db:
                    db.row_factory = sqlite3.Row
                    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok' or any(
                        {r[1] for r in db.execute(f'PRAGMA table_info({table})')}!=columns for table,columns in required.items()):
                        raise ValueError('Invalid invocation ledger schema')
                    if db.execute('''SELECT COUNT(*) FROM attempts a LEFT JOIN requests r USING(request_id)
                            LEFT JOIN reservations b USING(attempt_id) WHERE r.request_id IS NULL OR b.attempt_id IS NULL''').fetchone()[0]:
                        raise ValueError('Incomplete invocation ledger attempt facts')
                    self._validate_reservations(db)
            except sqlite3.DatabaseError as exc:
                raise ValueError('Invalid invocation ledger database') from exc
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS contracts (name TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS reservations (
                attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, currency TEXT NOT NULL,
                input_cap INTEGER NOT NULL, output_cap INTEGER NOT NULL,
                input_price INTEGER NOT NULL, output_price INTEGER NOT NULL,
                reserved INTEGER NOT NULL, accounted INTEGER, incoming INTEGER, outgoing INTEGER,
                settled INTEGER NOT NULL DEFAULT 0)''')
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL, body TEXT NOT NULL,
                success_attempt TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, number INTEGER NOT NULL,
                response TEXT, parsed TEXT, error TEXT, latency_ms INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'started', UNIQUE(request_id,number))''')

    @staticmethod
    def _validate_reservations(db):
        from .providers import ProviderConfig
        for row in db.execute('''SELECT b.*,r.body FROM reservations b
                LEFT JOIN attempts a USING(attempt_id) LEFT JOIN requests r USING(request_id)'''):
            if (any(type(row[k]) is not int or row[k]<=0 for k in ('input_cap','output_cap'))
                    or any(type(row[k]) is not int or row[k]<0 for k in ('input_price','output_price','reserved'))
                    or any(row[k] is not None and (type(row[k]) is not int or row[k]<0)
                           for k in ('accounted','incoming','outgoing'))
                    or type(row['settled']) is not int or row['settled'] not in (0,1)
                    or row['currency'] not in ('USD','CNY')):
                raise ValueError('Invalid invocation ledger reservation values')
            known = row['incoming'] is not None and row['outgoing'] is not None
            cost = row['incoming']*row['input_price']+row['outgoing']*row['output_price'] if known else None
            if (row['reserved']!=row['input_cap']*row['input_price']+row['output_cap']*row['output_price']
                    or (row['incoming'] is None)!=(row['outgoing'] is None) or row['accounted']!=cost
                    or (not row['settled'] and (known or row['accounted'] is not None))):
                raise ValueError('Inconsistent invocation ledger reservation accounting')
            if row['body'] is not None:
                try:
                    config = ProviderConfig.model_validate(strict_json_loads(row['body'])['configuration'])
                except (ValueError,KeyError,TypeError) as exc:
                    raise ValueError('Invalid invocation ledger reservation request') from exc
                prices = (config.price.input_nano,config.price.output_nano) if config.source=='live' else (0,0)
                if ((row['input_cap'],row['output_cap'],row['currency'],row['input_price'],row['output_price'])
                        != (config.max_input_tokens,config.max_output_tokens,config.price.currency,*prices)):
                    raise ValueError('Invalid invocation ledger reservation: differs from request configuration')

    @contextmanager
    def transaction(self):
        target = self.path.as_uri()+('?mode=ro' if self.read_only else '?mode=rw') if self.existing else self.path
        db = sqlite3.connect(target,uri=self.existing,timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if not self.read_only:
                db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN' if self.read_only else 'BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def pin_contract(self, name, body):
        with self.transaction() as db:
            old = db.execute('SELECT body FROM contracts WHERE name=?', (name,)).fetchone()
            if old and strict_json_loads(old['body']) != body:
                raise ValueError('Persisted contract cannot change')
            db.execute('INSERT OR IGNORE INTO contracts VALUES (?, ?)', (name, encode(body)))

    def open_request(self, request_id, body, *, allow_cache=True):
        if safe_value(body) != body:
            raise ValueError('Credential-bearing requests cannot be archived')
        key = content_hash(body)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['cache_key'] != key or strict_json_loads(row['body']) != body:
                    raise ValueError('request_id content conflict')
                return row['success_attempt']
            hit = db.execute('SELECT success_attempt FROM requests WHERE cache_key=? AND success_attempt IS NOT NULL',
                             (key,)).fetchone() if allow_cache else None
            attempt = hit['success_attempt'] if hit else None
            db.execute('INSERT INTO requests VALUES (?,?,?,?)', (request_id, key, encode(body), attempt))
            return attempt

    def begin_attempt(self, request_id, run_id, budget, config):
        from .budget import Price
        with self.transaction() as db:
            request = db.execute('SELECT success_attempt FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if request is None or request['success_attempt'] is not None:
                raise RuntimeError('Missing request or request already completed')
            previous = list(db.execute('SELECT * FROM attempts WHERE request_id=? ORDER BY number', (request_id,)))
            if previous and previous[-1]['state'] in {'started', 'received'}:
                raise RuntimeError('Unresolved attempt; recover its saved response before retrying')
            number = len(previous) + 1
            if number > config.max_attempts:
                raise RuntimeError('Retry limit exhausted')
            attempt_id = f'{request_id}:attempt:{number}'
            price = config.price if config.source == 'live' else Price(currency=config.price.currency, input_nano=0, output_nano=0)
            budget._reserve(db, run_id, attempt_id, price, config.max_input_tokens, config.max_output_tokens)
            db.execute('INSERT INTO attempts (attempt_id,request_id,number) VALUES (?,?,?)',
                       (attempt_id, request_id, number))
        return attempt_id

    def save_response(self, attempt_id, response, latency_ms, budget, incoming, outgoing):
        with self.transaction() as db:
            previous = db.execute('SELECT response FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if previous is None or previous['response'] is not None:
                raise ValueError('Missing attempt or duplicate response')
            body = encode(safe_value(response))
            db.execute("UPDATE attempts SET response=?,latency_ms=?,state='received' WHERE attempt_id=?",
                       (body, latency_ms, attempt_id))
            budget._settle(db, attempt_id, incoming, outgoing)

    def finish(self, attempt_id, *, parsed=None, error=None):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if row is None or row['response'] is None:
                raise ValueError('A saved response or transport error is required')
            db.execute('UPDATE attempts SET parsed=?,error=?,state=? WHERE attempt_id=?',
                       (encode(parsed) if parsed is not None else None, encode(safe_value(error)) if error else None,
                        'failed' if error else 'success', attempt_id))
            if error is None:
                db.execute('UPDATE requests SET success_attempt=? WHERE request_id=?', (attempt_id, row['request_id']))

    def records(self, request_id=None, *, run_id=None):
        if request_id is not None and run_id is not None:
            raise ValueError('Filter a model ledger by one identity')
        if run_id is not None:
            return self._records('WHERE b.run_id=? ', (run_id,))
        return self._records('WHERE a.request_id=? ' if request_id else '', (request_id,) if request_id else ())

    def _records(self, where, parameters):
        with self.transaction() as db:
            rows = db.execute('''SELECT a.*,r.body,r.cache_key,b.accounted,b.incoming,b.outgoing,b.run_id
                FROM attempts a JOIN requests r USING(request_id) JOIN reservations b USING(attempt_id)
                ''' + where + 'ORDER BY a.request_id,a.number', parameters).fetchall()
        return [{**{k: row[k] for k in row.keys() if k not in {'body','response','parsed','error'}},
                 'request': strict_json_loads(row['body']),
                 **{k: strict_json_loads(row[k]) if row[k] is not None else None for k in ('response','parsed','error')}}
                for row in rows]

    def record(self, attempt_id):
        return next(iter(self._records('WHERE a.attempt_id=? ', (attempt_id,))))
