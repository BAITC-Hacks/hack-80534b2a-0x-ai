"""Bounded, process-local recommendation work for the single-worker MVP."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException


class RecommendationRuntime:
    def __init__(self, *, timeout=8.5, per_user=6, global_limit=60, max_workers=2):
        self.timeout = timeout
        self.per_user = per_user
        self.global_limit = global_limit
        self.max_workers = max_workers
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="recommendation")
        self._revision = 0
        self._cache = OrderedDict()
        self._jobs = {}
        self._requests = {}
        self._global_requests = deque()

    def invalidate(self):
        # Running jobs retain their slots until the actual network call ends.
        with self._lock:
            self._revision += 1
            self._cache.clear()

    def _limit(self, username, now):
        for user, times in list(self._requests.items()):
            while times and times[0] <= now - 60:
                times.popleft()
            if not times:
                del self._requests[user]
        while self._global_requests and self._global_requests[0] <= now - 60:
            self._global_requests.popleft()
        times = self._requests.setdefault(username, deque())
        if len(times) >= self.per_user or len(self._global_requests) >= self.global_limit:
            raise HTTPException(429, "recommendation_rate_limited", headers={"Retry-After": "60"})
        times.append(now)
        self._global_requests.append(now)

    def _finished(self, key, job):
        with self._lock:
            self._jobs.pop(key, None)
            if job.cancelled() or job.exception() is not None or key[0] != self._revision:
                return
            value = job.result()
            ttl = 60 if value.get("source") == "ai" else 10
            self._cache[key] = (time.monotonic() + ttl, copy.deepcopy(value))
            self._cache.move_to_end(key)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)

    async def get(self, identity, username, compute, fallback):
        with self._lock:
            key = (self._revision, *identity)
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                self._cache.move_to_end(key)
                return copy.deepcopy(cached[1])
            self._cache.pop(key, None)
            job = self._jobs.get(key)
            if job is None:
                self._limit(username, now)
                if len(self._jobs) < self.max_workers:
                    job = self._executor.submit(compute)
                    self._jobs[key] = job
                    job.add_done_callback(lambda finished: self._finished(key, finished))
        if job is None:
            return {**fallback(), "fallback_reason": "provider_busy"}
        try:
            # Cancelling the HTTP wait must not free the real worker's slot.
            return copy.deepcopy(await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(job)), self.timeout))
        except asyncio.TimeoutError:
            return {**fallback(), "fallback_reason": "provider_timeout"}
        except Exception:
            return {**fallback(), "fallback_reason": "provider_error"}

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)
