"""出口信封治理：非流式与 SSE 的网关扩展字段统一收进 gateway 命名空间。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.fake_upstream import FakeUpstream


def _client():
    from app.main import app

    c = TestClient(app)
    c.__enter__()
    c.app.state.services.engine._client = FakeUpstream().client()
    return c


def test_nonstream_extensions_under_gateway_namespace():
    c = _client()
    try:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "standard-chat",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        body = r.json()

        # 顶层只有 OpenAI 兼容字段
        assert body["object"] == "chat.completion"
        assert body["model"] == "deepseek-v4-flash-20250801"
        assert body["choices"][0]["message"]["content"]
        for leaked in ("requested_model", "endpoint_id", "provider_id",
                       "output_valid", "upstream_request_id"):
            assert leaked not in body

        # 治理扩展全部在独立 gateway 命名空间
        gw = body["gateway"]
        assert gw["requested_model"] == "standard-chat"
        assert gw["resolved_model"] == "deepseek-v4-flash-20250801"
        assert gw["endpoint_id"] == "ds-v4-flash"
        assert gw["provider_id"] == "deepseek-anthropic"

        # usage 标准三字段 + 扩展字段
        usage = body["usage"]
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
        assert "cached_tokens" in usage and "reasoning_tokens" in usage
    finally:
        c.__exit__(None, None, None)


def test_sse_openai_compatible_chunks_with_gateway_namespace():
    c = _client()
    try:
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "standard-chat", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            frames = []
            for line in r.iter_lines():
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    frames.append(json.loads(line[5:].strip()))

        # 首帧：assistant role + 实际模型 + gateway 元数据
        first = frames[0]
        assert first["object"] == "chat.completion.chunk"
        assert first["choices"][0]["delta"]["role"] == "assistant"
        assert first["model"] == "deepseek-v4-flash"
        assert first["gateway"]["requested_model"] == "standard-chat"
        assert first["gateway"]["endpoint_id"] == "ds-v4-flash"
        # 帧顶层无散落元数据
        for leaked in ("requested_model", "endpoint_id", "upstream_request_id"):
            assert all(leaked not in f for f in frames)

        # 内容帧
        content = "".join(
            f["choices"][0]["delta"].get("content", "")
            for f in frames if f.get("choices") and f["choices"][0]["delta"].get("content")
        )
        assert content == "Hello"

        # finish 终帧 + 独立 usage 帧（choices 为空）
        finish = next(
            f for f in frames
            if f.get("choices") and f["choices"][0]["finish_reason"] is not None
        )
        assert finish["choices"][0]["finish_reason"] == "stop"
        assert finish["model"] == "deepseek-v4-flash-20250801"
        usage_frame = next(f for f in frames if f.get("choices") == [])
        assert usage_frame["usage"]["total_tokens"] == (
            usage_frame["usage"]["prompt_tokens"] + usage_frame["usage"]["completion_tokens"]
        )
    finally:
        c.__exit__(None, None, None)
