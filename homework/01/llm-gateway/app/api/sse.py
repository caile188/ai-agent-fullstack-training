"""出口 SSE 编码：内部 StreamEvent -> OpenAI Chat Completions 兼容 chunk 帧。

帧序列（OpenAI 兼容）：
  首帧 choices[0].delta.role=assistant（带实际 model 与 gateway 元数据）
  -> 若干 content / tool_calls delta 帧
  -> finish_reason 终帧
  -> usage 帧（choices=[]，与 OpenAI include_usage 形态一致）
  -> data: [DONE]
出错时只发一帧 {"error": {...}} 作为唯一错误终态，随后同样 [DONE]。
内部 USAGE 事件仅用于累计用量，不单独出帧。

每条业务帧带单调递增的 SSE 帧序号 ``id: {seq}``（从 0 起），供客户端去重，
也是未来 Last-Event-ID 断线重放的锚点；``data: [DONE]`` 是终止哨兵，不占序号。
无事件期间定时下发 ``: heartbeat`` 注释帧（不推进序号、不更新 lastEventId），
避免反代/CDN 因空闲关闭长连接。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.core.streaming import DELTA, ERROR, FINISH, START, TOOL_CALL, USAGE, StreamEvent

_HEARTBEAT_FRAME = ": heartbeat\n\n"
_DONE_FRAME = "data: [DONE]\n\n"


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


async def to_sse(
    events: AsyncIterator[StreamEvent],
    *,
    heartbeat_seconds: float | None = 15.0,
) -> AsyncIterator[str]:
    chunk_id = f"chatcmpl_{uuid.uuid4().hex[:24]}"
    model: str | None = None
    gateway: dict = {}
    latest_usage = None
    seq = 0

    # 事件泵：把上游事件搬到队列，主循环同时竞争"下一事件"与"心跳超时"，
    # 使心跳只在无任何事件的空闲间隙下发。事件生成器抛出的异常也经队列回传。
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def _pump() -> None:
        try:
            async for evt in events:
                await queue.put(("event", evt))
        except Exception as e:  # noqa: BLE001 - 交给主循环编码为唯一错误帧
            await queue.put(("error", e))
        finally:
            await queue.put(("end", None))

    pump_task = asyncio.create_task(_pump())

    def _numbered(data_frame: str) -> str:
        nonlocal seq
        frame = f"id: {seq}\n{data_frame}"
        seq += 1
        return frame

    wait_timeout = heartbeat_seconds if heartbeat_seconds and heartbeat_seconds > 0 else None
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                yield _HEARTBEAT_FRAME
                continue

            if kind == "end":
                break
            if kind == "error":
                code = getattr(payload, "code", "UPSTREAM_STREAM_INTERRUPTED")
                yield _numbered(_error_frame(code, str(payload)))
                yield _DONE_FRAME
                return

            evt: StreamEvent = payload
            if evt.type == START:
                model = evt.resolved_model or evt.requested_model
                gateway = _gateway_meta(evt)
                yield _numbered(_encode(_chunk(chunk_id, model, gateway=gateway, role="assistant")))

            elif evt.type == DELTA and evt.text:
                yield _numbered(_encode(_chunk(chunk_id, model, content=evt.text)))

            elif evt.type == TOOL_CALL and evt.tool_calls:
                tool_deltas = [
                    {"index": i, "id": t.id, "type": "function",
                     "function": {"name": t.name, "arguments": t.arguments}}
                    for i, t in enumerate(evt.tool_calls)
                ]
                yield _numbered(_encode(_chunk(chunk_id, model, tool_calls=tool_deltas)))

            elif evt.type == USAGE and evt.usage is not None:
                # 内部累计快照：OpenAI 用量只在末帧给出，此处不出帧
                latest_usage = evt.usage

            elif evt.type == FINISH:
                model = evt.resolved_model or model
                gateway = {**gateway, **_gateway_meta(evt)}
                yield _numbered(_encode(_chunk(
                    chunk_id, model, gateway=gateway, finish_reason=evt.finish_reason.value
                )))
                if evt.usage is not None:
                    latest_usage = evt.usage
                if latest_usage is not None:
                    yield _numbered(_usage_frame(chunk_id, model, latest_usage, gateway))

            elif evt.type == ERROR:
                yield _numbered(_error_frame(
                    evt.error_code or "UPSTREAM_STREAM_INTERRUPTED",
                    evt.error_message or "stream error",
                ))
                yield _DONE_FRAME
                return

        yield _DONE_FRAME
    except Exception as e:  # noqa: BLE001 - 编码期异常也以唯一一帧告知客户端
        yield _numbered(_error_frame(
            getattr(e, "code", "UPSTREAM_STREAM_INTERRUPTED"), str(e)
        ))
        yield _DONE_FRAME
    finally:
        # 客户端断连等导致生成器提前关闭：取消事件泵并关闭上游，释放其并发闸/连接。
        pump_task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await pump_task
        with contextlib.suppress(Exception):
            await events.aclose()
