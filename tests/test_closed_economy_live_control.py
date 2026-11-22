import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from civitas.application import economic_run as runner
from civitas.cognition.budget import BudgetController, BudgetLimits, Price
from civitas.cognition.invocations import ModelInvocationStore
from civitas.cognition.providers import ProviderConfig
from civitas.cognition.runtime import RunControlError


@unittest.skipUnless(importlib.util.find_spec('openai'), 'requires llm extra')
class ClosedEconomyLiveControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / 'project.sqlite'
        self.ledger = ModelInvocationStore(self.path)
        self.project = BudgetController(self.ledger, BudgetLimits())
        self.provider = ProviderConfig(source='live', provider='openai', model='gpt-6-luna',
            revision='alias-only-test', endpoint='https://api.openai.com/v1', max_attempts=2,
            max_input_tokens=8192, max_output_tokens=256, price=Price(currency='USD', input_nano=250, output_nano=500),
            pricing_reference='mock-transport-v1', verified_at='2026-11-20')
        self.config = {'ticks': 3, 'households': 4, 'supplier_cognition': {'mode': 'L', 'source': 'live',
                       'ticks': [2], 'reflect': False}}
        env = patch.dict(os.environ, OPENAI_API_KEY='mock-transport-placeholder')
        env.start()
        self.addCleanup(env.stop)

    def options(self, attempts=2):
        return {'provider_config': self.provider, 'project_ledger': self.path,
                'limits': BudgetLimits(usd_nano=2_176_000 * attempts, cny_nano=0,
                                      max_tokens=8448 * attempts, max_attempts=attempts)}

    def mock_sdk(self, handler):
        import httpx
        import openai
        client = openai.AsyncOpenAI

        def mock_client(**kwargs):
            self.assertEqual(kwargs['max_retries'], 0)
            return client(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        return patch('openai.AsyncOpenAI', mock_client)

    @staticmethod
    def response(request, *, body=None, usage=True):
        import httpx
        wire = json.loads(request.content)
        obs = json.loads(wire['input'][1]['content'])['observation']
        if body is None:
            quotes = obs['market_view']
            body = json.dumps({'actor_id': obs['actor_id'], 'observation_id': obs['observation_id'],
                'scores_bps': {q['value']['seller_id']: 5000 for q in quotes},
                'evidence_ids': [q['fact_id'] for q in quotes]})
        return httpx.Response(200, json={'id': 'resp_mock', 'status': 'completed', 'model': 'gpt-6-luna',
            'output': [{'type': 'message', 'id': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': body, 'annotations': []}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}} if usage else None})

    async def test_real_sdk_with_mock_http_retries_once_and_archives_attempt_sequence(self):
        root = self.folder / 'retry'
        attempts = []

        async def handle(request):
            attempts.append(request.url.path)
            self.assertEqual(request.url.host, 'api.openai.com')
            return self.response(request, body='{' if len(attempts) == 1 else None)

        with self.mock_sdk(handle):
            result = await runner.run_new(self.config, root, **self.options())
        self.assertEqual(attempts, ['/v1/responses'] * 2)
        _, store, _ = runner.open_run(root)
        one, two = store.archived_records()
        self.assertEqual((one['state'], two['state']), ('failed', 'success'))
        self.assertEqual([r['accounted'] for r in (one, two)], [35000, 35000])
        with self.mock_sdk(lambda _: self.fail('archive called the network')):
            replay = await runner.replay_run(root, destination=self.folder / 'retry-replay')
        self.assertEqual((replay['state_hash'], replay['compared_ticks']), (result['state_hash'], 3))

    async def test_cancel_preserves_unknown_cost_and_recovery_reuses_history(self):
        root = self.folder / 'cancel'
        reached = asyncio.Event()

        async def hang(_):
            reached.set()
            await asyncio.Future()

        with self.mock_sdk(hang):
            task = asyncio.create_task(runner.run_new(self.config, root, **self.options()))
            await asyncio.wait_for(reached.wait(), 20)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        _, store, marker = runner.open_run(root)
        self.assertEqual(marker.tick, 1)
        self.assertEqual(store.archived_records(), [])
        cancelled, = self.ledger.records(run_id='cancel')
        self.assertEqual(cancelled['error']['code'], 'cancelled')
        self.assertIsNone(cancelled['accounted'])
        self.assertEqual(self.project.snapshot()['USD']['reserved_nano'], 2_176_000)
        with self.mock_sdk(self.response):
            result = await runner.resume_run(root)
        _, store, _ = runner.open_run(root)
        self.assertEqual([r['state'] for r in store.archived_records()], ['failed', 'success'])
        replay = await runner.replay_run(root, destination=self.folder / 'cancel-replay')
        self.assertEqual(replay['state_hash'], result['state_hash'])

    async def test_missing_usage_and_exhausted_budget_fail_closed_after_one_dispatch(self):
        for usage in (False, True):
            with self.subTest(usage=usage):
                root = self.folder / str(usage)
                calls = []

                def handle(request):
                    calls.append(request.url.path)
                    return self.response(request, body='{' if usage else None, usage=usage)

                with self.mock_sdk(handle), self.assertRaisesRegex(RunControlError,
                        'budget_exhausted' if usage else 'billing_unknown'):
                    await runner.run_new(self.config, root, **self.options(attempts=1))
                self.assertEqual(calls, ['/v1/responses'])
                _, store, marker = runner.open_run(root)
                self.assertEqual((marker.tick, store.archived_records()), (1, []))
                row, = self.ledger.records(run_id=root.name)
                self.assertEqual(row['accounted'], 35000 if usage else None)
