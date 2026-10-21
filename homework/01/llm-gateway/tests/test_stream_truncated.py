"""上游无报错却提前关闭（缺 message_stop / response.completed）：

连接读完不等于模型完成。唯一成功依据是终止事件；缺失时必须判为
UPSTREAM_STREAM_INTERRUPTED：trace 落 failed、出口是唯一 error 帧而非裸 [DONE]。
"""
from __future__ import annotations

import json

from app.api.sse import to_sse
from app.core.errors import GatewayError
from tests.conftest import make_request


def _data_lines(frames: list[str]) -> list[str]:
    lines: list[str] = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data:"):
                lines.append(line[5:].strip())
    return lines


async def _consume(pipe) -> tuple[list[str], GatewayError | None]:
    kinds: list[str] = []
    err: GatewayError | None = None
    try:
        async for evt in pipe:
            kinds.append(evt.type)
    except GatewayError as e:
        err = e
    return kinds, err


async def test_anthropic_truncated_before_message_stop_is_failure(harness):
    svc, fake = harness
    fake.set_truncated_stream("flash")

    pipe = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    kinds, err = await _consume(pipe)
    assert err is not None and err.code == "UPSTREAM_STREAM_INTERRUPTED"
    assert "finish" not in kinds  # 从未产出 finish 终态

    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "failed"
    assert t["error_code"] == "UPSTREAM_STREAM_INTERRUPTED"

    # message_start 已上报 input tokens：已产出的成本仍须落账
    calls = await svc.repo.get_trace_calls(t["request_id"])
    assert len(calls) == 1 and calls[0]["http_status"] == 200

    # 出口：内容帧已交付，但终态是 error，最后是 [DONE]；没有 finish_reason 帧
    fake.set_truncated_stream("flash")
    pipe2 = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    frames = [f async for f in to_sse(pipe2)]
    data = _data_lines(frames)
    payloads = [json.loads(d) for d in data if d != "[DONE]"]
    assert any("error" in p for p in payloads)
    assert all(
        not (p.get("choices") and p["choices"][0].get("finish_reason")) for p in payloads
    )
    assert data[-1] == "[DONE]"


async def test_responses_truncated_before_completed_is_failure(harness):
    svc, fake = harness
    fake.set_truncated_stream("pro")

    pipe = await svc.gateway.stream_chat(make_request("premium-chat", stream=True))
    _, err = await _consume(pipe)
    assert err is not None and err.code == "UPSTREAM_STREAM_INTERRUPTED"

    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "failed"
    assert t["error_code"] == "UPSTREAM_STREAM_INTERRUPTED"

    fake.set_truncated_stream("pro")
    pipe2 = await svc.gateway.stream_chat(make_request("premium-chat", stream=True))
    frames = [f async for f in to_sse(pipe2)]
    data = _data_lines(frames)
    payloads = [json.loads(d) for d in data if d != "[DONE]"]
    assert any("error" in p for p in payloads)
    assert data[-1] == "[DONE]"
