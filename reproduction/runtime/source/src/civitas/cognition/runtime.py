"""Bounded calls, raw-first persistence, explicit run-control failures and exact recovery."""
import asyncio
from dataclasses import dataclass
import math
import time

from civitas.contracts.base import content_hash
from civitas.contracts.receipts import ModelCallReceipt
from core.experiments.strict_types import strict_json_loads
from .budget import BudgetExceeded
from .cache import make_request, semantic_key
from .providers import ProviderConfig, ProviderResponse


class RunControlError(RuntimeError):
    pass


class ModelRefusal(RunControlError):
    def __init__(self, receipt):
        super().__init__('refusal: retry_exhausted')
        self.receipt = receipt


@dataclass(frozen=True)
class InvocationResult:
    value: object
    receipt: ModelCallReceipt
    cache_hit: bool


def usage_counts(usage):
    incoming = usage.get('input_tokens', usage.get('prompt_tokens'))
    outgoing = usage.get('output_tokens', usage.get('completion_tokens'))
    if not all(type(x) is int and x >= 0 for x in (incoming, outgoing)):
        return None, None
    for details, field, total in (('input_tokens_details', 'cached_tokens', incoming),
                                  ('prompt_tokens_details', 'cached_tokens', incoming),
                                  ('output_tokens_details', 'reasoning_tokens', outgoing),
                                  ('completion_tokens_details', 'reasoning_tokens', outgoing)):
        part = usage.get(details, {})
        if not isinstance(part, dict) or (field in part and
                (type(part[field]) is not int or not 0 <= part[field] <= total)):
            return None, None
    return incoming, outgoing


def invocation_receipt(row):
    request, response = row['request'], row['response']
    model = response['model'] if response else request['configuration']['model']
    usage = {} if row['incoming'] is None else {'input_tokens': row['incoming'], 'output_tokens': row['outgoing']}
    return ModelCallReceipt(request_id=row['request_id'], attempt_id=row['attempt_id'], input_hash=row['cache_key'],
        prompt_version=request['prompt_version'], parser_version=request['parser_version'], model_version=model,
        raw_response_ref='invocations:' + row['attempt_id'], usage=usage, latency_ms=row['latency_ms'],
        status='billing_unknown' if row['accounted'] is None else ('success' if row['state'] == 'success' else 'error'),
        metadata={'source': request['configuration']['source'], 'origin': response['origin'] if response else None,
                  'run_id': row['run_id'], 'task': request['task'], 'sample_id': request['sample_id'],
                  'error': row['error'], 'accounted_cost_upper_bound_nano': row['accounted']})


