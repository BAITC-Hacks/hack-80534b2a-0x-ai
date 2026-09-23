"""Recommendation scheduling checks with fake local work and no provider calls."""
import asyncio
import threading
import time
import unittest

from fastapi import HTTPException

from recommendation_runtime import RecommendationRuntime


class RecommendationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtimes = []
        self.releases = []

    def tearDown(self):
        # Always unblock real threads before shutdown, including failed tests.
        for release in self.releases:
            release.set()
        for runtime in self.runtimes:
            runtime.close()

    def runtime(self, **kwargs):
        runtime = RecommendationRuntime(**kwargs)
        self.runtimes.append(runtime)
        return runtime

    def blocked_compute(self, value=None):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []

        def compute():
            calls.append(1)
            started.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release worker')
            return value or {'source': 'ai', 'recommendations': [{'event_id': 'EV_005'}]}

        return compute, started, release, calls

    async def eventually(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            if time.monotonic() > deadline:
                self.fail('Condition did not become true')
            await asyncio.sleep(.001)

    @staticmethod
    def fallback():
        return {'source': 'rules_fallback', 'recommendations': []}

    async def test_concurrent_identical_requests_coalesce_and_cache_is_copied(self):
        runtime = self.runtime(timeout=2)
        compute, started, release, calls = self.blocked_compute()
        identity = ('E0001', 'ru')
        first = asyncio.create_task(runtime.get(identity, 'employee', compute, self.fallback))
        await self.eventually(started.is_set)
        second = asyncio.create_task(runtime.get(identity, 'hr', compute, self.fallback))
        await asyncio.sleep(.01)
        release.set()
        one, two = await asyncio.gather(first, second)
        self.assertEqual(len(calls), 1)
        one['recommendations'][0]['event_id'] = 'modified'
        self.assertEqual(two['recommendations'][0]['event_id'], 'EV_005')
        three = await runtime.get(identity, 'employee', compute, self.fallback)
        self.assertEqual(three['recommendations'][0]['event_id'], 'EV_005')
        self.assertEqual(len(calls), 1)

    async def test_invalidation_during_work_prevents_old_result_from_reentering_cache(self):
        runtime = self.runtime(timeout=2)
        old_compute, started, release, _ = self.blocked_compute(
            {'source': 'ai', 'recommendations': ['old']})
        identity = ('E0001', 'ru')
        old_request = asyncio.create_task(runtime.get(identity, 'employee', old_compute, self.fallback))
        await self.eventually(started.is_set)
        runtime.invalidate()
        new_calls = []

        def new_compute():
            new_calls.append(1)
            return {'source': 'ai', 'recommendations': ['new']}

        new_result = await runtime.get(identity, 'employee', new_compute, self.fallback)
        self.assertEqual(new_result['recommendations'], ['new'])
        release.set()
        await old_request
        await self.eventually(lambda: not runtime._jobs)
        cached = await runtime.get(identity, 'employee', new_compute, self.fallback)
        self.assertEqual(cached['recommendations'], ['new'])
        self.assertEqual(len(new_calls), 1)

    async def test_per_user_limit_returns_429_with_retry_after(self):
        runtime = self.runtime(per_user=1, global_limit=10)
        compute = lambda: {'source': 'ai', 'recommendations': []}
        await runtime.get(('E0001', 'ru'), 'employee', compute, self.fallback)
        with self.assertRaises(HTTPException) as caught:
            await runtime.get(('E0001', 'en'), 'employee', compute, self.fallback)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.headers['Retry-After'], '60')
        # A cached response spends no new provider quota; another user is allowed.
        await runtime.get(('E0001', 'ru'), 'employee', compute, self.fallback)
        await runtime.get(('E0002', 'ru'), 'hr', compute, self.fallback)

    async def test_global_limit_applies_across_users(self):
        runtime = self.runtime(per_user=10, global_limit=1)
        compute = lambda: {'source': 'ai', 'recommendations': []}
        await runtime.get(('E0001', 'ru'), 'employee', compute, self.fallback)
        with self.assertRaises(HTTPException) as caught:
            await runtime.get(('E0002', 'ru'), 'hr', compute, self.fallback)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.headers['Retry-After'], '60')

    async def test_timeout_and_invalidation_do_not_free_actual_worker_slots(self):
        runtime = self.runtime(timeout=.02, max_workers=2, per_user=20)
        compute_a, started_a, release_a, calls_a = self.blocked_compute()
        compute_b, started_b, release_b, calls_b = self.blocked_compute()
        result_a, result_b = await asyncio.gather(
            runtime.get(('E0001', 'ru'), 'employee', compute_a, self.fallback),
            runtime.get(('E0002', 'ru'), 'hr', compute_b, self.fallback))
        self.assertTrue(started_a.is_set() and started_b.is_set())
        self.assertEqual(result_a['fallback_reason'], 'provider_timeout')
        self.assertEqual(result_b['fallback_reason'], 'provider_timeout')
        runtime.invalidate()
        calls_c = []

        def compute_c():
            calls_c.append(1)
            return {'source': 'ai', 'recommendations': []}

        busy = await runtime.get(('E0003', 'ru'), 'hr', compute_c, self.fallback)
        self.assertEqual(busy['fallback_reason'], 'provider_busy')
        self.assertEqual(calls_c, [])
        self.assertEqual(len(runtime._jobs), 2)
        self.assertEqual((len(calls_a), len(calls_b)), (1, 1))
        release_a.set()
        release_b.set()
        await self.eventually(lambda: not runtime._jobs)
        recovered = await runtime.get(('E0003', 'ru'), 'hr', compute_c, self.fallback)
        self.assertEqual(recovered['source'], 'ai')
        self.assertEqual(len(calls_c), 1)

    async def test_cancelled_http_wait_does_not_cancel_real_job(self):
        runtime = self.runtime(timeout=2, max_workers=1)
        compute, started, release, calls = self.blocked_compute()
        request = asyncio.create_task(runtime.get(('E0001', 'ru'), 'employee', compute, self.fallback))
        await self.eventually(started.is_set)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(len(runtime._jobs), 1)
        busy = await runtime.get(('E0002', 'ru'), 'hr', compute, self.fallback)
        self.assertEqual(busy['fallback_reason'], 'provider_busy')
        self.assertEqual(len(calls), 1)
        release.set()
        await self.eventually(lambda: not runtime._jobs)


if __name__ == '__main__':
    unittest.main()
