"""结构化输出第一层保证：Anthropic 协议无原生 schema 支持时的提示引导。

出口本地 Schema 校验是第二层；第一层确保供应商在被请求时就拿到输出约束，
而不是零引导地依赖事后 repair（提高成功率、降低额外计费）。
"""
from __future__ import annotations

from app.core.models import (
    ChatRequest,
    GenerationParams,
    Message,
    ResponseFormat,
    Role,
)


def _request(rf: ResponseFormat) -> ChatRequest:
    return ChatRequest(
        requested_model="standard-json",
        messages=[Message(role=Role.USER, content="return json")],
        params=GenerationParams(response_format=rf),
    )


async def test_anthropic_json_schema_injected_into_system(harness):
    svc, fake = harness
    adapter = svc.registry.adapter_for("ds-v4-flash")
    endpoint = svc.registry.endpoint("ds-v4-flash")
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    _, _, _, body = adapter.build_http(
        _request(ResponseFormat(type="json_schema", json_schema=schema)),
        endpoint,
        stream=False,
    )
    assert "JSON Schema" in body["system"]
    assert "answer" in body["system"]


async def test_anthropic_json_object_guidance_unchanged(harness):
    svc, fake = harness
    adapter = svc.registry.adapter_for("ds-v4-flash")
    endpoint = svc.registry.endpoint("ds-v4-flash")
    _, _, _, body = adapter.build_http(
        _request(ResponseFormat(type="json_object")), endpoint, stream=False
    )
    assert body["system"] == "Respond with a single valid JSON object and no prose."


async def test_responses_protocol_uses_native_text_format(harness):
    # 对照：Responses 协议走原生 text.format，不往 instructions 塞 schema
    svc, _ = harness
    adapter = svc.registry.adapter_for("ds-v4-pro")
    endpoint = svc.registry.endpoint("ds-v4-pro")
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    _, _, _, body = adapter.build_http(
        _request(ResponseFormat(type="json_schema", json_schema=schema)),
        endpoint,
        stream=False,
    )
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["json_schema"] == schema
    assert "instructions" not in body