class CognitiveRuntime:
    def __init__(self, *, store, budget, provider, config: ProviderConfig, run_id, concurrency=8, on_receipt=None,
                 request_namespace=None, allow_cache_reuse=True, archive_history=False):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError('Positive concurrency required')
        if store.path != budget.store.path:
            raise ValueError('Invocations and budget must share one atomic database')
        self.config = ProviderConfig.model_validate(config)
        if provider.source != config.source:
            raise ValueError('Provider source is not the declared data origin')
        self.store, self.budget, self.provider, self.run_id = store, budget, provider, run_id
        self.on_receipt = on_receipt
        if request_namespace is not None and (set(request_namespace) != {'lineage','branch'} or
                not all(type(v) is str and v for v in request_namespace.values())):
            raise ValueError('Request namespace requires lineage and branch identities')
        self.request_namespace = request_namespace
        if type(allow_cache_reuse) is not bool:
            raise ValueError('Cache reuse must be an explicit boolean')
        self.allow_cache_reuse = allow_cache_reuse
        self.archive_history = archive_history
        self._semaphore, self._locks = asyncio.Semaphore(concurrency), {}

    def emit(self, row):
        receipt = invocation_receipt(row)
        if self.on_receipt:
            self.on_receipt(receipt)
        return receipt

    async def request(self, observation, memory, *, policy_id, policy_version, task, sample_id, response_model,
                      validator, request_id=None, task_context=None, mechanism_versions=None, institution_versions=None,
                      **versions):
        body = make_request(observation, memory, self.config, policy_id=policy_id, policy_version=policy_version,
            task=task, sample_id=sample_id, response_schema=response_model.model_json_schema(),
            task_context=task_context or {}, mechanism_versions=mechanism_versions or {},
            institution_versions=institution_versions or {}, **versions)
        identity = ({**self.request_namespace, 'tick':observation.visible_until.tick, 'phase':'DECIDE',
                     'actor':observation.actor_id, 'cognition_slot':task+':'+sample_id}
                    if self.request_namespace is not None else {'run': self.run_id, 'input': semantic_key(body)})
        if self.request_namespace is not None and request_id not in (None,'request:'+content_hash(identity)):
            raise ValueError('Explicit request ID differs from the logical cognition slot')
        request_id = request_id or 'request:' + content_hash(identity)
        async with self._locks.setdefault(request_id, asyncio.Lock()):
            if self.archive_history:
                for row in self.store.records(request_id):
                    if row['state'] == 'failed':
                        self.emit(row)
            hit = self.store.open_request(request_id, body, allow_cache=self.allow_cache_reuse)
            if hit:
                row = self.store.record(hit)
                value = response_model.model_validate(row['parsed'])
                validator(value)
                return InvocationResult(value, self.emit(row), True)
            async with self._semaphore:
                return await self._invoke(request_id, body, response_model, validator)

    async def _invoke(self, request_id, body, response_model, validator):
        error = {'code': 'retry_exhausted'}
        while True:
            previous = self.store.records(request_id)
            if previous and previous[-1]['state'] == 'success':
                row = previous[-1]
                value = response_model.model_validate(row['parsed'])
                validator(value)
                return InvocationResult(value, self.emit(row), True)
            if previous and previous[-1]['state'] == 'received':
                row = previous[-1]  # Crash after saving a response: parse it without another request.
            else:
                if previous and previous[-1]['state'] == 'started':
                    raise RunControlError('attempt_unresolved: request may have reached provider')
                if len(previous) >= self.config.max_attempts:
                    code = previous[-1]['error']['code'] if previous else error['code']
                    if code == 'refusal' and previous[-1]['accounted'] == 0:
                        raise ModelRefusal(invocation_receipt(previous[-1]))
                    raise RunControlError(code + ': retry_exhausted')
                try:
                    attempt_id = self.store.begin_attempt(request_id, self.run_id, self.budget, self.config)
                except BudgetExceeded as exc:
                    raise RunControlError('budget_exhausted') from exc
                except RuntimeError as exc:
                    hit = self.store.open_request(request_id, body, allow_cache=self.allow_cache_reuse)
                    if hit:
                        row = self.store.record(hit)
                        value = response_model.model_validate(row['parsed'])
                        validator(value)
                        return InvocationResult(value, self.emit(row), True)
                    raise RunControlError('attempt_unresolved: another worker owns this request') from exc
                start = time.perf_counter()
                cancelled = None
                try:
                    raw = await asyncio.wait_for(self.provider.complete(body), timeout=self.config.timeout_seconds)
                    response = ProviderResponse.model_validate(raw)
                except BaseException as exc:
                    cancelled = exc if not isinstance(exc, Exception) else None
                    code = 'timeout' if isinstance(exc, TimeoutError) else ('cancelled' if cancelled else 'transport_error')
                    response = ProviderResponse(text='', provider=self.config.provider, model=self.config.model,
                        ok=False, error_code=code, raw={'exception_type': type(exc).__name__, 'error': str(exc)},
                        origin='live' if self.config.source == 'live' else 'synthetic_test')
                incoming, outgoing = usage_counts(response.usage)
                self.store.save_response(attempt_id, response, math.ceil((time.perf_counter() - start) * 1000),
                                         self.budget, incoming, outgoing)
                row = self.store.record(attempt_id)
                if cancelled:
                    self.store.finish(attempt_id, error={'code': 'cancelled'})
                    self.emit(self.store.record(attempt_id))
                    raise cancelled
            response = ProviderResponse.model_validate(row['response'])
            error, value = None, None
            if not response.ok:
                error = {'code': response.error_code or 'provider_error'}
            elif (response.provider != self.config.provider
                    or response.model not in {self.config.model, self.config.revision}
                    or (self.config.model_manifest is not None and response.model != self.config.revision)
                    or (self.config.source == 'live' and response.origin != 'live')):
                error = {'code': 'provider_identity_mismatch'}
            else:
                try:
                    value = response_model.model_validate(strict_json_loads(response.text))
                    validator(value)
                except (ValueError, TypeError) as exc:
                    value = None
                    error = {'code': 'invalid_response', 'type': type(exc).__name__, 'message': str(exc)}
            if not error and row['accounted'] is None:
                error = {'code': 'billing_unknown'}
            if self.budget.snapshot()['blocked']:
                error = {'code': 'usage_exceeds_bound'}
            self.store.finish(row['attempt_id'], parsed=value.model_dump(mode='json') if value is not None else None, error=error)
            row = self.store.record(row['attempt_id'])
            receipt = self.emit(row)
            if not error:
                return InvocationResult(value, receipt, False)
            if error['code'] in {'billing_unknown', 'usage_exceeds_bound', 'provider_identity_mismatch'}:
                raise RunControlError(error['code'])
