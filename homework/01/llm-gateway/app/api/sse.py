"""出口 SSE 编码：内部 StreamEvent -> OpenAI Chat Completions 兼容 chunk 帧。

帧序列（OpenAI 兼容）：
  首帧 choices[0].delta.role=assistant（带实际 model 与 gateway 元数据）
  -> 若干 content / tool_calls delta 帧
  -> finish_reason 终帧
  -> usage 帧（choices=[]，与 OpenAI include_usage 形态一致）
  -> data: [DONE]
出错时只发一帧 {"error": {...}} 作为唯一错误终态，随后同样 [DONE]。
内部 USAGE 事件仅用于累计用量，不单独出帧。
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from app.core.streaming import DELTA, ERROR, FINISH, START, TOOL_CALL, USAGE, StreamEvent


def _gateway_meta(evt: StreamEvent) -> dict:
    meta = {
        "requested_model": evt.requested_model,
        "resolved_model": evt.resolved_model,
        "endpoint_id": evt.endpoint_id,
        "upstream_request_id": evt.upstream_request_id,
    }
    return {k: v for k, v in meta.items() if v}


def _chunk(
    chunk_id: str,
    model: str | None,
    *,
    gateway: dict | None = None,
    finish_reason: str | None = None,
    **delta_fields,
) -> dict:
    chunk: dict = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta_fields, "finish_reason": finish_reason}],
    }
    if gateway:
        chunk["gateway"] = gateway
    return chunk


def _encode(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _error_frame(code: str, message: str) -> str:
    return _encode({"error": {"code": code, "message": message}})


def _usage_frame(chunk_id: str, model: str | None, usage, gateway: dict) -> str:
    return _encode({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.output_tokens,
            "cached_tokens": usage.cached_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
        },
        "gateway": gateway,
    })


async def to_sse(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    chunk_id = f"chatcmpl_{uuid.uuid4().hex[:24]}"
    model: str | None = None
    gateway: dict = {}
    latest_usage = None
    try:
        async for evt in events:
            if evt.type == START:
                model = evt.resolved_model or evt.requested_model
                gateway = _gateway_meta(evt)
                yield _encode(_chunk(chunk_id, model, gateway=gateway, role="assistant"))

            elif evt.type == DELTA and evt.text:
                yield _encode(_chunk(chunk_id, model, content=evt.text))

            elif evt.type == TOOL_CALL and evt.tool_calls:
                tool_deltas = [
                    {"index": i, "id": t.id, "type": "function",
                     "function": {"name": t.name, "arguments": t.arguments}}
                    for i, t in enumerate(evt.tool_calls)
                ]
                yield _encode(_chunk(chunk_id, model, tool_calls=tool_deltas))

            elif evt.type == USAGE and evt.usage is not None:
                # 内部累计快照：OpenAI 用量只在末帧给出，此处不出帧
                latest_usage = evt.usage

            elif evt.type == FINISH:
                model = evt.resolved_model or model
                gateway = {**gateway, **_gateway_meta(evt)}
                yield _encode(_chunk(
                    chunk_id, model, gateway=gateway, finish_reason=evt.finish_reason.value
                ))
                if evt.usage is not None:
                    latest_usage = evt.usage
                if latest_usage is not None:
                    yield _usage_frame(chunk_id, model, latest_usage, gateway)

            elif evt.type == ERROR:
                yield _error_frame(evt.error_code or "UPSTREAM_STREAM_INTERRUPTED",
                                   evt.error_message or "stream error")
        yield "data: [DONE]\n\n"
    except Exception as e:  # noqa: BLE001 - 流中错误也要以唯一一帧告知客户端
        code = getattr(e, "code", "UPSTREAM_STREAM_INTERRUPTED")
        yield _error_frame(code, str(e))
        yield "data: [DONE]\n\n"
