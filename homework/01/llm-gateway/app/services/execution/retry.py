"""重试策略：次数 + 总截止时间 + 错误类型三重约束，指数退避。"""
from __future__ import annotations

import asyncio
import random

from app.config import ResilienceConfig
from app.core.errors import GatewayError
from app.services.execution.budget import TimeoutBudget


class RetryPolicy:
    def __init__(self, config: ResilienceConfig) -> None:
        self.config = config

    def should_retry(self, attempt: int, err: GatewayError, budget: TimeoutBudget) -> bool:
        """attempt 从 1 起（1 = 首次调用）。"""
        if attempt - 1 >= self.config.retry.max_retries:
            return False
        if budget.expired():
            return False
        if not err.spec.retryable:
            return False
        # 重试后至少要留得出连接时间
        return budget.remaining_ms() > self.config.connect_timeout_ms

    def delay_ms(self, attempt: int, err: GatewayError | None = None) -> float:
        rc = self.config.retry
        # 429 若带 Retry-After 由 wait_seconds 决定；这里用指数 + 抖动
        base = min(rc.backoff_max_ms, rc.backoff_base_ms * (2 ** (attempt - 1)))
        return base * (0.5 + random.random() * 0.5)

    def wait_seconds(self, attempt: int, err: GatewayError, budget: TimeoutBudget) -> float | None:
        """本次重试前应等待的秒数；None 表示不应等待（等不起 -> 交给 fallback/抛出）。"""
        remaining = budget.remaining_ms()
        # 429：优先遵守上游 Retry-After；等待会超出总预算则立即放弃同端点重试
        if err.code == "UPSTREAM_429" and err.retry_after_ms is not None:
            if err.retry_after_ms > remaining:
                return None
            return err.retry_after_ms / 1000.0
        delay_ms = min(self.delay_ms(attempt), remaining)
        return delay_ms / 1000.0 if delay_ms > 0 else None

    async def sleep(self, attempt: int, err: GatewayError, budget: TimeoutBudget) -> bool:
        delay = self.wait_seconds(attempt, err, budget)
        if delay is None:
            return False  # 等不起（如 429 Retry-After 超出总预算）-> 交给 fallback
        try:
            await asyncio.sleep(delay)
            return not budget.expired()
        except asyncio.CancelledError:
            raise
