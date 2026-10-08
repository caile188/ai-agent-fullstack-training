"""两种协议各走一个适配器，并验证 requested/resolved 模型同时落 Trace。"""
from __future__ import annotations

from tests.conftest import make_request


async def test_premium_routes_to_responses_protocol(harness):
    svc, fake = harness
    resp = await svc.gateway.chat(make_request("premium-chat"))
    # premium-chat 首选 pro（OpenAI Responses 协议）
    assert resp.endpoint_id == "ds-v4-pro"
    assert resp.provider_id == "deepseek-responses"
    assert resp.requested_model == "premium-chat"
    assert resp.resolved_model == "deepseek-v4-pro-250801"
    assert resp.text == "pro-ok"
    assert resp.usage.input_tokens == 10 and resp.usage.cached_tokens == 2
    assert any(p.url.path.endswith("/responses") for p in fake.calls)


async def test_standard_routes_to_anthropic_protocol(harness):
    svc, fake = harness
    resp = await svc.gateway.chat(make_request("standard-chat"))
    # standard-chat 首选 flash（Anthropic Messages 协议）
    assert resp.endpoint_id == "ds-v4-flash"
    assert resp.provider_id == "deepseek-anthropic"
    assert resp.resolved_model == "deepseek-v4-flash-20250801"
    assert resp.text == "flash-ok"
    assert any("/anthropic/v1/messages" in p.url.path for p in fake.calls)


async def test_trace_persists_requested_and_resolved(harness):
    svc, _ = harness
    await svc.gateway.chat(make_request("premium-chat"))
    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["requested_model"] == "premium-chat"
    assert t["resolved_model"] == "deepseek-v4-pro-250801"
    assert t["endpoint_id"] == "ds-v4-pro"
    assert t["finish_reason"] == "stop"
    calls = await svc.repo.get_trace_calls(t["trace_id"])
    assert len(calls) == 1 and calls[0]["output_tokens"] == 5
    assert t["trace_id"]
