"""幂等键去重/冲突拒绝；成本绑定版本化价格表。"""
from __future__ import annotations

import pytest

from app.api.schemas import ChatCompletionRequest, IncomingMessage
from app.core.errors import GatewayError
from tests.conftest import make_request


def _idem_request(key: str, content: str = "hi") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="standard-chat",
        messages=[IncomingMessage(role="user", content=content)],
        idempotency_key=key,
    )


# ---------------------------------------------------------------- 幂等
async def test_same_key_same_body_replays_without_second_call(harness):
    svc, fake = harness
    r1 = await svc.gateway.chat(_idem_request("k-1"))
    assert r1.endpoint_id == "ds-v4-flash"
    assert len(fake.calls) == 1

    # 同键同体：直接重放缓存，不再触达上游
    r2 = await svc.gateway.chat(_idem_request("k-1"))
    assert len(fake.calls) == 1
    assert r2.text == r1.text
    assert r2.endpoint_id == r1.endpoint_id

    # 只有一条 call 记录（去重，不产生第二次调用记账）
    recent = await svc.repo.recent_traces(20)
    calls = await svc.repo.get_trace_calls(recent[0]["trace_id"])
    assert len(calls) == 1


async def test_same_key_different_body_conflicts(harness):
    svc, fake = harness
    await svc.gateway.chat(_idem_request("k-2", content="first question"))
    with pytest.raises(GatewayError) as ei:
        await svc.gateway.chat(_idem_request("k-2", content="different question"))
    assert ei.value.code == "IDEMPOTENCY_CONFLICT"
    assert ei.value.http_status == 409
    # 冲突请求未触达上游
    assert len(fake.calls) == 1


async def test_failed_request_releases_key_for_retry(harness):
    svc, fake = harness
    # 未知逻辑模型 -> 路由失败；占位应释放，同键可用于后续合法请求
    bad = ChatCompletionRequest(
        model="does-not-exist",
        messages=[IncomingMessage(role="user", content="hi")],
        idempotency_key="k-3",
    )
    with pytest.raises(GatewayError):
        await svc.gateway.chat(bad)

    good = ChatCompletionRequest(
        model="standard-chat",
        messages=[IncomingMessage(role="user", content="hi")],
        idempotency_key="k-3",
    )
    resp = await svc.gateway.chat(good)
    assert resp.endpoint_id == "ds-v4-flash"


# ------------------------------------------------------------- 价格版本化
async def test_cost_bound_to_versioned_price_table(harness):
    svc, fake = harness
    await svc.gateway.chat(make_request("standard-chat"))
    recent = await svc.repo.recent_traces(1)
    trace_id = recent[0]["trace_id"]

    async with svc.db.conn.execute(
        "SELECT price_version, total_cost FROM cost_ledger WHERE trace_id=?", (trace_id,)
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows and all(r["price_version"].startswith("cfg_") for r in rows)
    version = rows[0]["price_version"]

    async with svc.db.conn.execute(
        "SELECT snapshot_json FROM price_versions WHERE version=?", (version,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    import json
    snap = json.loads(row["snapshot_json"])
    assert set(snap) == {"ds-v4-pro", "ds-v4-flash"}


async def test_price_version_deterministic_for_same_config(harness):
    svc, _ = harness
    from app.services.observability.pricing import PriceCatalog
    from app.config import load_config
    other = PriceCatalog(load_config("gateway.yaml"))
    assert other.version == svc.gateway.pricing.version
