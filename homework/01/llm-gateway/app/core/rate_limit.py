"""按 (租户, 逻辑模型) 独立的令牌桶限流（内存实现，并发安全）。

每个逻辑模型拥有独立配额桶，互不挤占；模型级未配置时回退租户默认 rate_limit。
并发限制管"同时在途数"，限流管"单位时间请求数"，两者互补。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.config import GatewayConfig, RateLimitConfig

# 空闲超过该时长的桶可被回收（桶的 last 即最后访问时刻）
_BUCKET_IDLE_TTL_SEC = 600.0
# 桶数量超过该阈值时触发一次惰性清扫，避免字典无限增长
_BUCKET_SWEEP_THRESHOLD = 1024


@dataclass(frozen=True)
class RateLimitResult:
    """
    不可变的判定结果：是否放行；拒绝时带"最快多久后恢复"的毫秒预估，放行时为 None
    """
    allowed: bool
    retry_after_ms: int | None = None


class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float) -> None:
        self.tokens = tokens  # 当前令牌数
        self.last = time.monotonic()  # 上次访问/计算时刻，既用于令牌回填，也作为空闲清扫的"最后访问时间"


class RateLimiter:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._buckets: dict[tuple[str, str], _Bucket] = {}  # key=(tenant_id, model)
        self._lock = asyncio.Lock()

    def _cfg(self, tenant_id: str, model: str) -> RateLimitConfig:
        """
        取该 (租户,模型) 生效的限流配置
        """
        tenant = self.config.tenant(tenant_id)
        return tenant.model_rate_limits.get(model, tenant.rate_limit)

    def _sweep_idle(self, now: float) -> None:
        """
        清扫空闲桶
        """
        stale = [
            key for key, bucket in self._buckets.items()
            if now - bucket.last >= _BUCKET_IDLE_TTL_SEC
        ]
        for key in stale:
            del self._buckets[key]

    async def acquire(self, tenant_id: str, model: str) -> RateLimitResult:
        """
        核心：尝试取一个令牌
        """
        cfg = self._cfg(tenant_id, model)
        if not cfg.enabled:
            return RateLimitResult(True)  # ① 限流关闭，直接放行
        refill = cfg.requests_per_minute / 60.0  # 每秒补充令牌数
        key = (tenant_id, model)
        async with self._lock:  # ② 临界区串行化
            now = time.monotonic()
            bucket = self._buckets.get(key)
            if bucket is None:  # ③ 懒建桶：初始令牌=burst
                # 以本次采样时刻为起点，避免新建桶时 now 早于 last 造成负回填
                bucket = _Bucket(float(cfg.burst))
                bucket.last = now
                self._buckets[key] = bucket
            # ④ 按经过时间回填，上限不超过 burst
            bucket.tokens = min(
                float(cfg.burst), bucket.tokens + (now - bucket.last) * refill
            )
            bucket.last = now
            if bucket.tokens >= 1.0:  # ⑤ 够 1 个令牌：扣 1 放行
                bucket.tokens -= 1.0
                result = RateLimitResult(True)
            else:
                # ⑥ 不够：预估攒到 1 个令牌的时间
                wait_sec = max(1.0, (1.0 - bucket.tokens) / refill)
                result = RateLimitResult(False, int(wait_sec * 1000))
            # 惰性清扫：仅在桶数量超阈值时扫描一次，摊销 O(n) 开销
            if len(self._buckets) > _BUCKET_SWEEP_THRESHOLD:
                self._sweep_idle(now)
            return result
