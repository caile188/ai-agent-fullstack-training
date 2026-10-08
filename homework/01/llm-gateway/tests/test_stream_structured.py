"""流式结构化输出：中途不校验，终态对完整文本做一次出口校验；脏输出唯一错误终态且仍计费。"""
from __future__ import annotations

from app.api.schemas import IncomingResponseFormat
from app.core.streaming import DELTA, ERROR, FINISH
from tests.conftest import make_request


async def _collect(iter_):
    return [evt async for evt in iter_]


async def test_stream_structured_valid_accumulates_then_finish(harness):
    svc, fake = harness
    # flash 在 json_object 请求时 body 带 system，假上游流式回吐合法 JSON
    pipe = await svc.gateway.stream_chat(
        make_request("standard-json", stream=True,
                     response_format=IncomingResponseFormat(type="json_object"))
    )
    events = await _collect(pipe)
    kinds = [e.type for e in events]
    assert FINISH in kinds and ERROR not in kinds
    # 两个分片拼起来才是完整 JSON（中途不完整，绝不在中途校验）
    text = "".join(e.text for e in events if e.type == DELTA)
    import json
    payload = json.loads(text)
    assert payload["answer"] == "flash"

    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "success"
    assert t["output_validation"] == "pass"


async def test_stream_structured_invalid_single_error_terminal_still_billed(harness):
    svc, fake = harness
    fake.set_bad_json("flash", 99)  # 流式持续回吐不完整/非法 JSON
    pipe = await svc.gateway.stream_chat(
        make_request("standard-json", stream=True,
                     response_format=IncomingResponseFormat(type="json_object"))
    )
    events = await _collect(pipe)
    kinds = [e.type for e in events]
    assert ERROR in kinds
    assert FINISH not in kinds                      # 只有一个终态：error 取代 finish
    err = next(e for e in events if e.type == ERROR)
    assert err.error_code == "OUTPUT_INVALID_JSON"

    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["terminal_status"] == "failed"
    assert t["error_code"] == "OUTPUT_INVALID_JSON"
    # 脏输出已实际产出 token：call 与 cost 仍落账
    calls = await svc.repo.get_trace_calls(t["trace_id"])
    assert len(calls) == 1 and calls[0]["endpoint_id"] == "ds-v4-flash"
    async with svc.db.conn.execute(
        "SELECT COUNT(*) AS n FROM cost_ledger WHERE trace_id=?", (t["trace_id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 1


async def test_stream_without_format_skips_validation(harness):
    svc, _ = harness
    pipe = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    events = await _collect(pipe)
    assert FINISH in [e.type for e in events]
    recent = await svc.repo.recent_traces(1)
    assert recent[0]["output_validation"] == "skipped:stream"
