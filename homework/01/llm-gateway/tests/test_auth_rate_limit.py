"""接入认证（多 Key 常量时间比较）与每租户令牌桶限流。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import GatewayAuthConfig, RateLimitConfig, TenantConfig
from app.core.rate_limit import RateLimiter


class _FakeConfig:
    def __init__(self, rl: RateLimitConfig,
                 model_rls: dict[str, RateLimitConfig] | None = None) -> None:
        self._rl = rl
        self._model_rls = model_rls or {}

    def tenant(self, tenant_id: str) -> TenantConfig:
        return TenantConfig(
            id=tenant_id, budget_limit_usd=1.0, rate_limit=self._rl,
            model_rate_limits=self._model_rls,
        )


async def test_token_bucket_burst_then_reject():
    cfg = _FakeConfig(RateLimitConfig(enabled=True, requests_per_minute=60, burst=3))
    rl = RateLimiter(cfg)  # type: ignore[arg-type]
    results = [await rl.acquire("default", "standard-chat") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]


async def test_rate_limit_disabled_passes():
    cfg = _FakeConfig(RateLimitConfig(enabled=False, requests_per_minute=1, burst=1))
    rl = RateLimiter(cfg)  # type: ignore[arg-type]
    for _ in range(5):
        result = await rl.acquire("default", "standard-chat")
        assert result.allowed is True and result.retry_after_ms is None


async def test_buckets_are_isolated_per_model():
    # 每个逻辑模型独立桶：一个模型耗尽不影响另一个
    cfg = _FakeConfig(RateLimitConfig(enabled=True, requests_per_minute=60, burst=2))
    rl = RateLimiter(cfg)  # type: ignore[arg-type]
    assert (await rl.acquire("default", "standard-chat")).allowed is True
    assert (await rl.acquire("default", "standard-chat")).allowed is True
    assert (await rl.acquire("default", "standard-chat")).allowed is False
    # 另一模型仍有完整突发额度
    assert (await rl.acquire("default", "premium-chat")).allowed is True
    assert (await rl.acquire("default", "premium-chat")).allowed is True
    assert (await rl.acquire("default", "premium-chat")).allowed is False
    # 租户同样隔离
    assert (await rl.acquire("team-a", "standard-chat")).allowed is True


async def test_model_rate_limit_overrides_tenant_default():
    # premium-chat 覆盖为更小配额（burst=1），其余模型走租户默认（burst=3）
    cfg = _FakeConfig(
        RateLimitConfig(enabled=True, requests_per_minute=60, burst=3),
        model_rls={"premium-chat": RateLimitConfig(
            enabled=True, requests_per_minute=60, burst=1)},
    )
    rl = RateLimiter(cfg)  # type: ignore[arg-type]
    assert (await rl.acquire("default", "premium-chat")).allowed is True
    assert (await rl.acquire("default", "premium-chat")).allowed is False
    assert (await rl.acquire("default", "standard-chat")).allowed is True


async def test_denied_result_carries_retry_after():
    cfg = _FakeConfig(RateLimitConfig(enabled=True, requests_per_minute=60, burst=1))
    rl = RateLimiter(cfg)  # type: ignore[arg-type]
    assert (await rl.acquire("default", "standard-chat")).allowed is True
    denied = await rl.acquire("default", "standard-chat")
    assert denied.allowed is False
    # 60/min -> 1 token/sec，至少需等待 1s
    assert denied.retry_after_ms is not None and denied.retry_after_ms >= 1000


async def test_idle_buckets_are_swept(monkeypatch):
    import app.core.rate_limit as rl_mod

    # 缩短阈值与 TTL，便于构造
    monkeypatch.setattr(rl_mod, "_BUCKET_SWEEP_THRESHOLD", 2)
    monkeypatch.setattr(rl_mod, "_BUCKET_IDLE_TTL_SEC", 0.0)
    cfg = _FakeConfig(RateLimitConfig(enabled=True, requests_per_minute=60, burst=3))
    rl = RateLimiter(cfg)  # type: ignore[arg-type]
    await rl.acquire("default", "model-a")
    await rl.acquire("default", "model-b")
    # 桶数量超阈值（2）触发清扫；TTL=0 时全部视为空闲被回收
    await rl.acquire("default", "model-c")
    assert len(rl._buckets) == 0


def test_auth_rejects_missing_and_bad_key(monkeypatch):
    from app.main import app

    monkeypatch.setenv("GATEWAY_KEYS", "key-one,key-two")
    payload = {"model": "standard-chat", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as c:
        c.app.state.services.config.auth = GatewayAuthConfig(
            enabled=True, api_key_envs=["GATEWAY_KEYS"]
        )
        # 无凭证
        r = c.post("/v1/chat/completions", json=payload)
        assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_MISSING_KEY"
        # 错误 key（在鉴权阶段即拒绝，不触达上游）
        r = c.post("/v1/chat/completions", json=payload,
                   headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_INVALID_KEY"
        # x-api-key 错误同样拒绝
        r = c.post("/v1/chat/completions", json=payload,
                   headers={"x-api-key": "nope"})
        assert r.status_code == 401


def test_auth_accepts_configured_key(monkeypatch):
    from app.main import app
    from tests.fake_upstream import FakeUpstream

    monkeypatch.setenv("GATEWAY_KEYS", "good-key")
    payload = {"model": "standard-chat", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as c:
        services = c.app.state.services
        services.config.auth = GatewayAuthConfig(
            enabled=True, api_key_envs=["GATEWAY_KEYS"]
        )
        services.engine._client = FakeUpstream().client()   # 不触真实网络
        r = c.post("/v1/chat/completions", json=payload,
                   headers={"Authorization": "Bearer good-key"})
        assert r.status_code == 200
        assert r.json()["model"] == "deepseek-v4-flash-20250801"
