"""脱敏、成本计算与 Trace 字段口径。"""
from __future__ import annotations

from app.api.schemas import IncomingResponseFormat
from app.core.models import TokenUsage
from app.services.observability.cost import price_usage
from app.services.observability.redaction import fingerprint, redact, text_meta
from tests.conftest import make_request


def test_redact_secrets():
    assert "sk-abcdefgh123456" not in redact("token sk-abcdefgh123456 here")
    assert "[REDACTED]" in redact("Authorization: Bearer sk-abcdefgh123456")
    # 正文不入库：只留长度与指纹
    meta = text_meta("hello world")
    assert set(meta) == {"chars", "sha256"}
    assert meta["chars"] == 11
    assert fingerprint("hello world") == fingerprint("hello world")


async def test_cost_ledger_pricing(harness):
    svc, _ = harness
    # pro: input 4.50/1m, output 13.50/1m（价格以 gateway.yaml 为准）
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0)
    cost = price_usage(svc.registry, "ds-v4-pro", usage)
    assert round(cost.input_cost, 4) == 4.50
    assert round(cost.output_cost, 4) == 13.50
    assert round(cost.total, 4) == 18.00


# ------------------------------------------------------- Trace 字段口径
async def test_nonstream_trace_timing_and_call_fields(harness):
    svc, _ = harness
    await svc.gateway.chat(make_request("standard-chat"))
    t = (await svc.repo.recent_traces(1))[0]
    calls = await svc.repo.get_trace_calls(t["request_id"])
    assert len(calls) == 1
    c = calls[0]
    # queue_ms = 各 attempt 并发闸等待之和（假上游无竞争：极小但真实，不再是假段）
    assert t["queue_ms"] is not None
    assert abs(t["queue_ms"] - c["wait_ms"]) < 1.0
    assert t["queue_ms"] < 50
    # 非流式 generation_ms = 成功 attempt 的纯调用耗时；call 行 latency_ms 同源
    assert t["generation_ms"] is not None
    assert abs(t["generation_ms"] - c["latency_ms"]) < 1.0
    # 非流式没有首 token 概念：ttft 保持 NULL，不造假
    assert c["ttft_ms"] is None
    # 路由维度：call 行带供应商；普通调用 kind=attempt
    assert c["provider_id"] == "deepseek-anthropic"
    assert c["kind"] == "attempt"
    # 无重试/退避：汇总段为 0 或空
    assert t["backoff_ms"] is None
    assert c["backoff_ms"] is None
    # traces 行直接带成功 attempt 的用量汇总，无需 JOIN
    assert t["input_tokens"] == 8
    assert t["output_tokens"] == 4
    assert t["cached_tokens"] == 1


async def test_retry_trace_backoff_excluded_from_generation(harness):
    svc, fake = harness
    object.__setattr__(svc.config.resilience.retry, "max_retries", 1)
    # 退避基数压到 1ms：用例快，且 backoff_ms 仍可测为正值
    object.__setattr__(svc.config.resilience.retry, "backoff_base_ms", 1)
    object.__setattr__(svc.config.resilience.retry, "backoff_max_ms", 2)
    # flash 首次 500（可重试），同端点第二次成功
    fake.set_fail("flash", status=500, times=1)

    resp = await svc.gateway.chat(make_request("standard-chat"))
    assert resp.endpoint_id == "ds-v4-flash"
    t = (await svc.repo.recent_traces(1))[0]
    calls = await svc.repo.get_trace_calls(t["request_id"])
    assert len(calls) == 2
    success_call = next(c for c in calls if c["http_status"] == 200)
    failed_call = next(c for c in calls if c["http_status"] is None)
    # 退避有真实记录，且归属成功调用之前（第二条 call 行）
    assert t["backoff_ms"] > 0
    assert failed_call["backoff_ms"] is None
    assert abs(success_call["backoff_ms"] - t["backoff_ms"]) < 1.0
    assert success_call["latency_ms"] is not None
    # generation_ms 只含成功调用耗时：退避 + 失败调用耗时被排除
    assert abs(t["generation_ms"] - success_call["latency_ms"]) < 1.0
    assert t["retries"] == 1
    assert t["fallback_from"] is None
    # 用量取成功 attempt
    assert t["input_tokens"] == 8


async def test_repair_call_marked_explicit_kind(harness):
    svc, fake = harness
    fake.set_bad_json("flash", 1)
    req = make_request(
        "standard-json",
        response_format=IncomingResponseFormat(type="json_object"),
    )
    await svc.gateway.chat(req)
    t = (await svc.repo.recent_traces(1))[0]
    calls = await svc.repo.get_trace_calls(t["request_id"])
    assert len(calls) == 2
    assert [c["kind"] for c in calls] == ["attempt", "repair"]
    repair = calls[1]
    assert repair["provider_id"] == "deepseek-anthropic"
    assert repair["latency_ms"] is not None
    # repair 不混入引擎重试计数
    assert t["retries"] == 0
    assert t["structured_repairs"] == 1


async def test_stream_trace_has_ttft_and_latency(harness):
    svc, _ = harness
    pipe = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    async for _evt in pipe:
        pass
    t = (await svc.repo.recent_traces(1))[0]
    calls = await svc.repo.get_trace_calls(t["request_id"])
    live = next(c for c in calls if c["http_status"] == 200)
    assert live["ttft_ms"] is not None
    assert live["latency_ms"] is not None
    assert live["provider_id"] == "deepseek-anthropic"
    assert t["ttft_ms"] is not None
    assert t["generation_ms"] is not None
    assert t["input_tokens"] == 8
