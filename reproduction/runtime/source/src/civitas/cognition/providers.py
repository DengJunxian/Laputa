"""Explicit provider configuration; optional live clients are imported only on use."""
from typing import Any, Literal

from pydantic import Field, model_validator

from civitas.contracts.base import Id, NonNegative, Record
from .budget import Price


class ProviderConfig(Record):
    source: Literal['synthetic_test', 'archived_response', 'live'] = 'synthetic_test'
    provider: Literal['synthetic', 'openai', 'deepseek'] = 'synthetic'
    model: Id = 'synthetic-test-v1'
    revision: Id = 'fixture-v1'
    endpoint: str = ''
    max_input_tokens: int = Field(default=32768, gt=0, le=200000)
    max_output_tokens: int = Field(default=2048, gt=0, le=128000)
    max_attempts: int = Field(default=2, gt=0, le=5)
    timeout_seconds: float = Field(default=30.0, gt=0, le=60.0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    price: Price = Price(currency='USD', input_nano=0, output_nano=0)
    pricing_reference: str = ''
    verified_at: str = ''

    @model_validator(mode='after')
    def live_boundaries(self):
        if self.source == 'live':
            endpoints = {'openai': 'https://api.openai.com/v1', 'deepseek': 'https://api.deepseek.com'}
            if self.endpoint != endpoints.get(self.provider):
                raise ValueError('Live endpoint must match the named provider')
            if not self.pricing_reference or not self.verified_at or min(self.price.input_nano, self.price.output_nano) <= 0:
                raise ValueError('Live model/pricing verification is required')
            if self.price.currency != ('USD' if self.provider == 'openai' else 'CNY'):
                raise ValueError('Provider currency mismatch')
            authorized = {'openai': ('gpt-6-luna', 250, 500), 'deepseek': ('deepseek-flash', 2000, 8000)}
            model, incoming, outgoing = authorized[self.provider]
            if self.model != model or self.price.input_nano < incoming or self.price.output_nano < outgoing:
                raise ValueError('Live identity or tariff falls outside the verified WP05 authorization')
        return self

    def wire_request(self, messages):
        if self.provider == 'openai':
            return {'model': self.model, 'input': messages, 'max_output_tokens': self.max_output_tokens,
                    'reasoning': {'effort': 'none'}, 'text': {'format': {'type': 'json_object'}},
                    'temperature': self.temperature, 'service_tier': 'default', 'store': False}
        body = {'model': self.model, 'messages': messages, 'max_tokens': self.max_output_tokens,
                'temperature': self.temperature, 'response_format': {'type': 'json_object'}}
        if self.provider == 'deepseek':
            body['thinking'] = {'type': 'disabled'}
        return body


class ProviderResponse(Record):
    text: str
    provider: Id
    model: Id
    usage: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error_code: str | None = None
    origin: Literal['synthetic_test', 'live'] = 'synthetic_test'


class SyntheticProvider:
    """Deterministic response fixtures for offline engineering checks."""
    source = 'synthetic_test'

    async def complete(self, request):
        from .invocations import encode
        obs, context = request['observation'], request['task_context']
        evidence = sorted([f['fact_id'] for key in ('account_view', 'market_view', 'private_memory_refs') for f in obs[key]]
                          + [m['message_id'] for m in obs['delivered_messages']])
        if request['task'] in ('reflection', 'cd_reflection'):
            body = {'actor_id': obs['actor_id'], 'observation_id': obs['observation_id'], 'evidence_ids': evidence[:1],
                    'semantic': ['Synthetic evidence-linked note.'] if evidence else [], 'procedural': []}
        elif request['task'] == 'cd_supplier':
            quotes = [f for f in obs['market_view'] if f['value'].get('kind') == 'goods_quote']
            scores, cited = {}, []
            for quote in quotes:
                seller = quote['value']['seller_id']
                by_root = {}
                for report in obs['delivered_messages']:
                    report_body = report['body']
                    root = report_body.get('root_claim_id')
                    if (root and report_body.get('seller_id') == seller
                            and report_body.get('outcome') in ('purchased', 'delivery_failed_refunded')):
                        by_root.setdefault(root, report)
                reports = list(by_root.values())
                scores[seller] = (10000 * sum(m['body']['outcome'] == 'purchased' for m in reports)
                                  // len(reports)) if reports else 5000
                cited.extend([quote['fact_id'], *(m['message_id'] for m in reports)])
            body = {'actor_id': obs['actor_id'], 'observation_id': obs['observation_id'],
                    'evidence_ids': sorted(set(cited)), 'scores_bps': scores}
        elif request['task'] == 'cd_labor':
            vacancy, = obs['market_view']
            body = {'vacancy_id': vacancy['fact_id'], 'action': 'wait'}
            return ProviderResponse(text=encode(body), provider=request['configuration']['provider'],
                model=request['configuration']['model'], usage={'prompt_tokens': 10, 'completion_tokens': 5},
                raw={'data_origin': 'synthetic_test', 'generator': 'closed-economy-labor-wait-v1'})
        elif request['task'] == 'cd_credit':
            offer, = obs['market_view']
            body = {'offer_fact_id': offer['fact_id'], 'action': 'wait'}
            return ProviderResponse(text=encode(body), provider=request['configuration']['provider'],
                model=request['configuration']['model'], usage={'prompt_tokens': 10, 'completion_tokens': 5},
                raw={'data_origin': 'synthetic_test', 'generator': 'closed-economy-credit-wait-v1'})
        elif request['task'] == 'cd_withdrawal':
            notice, = obs['market_view']
            body = {'notice_fact_id': notice['fact_id'], 'amount_minor': notice['value']['withdrawal_limit_minor']}
            return ProviderResponse(text=encode(body), provider=request['configuration']['provider'],
                model=request['configuration']['model'], usage={'prompt_tokens': 10, 'completion_tokens': 5},
                raw={'data_origin': 'synthetic_test', 'generator': 'closed-economy-withdrawal-v1'})
        elif request['task'] == 'cd_cooperation_choice':
            offer, = obs['market_view']
            body = {'opportunity_id': offer['fact_id'], 'action': 'defect'}
            return ProviderResponse(text=encode(body), provider=request['configuration']['provider'],
                model=request['configuration']['model'], usage={'prompt_tokens': 10, 'completion_tokens': 5},
                raw={'data_origin': 'synthetic_test', 'generator': 'closed-economy-cooperation-defect-v1'})
        elif request['task'] == 'cd_disclosure_choice':
            body = {'message_id': context['message_id'], 'action': 'withhold'}
            return ProviderResponse(text=encode(body), provider=request['configuration']['provider'],
                model=request['configuration']['model'], usage={'prompt_tokens': 10, 'completion_tokens': 5},
                raw={'data_origin': 'synthetic_test', 'generator': 'closed-economy-disclosure-withhold-v1'})
        elif request['task'] == 'closed_economy_cooperation':
            offer, = (f for f in obs['market_view'] if f['value'].get('kind') == 'cooperation_offer')
            body = {**context['identity'], 'evidence_ids': [offer['fact_id']], 'economic_intents': [{
                'kind': 'cooperation_choice', 'intent_id': context['intent_id'],
                'opportunity_id': offer['fact_id'], 'action': context['fixture_action']}]}
            return ProviderResponse(text=encode(body), provider=request['configuration']['provider'],
                model=request['configuration']['model'], usage={'prompt_tokens': 10, 'completion_tokens': 5},
                raw={'data_origin': 'synthetic_test', 'generator': 'closed-economy-cooperation-v1'})
        else:
            body = {**context['identity']}
            if request['task'] == 'hybrid':
                tick = obs['visible_until']['tick']
                scenarios = context['scenarios']
                horizon = context.get('outcome_contract', {}).get('horizon_ticks', min(3, context['max_horizon']))
                validity = min(2, horizon - 1) if 'outcome_contract' in context else min(2, context['max_horizon'])
                if 'plan_validity_ticks' in context:
                    validity = min(context['plan_validity_ticks']-1,horizon-1)
                body['belief_patch'] = {'joint_belief': {'probabilities': {s: 1.0 / len(scenarios) for s in scenarios},
                    'evidence_ids': evidence[:1], 'as_of_tick': tick,
                    'horizon_tick': tick + horizon,
                    'valid_until_tick': tick + validity, 'abstain': False}}
                body['evidence_ids'] = evidence[:1]
            else:
                body['economic_intents'] = [{'kind': 'hold', 'intent_id': 'intent:' + body['decision_id']}]
        return ProviderResponse(text=encode(body), provider=request['configuration']['provider'],
            model=request['configuration']['model'], usage={'prompt_tokens': 10, 'completion_tokens': 5},
            raw={'data_origin': 'synthetic_test', 'generator': 'uniform-belief-hold-v1'})


class ArchivedProvider:
    source = 'archived_response'

    def __init__(self, records, *, replay_attempts=False):
        from .cache import semantic_key
        self.responses = {}
        self.attempts = {} if replay_attempts else None
        for row in sorted(records, key=lambda row: (row['request_id'], row['number'])) if replay_attempts else records:
            if self.attempts is not None:
                if row['response'] is None or row['state'] not in ('failed', 'success'):
                    raise ValueError('Archived attempt has no terminal raw response')
                key = semantic_key(row['request'])
                self.attempts.setdefault(key, []).append(ProviderResponse.model_validate(row['response']))
                continue
            if row['response'] is None or row['parsed'] is None:
                continue
            key = semantic_key(row['request'])
            response = ProviderResponse.model_validate(row['response'])
            if key in self.responses and self.responses[key] != response:
                raise ValueError('Conflicting successful responses for one sample')
            self.responses[key] = response

    async def complete(self, request):
        from .cache import semantic_key
        key = semantic_key(request)
        if self.attempts is not None:
            if not self.attempts.get(key):
                raise ValueError('Archived attempt missing; online substitution is disabled')
            return self.attempts[key].pop(0)
        if key not in self.responses:
            raise ValueError('Archived response missing; online substitution is disabled')
        return self.responses[key]


class LiveProvider:
    source = 'live'

    def __init__(self, config: ProviderConfig, *, api_key=None, defer_client=False):
        self.config = ProviderConfig.model_validate(config)
        if config.source != 'live':
            raise ValueError('Explicit live configuration required')
        self.client = None
        if not defer_client or api_key is not None:
            self._connect(api_key)

    def _connect(self, api_key=None):
        import os
        config = self.config
        key = api_key if api_key is not None else os.environ.get(
            'OPENAI_API_KEY' if config.provider == 'openai' else 'DEEPSEEK_API_KEY', '')
        if not key:
            raise ValueError('Live credentials are not configured')
        if config.provider == 'openai':
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=key, base_url=config.endpoint, max_retries=0,
                                      timeout=config.timeout_seconds)
        else:
            from core.llm.config import LLMSettings
            from core.llm.deepseek_client import DeepSeekClient
            settings = LLMSettings(deepseek_api_key=key, zhipu_api_key='', default_provider='deepseek',
                deepseek_base_url=config.endpoint, max_retries=0, timeout_seconds=config.timeout_seconds)
            self.client = DeepSeekClient(settings=settings)

    async def complete(self, request):
        from .invocations import safe_value
        if self.client is None:
            self._connect()
        wire = request['wire']
        if self.config.provider == 'openai':
            try:
                raw = await self.client.responses.create(**wire)
            except Exception as exc:
                return ProviderResponse(text='', provider='openai', model=self.config.model, ok=False,
                    origin='live', error_code='http_' + str(getattr(exc, 'status_code', 'transport')),
                    raw=safe_value({'exception_type': type(exc).__name__, 'error': str(exc)}))
            text = raw.output_text
            error = None if raw.status == 'completed' else 'response_incomplete'
            if error is None:
                # SDK output_text also concatenates commentary; parse only one completed answer.
                messages = [item for item in raw.output if item.type == 'message'
                            and getattr(item, 'phase', None) != 'commentary']
                answer = messages[0] if len(messages) == 1 else None
                if (answer is None or answer.role != 'assistant' or answer.status != 'completed'
                        or getattr(answer, 'phase', None) not in (None, 'final_answer')
                        or not answer.content or any(part.type != 'output_text'
                            or not isinstance(getattr(part, 'text', None), str) for part in answer.content)):
                    error = 'response_output_invalid'
                else:
                    text = ''.join(part.text for part in answer.content)
            return ProviderResponse(text=text, provider='openai', model=raw.model, origin='live',
                usage=safe_value(raw.usage.model_dump()) if raw.usage else {}, raw=safe_value(raw),
                ok=error is None, error_code=error)
        response = await self.client.complete(wire['messages'], model=wire['model'],
            temperature=wire['temperature'], max_tokens=wire['max_tokens'],
            timeout=self.config.timeout_seconds, thinking=False, json_mode=True)
        raw = safe_value(response.raw)
        completed = response.ok and bool(raw.get('choices')) and raw['choices'][0].get('finish_reason') == 'stop'
        return ProviderResponse(text=response.text, provider=response.provider, model=response.model,
            usage=safe_value(response.usage), raw=raw, ok=completed, origin='live',
            error_code=None if completed else ('response_incomplete' if response.ok else 'provider_error'))

    async def close(self):
        if self.client is None:
            return
        if self.config.provider == 'openai':
            await self.client.close()
        else:
            # The legacy client has no lifecycle port; its SDK client is adapter-owned here.
            await self.client._get_client().close()
