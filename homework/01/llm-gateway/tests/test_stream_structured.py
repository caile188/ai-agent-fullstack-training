"""流式结构化输出已被网关禁令禁止：stream 与 response_format 互斥，请求校验期拒绝。"""
from __future__ import annotations

import pytest

from app.api.schemas import IncomingResponseFormat
from app.core.errors import GatewayError
from app.core.streaming import FINISH
from tests.conftest import make_request


async def _collect(iter_):
    return [evt async for evt in iter_]


async def test_stream_structured_valid_rejected(harness):
    svc, _ = harness
    with pytest.raises(GatewayError) as exc:
        await svc.gateway.stream_chat(
            make_request("standard-json", stream=True,
                         response_format=IncomingResponseFormat(type="json_object"))
        )
    assert exc.value.code == "VALIDATION_BAD_REQUEST"


async def test_stream_structured_invalid_rejected(harness):
    svc, fake = harness
    fake.set_bad_json("flash", 99)
    with pytest.raises(GatewayError) as exc:
        await svc.gateway.stream_chat(
            make_request("standard-json", stream=True,
                         response_format=IncomingResponseFormat(type="json_object"))
        )
    assert exc.value.code == "VALIDATION_BAD_REQUEST"


async def test_stream_without_format_skips_validation(harness):
    svc, _ = harness
    pipe = await svc.gateway.stream_chat(make_request("standard-chat", stream=True))
    events = await _collect(pipe)
    assert FINISH in [e.type for e in events]
    recent = await svc.repo.recent_traces(1)
    assert recent[0]["output_validation"] == "skipped:stream"
