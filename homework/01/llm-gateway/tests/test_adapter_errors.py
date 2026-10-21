"""适配器异常翻译：SDK 异常 -> 网关稳定错误码 / Retry-After / upstream 类型。"""
from __future__ import annotations

from types import SimpleNamespace as NS

import anthropic
import openai

from tests.fake_upstream import FakeHeaders


def _status_error(mod, status: int, body: dict, headers: dict | None = None):
    response = NS(
        status_code=status,
        headers=FakeHeaders(headers or {}),
        request=NS(),
    )
    return mod.APIStatusError("upstream error", response=response, body=body)


async def test_anthropic_429_maps_with_retry_after(harness):
    svc, _ = harness
    adapter = svc.registry.adapter_for("ds-v4-flash")
    exc = _status_error(
        anthropic, 429,
        {"error": {"type": "rate_limit_error", "message": "slow down"}},
        {"retry-after": "0", "request-id": "req_abc"},
    )
    err = adapter.translate_error(exc)
    assert err.code == "UPSTREAM_429"
    assert err.retry_after_ms == 0
    assert err.details == {"upstream_type": "rate_limit_error"}
    assert err.upstream_request_id == "req_abc"


async def test_anthropic_timeout_and_connection(harness):
    svc, _ = harness
    adapter = svc.registry.adapter_for("ds-v4-flash")
    assert adapter.translate_error(anthropic.APITimeoutError(request=NS())).code == "UPSTREAM_TIMEOUT"
    conn_err = anthropic.APIConnectionError(request=NS())
    assert adapter.translate_error(conn_err).code == "UPSTREAM_CONNECT_ERROR"


async def test_openai_5xx_and_auth(harness):
    svc, _ = harness
    adapter = svc.registry.adapter_for("ds-v4-pro")
    err_500 = adapter.translate_error(_status_error(
        openai, 503, {"error": {"code": "server", "message": "boom"}}
    ))
    assert err_500.code == "UPSTREAM_5XX"
    assert err_500.details == {"upstream_type": "server"}

    err_401 = adapter.translate_error(_status_error(
        openai, 401, {"error": {"code": "invalid_api_key", "message": "bad key"}}
    ))
    assert err_401.code == "UPSTREAM_AUTH_ERROR"
    assert err_401.fallbackable is False


async def test_gateway_error_passes_through(harness):
    svc, _ = harness
    from app.core.errors import GatewayError

    adapter = svc.registry.adapter_for("ds-v4-flash")
    original = GatewayError("UPSTREAM_TTFT_TIMEOUT", "x")
    assert adapter.translate_error(original) is original
