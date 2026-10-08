"""客户端断连：cancelled 是独立于 finish/error 的唯一终态。

两种触发路径：
  1. 主动探活：is_disconnected() 返回 True -> 落 cancelled 终态，已收 token 仍计费；
  2. ASGI 取消：消费任务在流中被 cancel（抛入 CancelledError）-> 落账后保持取消语义重抛。
flash 上游事件序：START -> USAGE(input=8) -> DELTA ...，故第三次探活时已有用量快照。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.errors import GatewayError
from tests.conftest import make_request


async def _assert_cancelled_and_billed(svc) -> None:
    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "cancelled"
    assert t["error_code"] == "CLIENT_CANCELLED"
    # message_start 已上报 8 个 input token：断连不影响已产出成本落账
    async with svc.db.conn.execute(
        "SELECT COUNT(*) AS n FROM cost_ledger WHERE trace_id=?", (t["trace_id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 1


async def test_proactive_disconnect_marks_cancelled(harness):
    svc, _ = harness
    calls = 0

    async def is_disconnected() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3  # START + USAGE 已处理，首个 DELTA 到达前判定断连

    pipe = await svc.gateway.stream_chat(
        make_request("standard-chat", stream=True),
        is_disconnected=is_disconnected,
    )
    with pytest.raises(GatewayError) as ei:
        async for _evt in pipe:
            pass
    assert ei.value.code == "CLIENT_CANCELLED"

    await _assert_cancelled_and_billed(svc)


async def test_asgi_cancel_finalizes_then_reraises(harness):
    svc, _ = harness
    reached = asyncio.Event()
    blocker = asyncio.Event()
    calls = 0

    async def is_disconnected() -> bool:
        nonlocal calls
        calls += 1
        if calls >= 3:
            # 已处理 START + USAGE；阻塞在这里让任务"挂在流中"，随后被外部取消
            reached.set()
            await blocker.wait()
        return False

    async def consume() -> None:
        pipe = await svc.gateway.stream_chat(
            make_request("standard-chat", stream=True),
            is_disconnected=is_disconnected,
        )
        async for _evt in pipe:
            pass

    task = asyncio.create_task(consume())
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_cancelled_and_billed(svc)
