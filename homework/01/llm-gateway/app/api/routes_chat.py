"""统一对话接口：/v1/chat/completions，SSE 与非 SSE 同一入口。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.auth import resolve_tenant
from app.api.schemas import ChatCompletionRequest
from app.api.sse import to_sse
from app.core.errors import GatewayError

router = APIRouter()


def _services(request: Request):
    return request.app.state.services


def _gateway_namespace(resp) -> dict:
    """治理扩展字段独立命名空间，与 OpenAI 兼容字段隔离；None 值省略。"""
    gw = {
        "requested_model": resp.requested_model,
        "resolved_model": resp.resolved_model,
        "endpoint_id": resp.endpoint_id,
        "provider_id": resp.provider_id,
        "output_valid": resp.output_valid,
        "upstream_request_id": resp.upstream_request_id,
    }
    return {k: v for k, v in gw.items() if v is not None}


def _chat_completion_json(resp) -> dict:
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "model": resp.resolved_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": resp.text,
                    "tool_calls": [
                        {"id": t.id, "type": "function",
                         "function": {"name": t.name, "arguments": t.arguments}}
                        for t in resp.tool_calls
                    ],
                },
                "finish_reason": resp.finish_reason.value,
            }
        ],
        "usage": {
            "prompt_tokens": resp.usage.input_tokens,
            "completion_tokens": resp.usage.output_tokens,
            "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
            "cached_tokens": resp.usage.cached_tokens,
            "reasoning_tokens": resp.usage.reasoning_tokens,
        },
        "gateway": _gateway_namespace(resp),
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant),
):
    svc = _services(request)

    rl_result = await svc.rate_limiter.acquire(tenant_id, payload.model)
    if not rl_result.allowed:
        raise GatewayError(
            "RATE_LIMITED",
            f"rate limit exceeded for tenant {tenant_id} model {payload.model}",
            retry_after_ms=rl_result.retry_after_ms,
        )

    if payload.stream:
        event_iter = await svc.gateway.stream_chat(
            payload, tenant_id, is_disconnected=request.is_disconnected
        )
        return StreamingResponse(
            to_sse(event_iter),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    resp = await svc.gateway.chat(payload, tenant_id)
    return _chat_completion_json(resp)
