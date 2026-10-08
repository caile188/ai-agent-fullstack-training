"""流式门开后中断：error 是唯一终态，但已产出 token 的 trace/call/cost 必须落账。

回归场景：门开后网络错误曾被 invoker 就地 yield 成 error 事件，gateway 的 finally
因拿不到 finish 而整段跳过，造成无 trace、无计费。现在统一为抛错路径并落账。
"""
from __future__ import annotations

import json

import pytest

from app.api.sse import to_sse
from app.core.errors import GatewayError
from app.core.models import TokenUsage
from app.core.streaming import DELTA, FINISH, START, USAGE, StreamEvent
from app.services.execution.engine import AttemptRecord
from tests.conftest import make_request


def _interrupt_after_output(records: list[AttemptRecord]):
    """模拟引擎：首包门已开（usage+delta 已产出），随后上游中断。"""

    async def execute_stream(req, chain, budget, recs):
        rec = AttemptRecord(attempt=1, endpoint_id="ds-v4-flash", stream=True, ttft_ms=12.5)
        recs.append(rec)
        records.append(rec)

        async def gen():
            yield StreamEvent.start(
                "standard-chat", resolved_model="deepseek-v4-flash", endpoint_id="ds-v4-flash"
            )
            yield StreamEvent.usage_snapshot(TokenUsage(input_tokens=8, cached_tokens=1))
            yield StreamEvent.delta("hi")
            raise GatewayError("UPSTREAM_STREAM_INTERRUPTED", "connection reset")

        return gen()

    return execute_stream


async def test_post_gate_interruption_records_trace_call_cost(harness, monkeypatch):
    svc, _ = harness
    captured: list[AttemptRecord] = []
    monkeypatch.setattr(
        svc.engine, "execute_stream", _interrupt_after_output(captured)
    )

    pipe = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    kinds = []
    with pytest.raises(GatewayError) as exc:
        async for evt in pipe:
            kinds.append(evt.type)
    assert exc.value.code == "UPSTREAM_STREAM_INTERRUPTED"
    # 中断前的内容事件已经交付，且没有 finish（唯一终态是 error）
    assert START in kinds and DELTA in kinds and USAGE in kinds and FINISH not in kinds

    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "failed"
    assert t["error_code"] == "UPSTREAM_STREAM_INTERRUPTED"
    assert t["endpoint_id"] == "ds-v4-flash"

    # 已在途尝试按 200 + 已处理 input tokens 记账
    calls = await svc.repo.get_trace_calls(t["trace_id"])
    assert len(calls) == 1
    assert calls[0]["http_status"] == 200 and calls[0]["input_tokens"] == 8

    async with svc.db.conn.execute(
        "SELECT COUNT(*) AS n FROM cost_ledger WHERE trace_id=?", (t["trace_id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 1


async def test_post_gate_interruption_sse_single_error_terminal(harness, monkeypatch):
    svc, _ = harness
    monkeypatch.setattr(svc.engine, "execute_stream", _interrupt_after_output([]))

    pipe = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    frames = [f async for f in to_sse(pipe)]
    data_frames = [f for f in frames if f.startswith("data:")]
    payloads = [
        json.loads(f[5:].strip()) for f in data_frames if f[5:].strip() != "[DONE]"
    ]
    # 中断前内容已交付（标准 content chunk），随后恰好一帧 error 信封 + DONE
    assert any(
        p.get("choices") and p["choices"][0]["delta"].get("content") == "hi"
        for p in payloads
    )
    errors = [p for p in payloads if "error" in p]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == "UPSTREAM_STREAM_INTERRUPTED"
    assert data_frames[-1].strip() == "data: [DONE]"
