"""Structured Streaming 提前止损：语法确定不可恢复时不等待上游终态。

上游即使随后正常补发 message_stop，网关也必须在识别到 BROKEN 的当下产出唯一
error 终态并停止消费（已处理 token 仍落账）。Schema 校验仍只在终态进行。
"""
from __future__ import annotations

import httpx

from app.api.schemas import IncomingResponseFormat
from app.core.streaming import DELTA, ERROR, FINISH
from tests.conftest import make_request


def _broken_json_stream() -> httpx.AsyncClient:
    # Anthropic 形态：未加引号的 oops 作为 JSON 值 -> 第二段即确定 BROKEN；
    # 上游随后仍补发正常的 stop 事件（网关不应消费到）
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"m_1",'
        '"model":"deepseek-v4-flash-20250801",'
        '"usage":{"input_tokens":8,"output_tokens":0,"cache_read_input_tokens":1}}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text": "{\\"answer\\": "}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text": "oops}"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":9}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_broken_stream_aborts_before_upstream_finish(harness):
    svc, _ = harness
    svc.engine._client = _broken_json_stream()

    pipe = await svc.gateway.stream_chat(
        make_request("standard-json", stream=True,
                     response_format=IncomingResponseFormat(type="json_object"))
    )
    events = [evt async for evt in pipe]

    assert any(e.type == DELTA for e in events)
    assert any(e.type == ERROR for e in events)
    assert not any(e.type == FINISH for e in events)
    err = next(e for e in events if e.type == ERROR)
    assert err.error_code == "OUTPUT_INVALID_JSON"

    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "failed"
    # message_start 已上报 8 个 input token：提前止损不影响计费
    async with svc.db.conn.execute(
        "SELECT COUNT(*) AS n FROM cost_ledger WHERE trace_id=?", (t["trace_id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 1
