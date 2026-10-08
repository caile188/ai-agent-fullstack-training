"""运行时健康视图：组合熔断器 + 并发闸，实现 core 路由所需的 HealthReader 协议。"""
from __future__ import annotations

from app.services.execution.concurrency import ConcurrencyLimiter
from app.services.execution.health import CircuitBreaker


class RuntimeHealth:
    def __init__(self, breaker: CircuitBreaker, limiter: ConcurrencyLimiter) -> None:
        self.breaker = breaker
        self.limiter = limiter

    def is_circuit_open(self, endpoint_id: str) -> bool:
        return self.breaker.is_open(endpoint_id)

    def health_score(self, endpoint_id: str) -> float:
        return self.breaker.health_score(endpoint_id)

    def load_ratio(self, endpoint_id: str) -> float:
        return self.limiter.load_ratio(endpoint_id)

    def concurrency_available(self, endpoint_id: str) -> bool:
        return self.limiter.available(endpoint_id)
