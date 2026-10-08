"""非流式结构化输出 repair 闭环：仅非流式、锁定同端点、额外调用独立计费。"""
from __future__ import annotations

import pytest

from app.api.schemas import IncomingResponseFormat
from app.core.errors import GatewayError
from tests.conftest import make_request


async def _cost_rows(svc, trace_id: str) -> list[dict]:
    async with svc.db.conn.execute(
        "SELECT * FROM cost_ledger WHERE trace_id=? ORDER BY id", (trace_id,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def test_repair_then_pass(harness):
    svc, fake = harness
    # standard-json 首选 flash(anthropic)：首次返回非法 JSON，repair 后返回合法 JSON
    fake.set_bad_json("flash", 1)
    req = make_request(
        "standard-json",
        response_format=IncomingResponseFormat(type="json_object"),
    )
    resp = await svc.gateway.chat(req)

    assert resp.output_valid is True
    # 两次真实上游调用：初次 + 一次 repair，且都落在同一端点（未 fallback）
    assert len(fake.calls) == 2
    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "success"
    assert t["output_validation"] == "pass_after_repair_1"
    assert t["structured_repairs"] == 1
    assert t["endpoint_id"] == "ds-v4-flash"
    assert t["fallback_from"] is None

    calls = await svc.repo.get_trace_calls(t["trace_id"])
    assert len(calls) == 2
    assert {c["endpoint_id"] for c in calls} == {"ds-v4-flash"}
    # 两次调用都产生了 token，故都进入成本台账
    costs = await _cost_rows(svc, t["trace_id"])
    assert len(costs) == 2


async def test_repair_exhausted_still_rejects_and_bills(harness):
    svc, fake = harness

    # 持续返回纯文本：覆盖文本逻辑，repair 次数用尽后仍非法
    def bad_text(key, body):
        return "not a json body"

    fake._text_for = staticmethod(bad_text)  # type: ignore[assignment]
    req = make_request(
        "standard-json",
        response_format=IncomingResponseFormat(type="json_object"),
    )
    with pytest.raises(GatewayError) as ei:
        await svc.gateway.chat(req)
    assert ei.value.code == "OUTPUT_INVALID_JSON"

    # 默认 structured_output_retries=1 => 初次 + 1 次 repair
    assert len(fake.calls) == 2
    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "failed"
    assert t["structured_repairs"] == 1
    # 即使最终失败，已产生的上游调用与费用仍落库
    calls = await svc.repo.get_trace_calls(t["trace_id"])
    assert len(calls) == 2
    costs = await _cost_rows(svc, t["trace_id"])
    assert len(costs) == 2
