"""Atomic project/run ceilings in integer nano currency units; no world rollback."""
from typing import Literal

from pydantic import Field

from civitas.contracts.base import NonNegative, Positive, Record, content_hash
from core.experiments.strict_types import strict_json_loads
from .invocations import encode


class BudgetLimits(Record):
    usd_nano: int = Field(default=80_000_000_000, ge=0, le=100_000_000_000)
    cny_nano: int = Field(default=80_000_000_000, ge=0, le=100_000_000_000)
    max_attempts: int = Field(default=200, ge=0)
    max_tokens: NonNegative = 10_000_000


class Price(Record):
    currency: Literal['USD', 'CNY']
    input_nano: NonNegative
    output_nano: NonNegative


class BudgetExceeded(RuntimeError):
    pass


class BudgetController:
    def __init__(self, store, limits: BudgetLimits):
        self.store = store
        self.limits = BudgetLimits.model_validate(limits)
        with store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO contracts VALUES (?,?)',
                       ('project-budget-v1', encode(self.limits.model_dump())))
        if self.limits != self.project_limits(store):
            raise ValueError('Persisted budget contract cannot change without an explicit amendment')

    @staticmethod
    def _amendment(base, limits, authorization):
        if type(authorization) is not str or not authorization.strip():
            raise ValueError('A quota amendment requires recorded authorization')
        if (base.usd_nano, base.cny_nano) != (limits.usd_nano, limits.cny_nano):
            raise ValueError('A quota amendment cannot change currency ceilings')
        if limits.max_attempts < base.max_attempts or limits.max_tokens < base.max_tokens:
            raise ValueError('A quota amendment cannot reduce cumulative quotas')
        return {'parent_hash': content_hash(base), 'limits': limits.model_dump(),
                'authorization': authorization}

    @classmethod
    def _project_versions(cls, store):
        with store.transaction() as db:
            rows = dict(db.execute("SELECT name,body FROM contracts WHERE name LIKE 'project-budget-v%'"))
        if 'project-budget-v1' not in rows:
            raise ValueError('Existing project budget contract required')
        if set(rows) != {f'project-budget-v{i}' for i in range(1,len(rows)+1)}:
            raise ValueError('Project budget versions must be contiguous')
        versions = [BudgetLimits.model_validate_json(rows['project-budget-v1'])]
        for i in range(2,len(rows)+1):
            amendment = strict_json_loads(rows[f'project-budget-v{i}'])
            limits = BudgetLimits.model_validate(amendment['limits'])
            if amendment != cls._amendment(versions[-1], limits, amendment['authorization']):
                raise ValueError('Invalid project budget amendment')
            versions.append(limits)
        return versions

    @classmethod
    def project_limits(cls, store):
        return cls._project_versions(store)[-1]

    def amend_project_limits(self, limits: BudgetLimits, *, authorization, version=2):
        limits = BudgetLimits.model_validate(limits)
        versions = self._project_versions(self.store)
        if type(version) is not int or not 2 <= version <= len(versions)+1:
            raise ValueError('An amendment version requires its existing parent')
        base = versions[version-2]
        self.store.pin_contract(f'project-budget-v{version}', self._amendment(base, limits, authorization))
        self.limits = self.project_limits(self.store)

    def register_run(self, run_id, limits: BudgetLimits, *, exclusive=False):
        limits = BudgetLimits.model_validate(limits)
        if any(getattr(limits, k) > getattr(self.limits, k) for k in BudgetLimits.model_fields):
            raise ValueError('Run allocation exceeds project contract')
        name = 'run:' + run_id
        if exclusive:
            with self.store.transaction() as db:
                if db.execute('SELECT 1 FROM contracts WHERE name=?', (name,)).fetchone():
                    raise ValueError('Live run identity already allocated in the cumulative ledger')
                db.execute('INSERT INTO contracts VALUES (?,?)', (name, encode(limits.model_dump())))
        else:
            self.store.pin_contract(name, limits.model_dump())

    def reserve(self, run_id, attempt_id, price: Price, *, input_cap, output_cap):
        from pydantic import TypeAdapter
        price = Price.model_validate(price)
        input_cap = TypeAdapter(Positive).validate_python(input_cap, strict=True)
        output_cap = TypeAdapter(Positive).validate_python(output_cap, strict=True)
        with self.store.transaction() as db:
            return self._reserve(db, run_id, attempt_id, price, input_cap, output_cap)

    def _reserve(self, db, run_id, attempt_id, price, input_cap, output_cap):
        run = db.execute('SELECT body FROM contracts WHERE name=?', ('run:' + run_id,)).fetchone()
        if not run:
            raise ValueError('Run allocation is required')
        if db.execute("SELECT 1 FROM contracts WHERE name='budget-blocked'").fetchone():
            raise BudgetExceeded('Provider usage exceeded its registered bound; reconcile first')
        rows = [dict(r) for r in db.execute('SELECT * FROM reservations')]
        reserved = input_cap * price.input_nano + output_cap * price.output_nano
        for scope, limits in ((rows, self.limits.model_dump()),
                              ([r for r in rows if r['run_id'] == run_id], strict_json_loads(run['body']))):
            consumed = sum(r['reserved'] if r['accounted'] is None else r['accounted']
                           for r in scope if r['currency'] == price.currency)
            if (consumed + reserved > limits[price.currency.lower() + '_nano']
                    or len(scope) + 1 > limits['max_attempts']
                    or sum(r['input_cap'] + r['output_cap'] for r in scope) + input_cap + output_cap > limits['max_tokens']):
                raise BudgetExceeded('Project or run budget exhausted')
        db.execute('''INSERT INTO reservations
            (attempt_id,run_id,currency,input_cap,output_cap,input_price,output_price,reserved)
            VALUES (?,?,?,?,?,?,?,?)''',
            (attempt_id, run_id, price.currency, input_cap, output_cap, price.input_nano, price.output_nano, reserved))
        return {'attempt_id': attempt_id, 'reserved_nano': reserved}

    def settle(self, attempt_id, *, input_tokens, output_tokens):
        with self.store.transaction() as db:
            return self._settle(db, attempt_id, input_tokens, output_tokens)

    def _settle(self, db, attempt_id, incoming, outgoing):
        row = db.execute('SELECT * FROM reservations WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None:
            raise ValueError('Usage without reservation')
        known = all(type(x) is int and x >= 0 for x in (incoming, outgoing))
        if not known:
            incoming = outgoing = None
        accounted = incoming * row['input_price'] + outgoing * row['output_price'] if known else None
        if row['accounted'] is not None:
            if (incoming, outgoing) != (row['incoming'], row['outgoing']):
                raise ValueError('Conflicting settled usage')
            return row['accounted']
        if known and (incoming > row['input_cap'] or outgoing > row['output_cap']):
            db.execute('INSERT OR IGNORE INTO contracts VALUES (?,?)',
                       ('budget-blocked', encode({'attempt_id': attempt_id, 'reason': 'token_bound_exceeded'})))
        db.execute('UPDATE reservations SET accounted=?,incoming=?,outgoing=?,settled=1 WHERE attempt_id=?',
                   (accounted, incoming, outgoing, attempt_id))
        return accounted

    def snapshot(self):
        with self.store.transaction() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM reservations ORDER BY attempt_id')]
            blocked = db.execute("SELECT body FROM contracts WHERE name='budget-blocked'").fetchone()
        result = {c: {'accounted_nano': sum(r['accounted'] or 0 for r in rows if r['currency'] == c),
                      'reserved_nano': sum(r['reserved'] for r in rows if r['currency'] == c and r['accounted'] is None)}
                  for c in ('USD', 'CNY')}
        return {**result, 'attempts': len(rows), 'tokens': sum(r['input_cap'] + r['output_cap'] for r in rows),
                'unknown_attempts': [r['attempt_id'] for r in rows if r['settled'] and r['accounted'] is None],
                'blocked': strict_json_loads(blocked['body']) if blocked else None}
