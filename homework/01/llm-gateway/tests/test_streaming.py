"""SSE：两种协议的上游事件都被归一为内部 StreamEvent 并逐块产出。"""
from __future__ import annotations

from app.core.streaming import DELTA, FINISH, START
from tests.conftest import make_request


async def _collect(iter_):
    events = []
    async for evt in iter_:
        events.append(evt)
    return events


async def test_anthropic_stream_normalized(harness):
    svc, _ = harness  # standard-chat -> flash(anthropic)
    pipe = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    events = await _collect(pipe)
    kinds = [e.type for e in events]
    assert START in kinds and DELTA in kinds and FINISH in kinds
    text = "".join(e.text for e in events if e.type == DELTA)
    assert text == "Hello"
    finish = next(e for e in events if e.type == FINISH)
    assert finish.resolved_model == "deepseek-v4-flash-20250801"
    assert finish.endpoint_id == "ds-v4-flash"
    assert finish.usage.output_tokens == 4


async def test_responses_stream_normalized(harness):
    svc, _ = harness  # premium-chat -> pro(responses)
    pipe = await svc.gateway.stream_chat(make_request("premium-chat", stream=True))
    events = await _collect(pipe)
    finish = next(e for e in events if e.type == FINISH)
    assert finish.resolved_model == "deepseek-v4-pro-250801"
    assert "".join(e.text for e in events if e.type == DELTA) == "Hello"
    assert finish.usage.reasoning_tokens == 1
