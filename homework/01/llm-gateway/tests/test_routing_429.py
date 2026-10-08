"""同优先级加权轮询流量分摊；429 Retry-After 遵守与超预算立即 fallback。"""
from __future__ import annotations

import time

from tests.conftest import make_request


# --------------------------------------------------------------- weighted RR
async def test_weighted_round_robin_distribution(harness):
    svc, fake = harness
    svc.gateway.router.strategy = "weighted_round_robin"

    counts = {"ds-v4-flash": 0, "ds-v4-pro": 0}
    n = 8
    for _ in range(n):
        resp = await svc.gateway.chat(make_request("balanced-chat"))
        counts[resp.endpoint_id] += 1

    # balanced-chat 同优先级权重 flash:pro = 3:1
    assert counts["ds-v4-flash"] == 6
    assert counts["ds-v4-pro"] == 2
    # fallback 链始终把本次选中端点放在首位
    recent = await svc.repo.recent_traces(1)
    decision = await svc.repo.get_decision(recent[0]["trace_id"])
    assert decision["chosen_endpoint"] in counts


async def test_score_strategy_unchanged_is_deterministic(harness):
    svc, fake = harness
    # 默认 score 策略：standard-chat 总是首选 flash
    for _ in range(4):
        resp = await svc.gateway.chat(make_request("standard-chat"))
        assert resp.endpoint_id == "ds-v4-flash"


# --------------------------------------------------------------- 429 handling
async def test_429_retry_after_short_retries_same_endpoint(harness):
    svc, fake = harness
    # conftest 默认关掉同端点重试，这里打开以验证 Retry-After 驱动的等待重试
    object.__setattr__(svc.config.resilience.retry, "max_retries", 1)
    # flash 首次 429 并要求立即重试（Retry-After: 0），随后恢复
    fake.set_fail("flash", status=429, times=1, retry_after=0)

    resp = await svc.gateway.chat(make_request("standard-chat"))
    assert resp.endpoint_id == "ds-v4-flash"          # 没切换端点
    flash_calls = [r for r in fake.calls if r.url.path.endswith("/messages")]
    pro_calls = [r for r in fake.calls if r.url.path.endswith("/responses")]
    assert len(flash_calls) == 2                       # 首次 429 + 一次重试
    assert len(pro_calls) == 0


async def test_429_retry_after_too_long_falls_back_immediately(harness):
    svc, fake = harness
    object.__setattr__(svc.config.resilience.retry, "max_retries", 1)
    # Retry-After 60s 远超总预算 30s：不在同端点空等，立即沿锁定链 fallback 到 pro
    fake.set_fail("flash", status=429, times=1, retry_after=60)

    t0 = time.monotonic()
    resp = await svc.gateway.chat(make_request("standard-chat"))
    elapsed = time.monotonic() - t0

    assert resp.endpoint_id == "ds-v4-pro"
    assert elapsed < 5                                  # 没有真的等待 60s
    recent = await svc.repo.recent_traces(1)
    assert recent[0]["fallback_from"] == "ds-v4-flash"
