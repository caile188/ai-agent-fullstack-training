"""Structured Streaming 已被网关禁令禁止：stream + response_format 在请求校验期拒绝。"""
from __future__ import annotations

import pytest

from app.api.schemas import IncomingResponseFormat
from app.core.errors import GatewayError
from tests.conftest import make_request


async def test_broken_stream_request_rejected_before_upstream(harness):
    svc, _ = harness

    with pytest.raises(GatewayError) as exc:
        await svc.gateway.stream_chat(
            make_request("standard-json", stream=True,
                         response_format=IncomingResponseFormat(type="json_object"))
        )
    assert exc.value.code == "VALIDATION_BAD_REQUEST"
