"""并发闸：并发限制比 QPS 更重要。每端点独立信号量 + 在途计数。"""
from __future__ import annotations

import asyncio

from app.config import GatewayConfig


class ConcurrencyLimiter:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._inflight: dict[str, int] = {}

    def _limit(self, endpoint_id: str) -> int:
        tenant = self.config.tenant("default")
        return tenant.max_concurrency_per_endpoint

    def _sem(self, endpoint_id: str) -> asyncio.Semaphore:
        if endpoint_id not in self._semaphores:
            self._semaphores[endpoint_id] = asyncio.Semaphore(self._limit(endpoint_id))
            self._inflight[endpoint_id] = 0
        return self._semaphores[endpoint_id]

    def available(self, endpoint_id: str) -> bool:
        # asyncio.Semaphore 无公开余量查询，用在途计数判定
        return self.inflight(endpoint_id) < self._limit(endpoint_id)

    def inflight(self, endpoint_id: str) -> int:
        return self._inflight.get(endpoint_id, 0)

    def load_ratio(self, endpoint_id: str) -> float:
        return self.inflight(endpoint_id) / float(self._limit(endpoint_id))

    async def acquire(self, endpoint_id: str) -> None:
        sem = self._sem(endpoint_id)
        await sem.acquire()
        self._inflight[endpoint_id] = self._inflight.get(endpoint_id, 0) + 1

    def release(self, endpoint_id: str) -> None:
        sem = self._sem(endpoint_id)
        sem.release()
        self._inflight[endpoint_id] = max(0, self._inflight.get(endpoint_id, 0) - 1)
