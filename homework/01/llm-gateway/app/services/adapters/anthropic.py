"""Anthropic Messages SDK 适配器（anthropic.AsyncAnthropic，/v1/messages）。

用于 DeepSeek v4 flash。与 OpenAI Responses 差异点：
  - system 为顶层字符串；messages.content 为字符串或部件数组
  - 工具参数字段名为 input_schema；结构化输出无原生 json_schema，
    采用"提示约束 + 网关出口 JSON Schema 校验"
  - stop_reason: end_turn / max_tokens / tool_use
  - 流式事件：message_start / content_block_delta / message_delta / message_stop
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.config import EndpointConfig
from app.core.errors import GatewayError
from app.core.models import (
    ChatRequest,
    ChatResponse,
    FinishReason,
    Message,
    Role,
    TokenUsage,
    ToolCall,
)
from app.core.streaming import StreamEvent
from app.services.adapters.base import LLMAdapter


class AnthropicAdapter(LLMAdapter):
    protocol = "anthropic"

    # ---------------------------------------------------------------- 参数
    def build_kwargs(
        self, req: ChatRequest, endpoint: EndpointConfig, *, stream: bool
    ) -> dict:
        system_text, messages = self._messages_to_anthropic(req.messages)

        kwargs: dict[str, Any] = {
            "model": endpoint.actual_model,
            "max_tokens": req.params.max_output_tokens or endpoint.max_output_tokens,
            "messages": messages,
            "stream": stream,
        }
        if system_text:
            kwargs["system"] = system_text
        if req.params.temperature is not None:
            kwargs["temperature"] = req.params.temperature
        if req.params.top_p is not None:
            kwargs["top_p"] = req.params.top_p
        if req.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters_json_schema,
                }
                for t in req.tools
            ]

        # Anthropic 协议无原生结构化输出：第一层引导——按 response_format 注入输出
        # 约束（json_schema 时附 schema 摘要）；第二层保证仍由网关出口本地校验
        rf = req.params.response_format
        if rf is not None:
            kwargs["system"] = (
                (system_text + "\n" if system_text else "")
                + _json_guidance(rf.type, rf.json_schema)
            )

        if req.idempotency_key:
            kwargs["extra_headers"] = {"Idempotency-Key": req.idempotency_key}
        return kwargs

    @staticmethod
    def _messages_to_anthropic(messages: list[Message]) -> tuple[str | None, list[dict]]:
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                system_parts.append(m.content)
                continue
            if m.role == Role.TOOL:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
                continue
            content: str | list = m.content
            if m.tool_calls:
                parts = []
                if m.content:
                    parts.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    parts.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": json.loads(tc.arguments) if tc.arguments else {},
                        }
                    )
                content = parts
            out.append({"role": "assistant" if m.role == Role.ASSISTANT else "user", "content": content})
        return ("\n\n".join(system_parts) if system_parts else None), out

    # ---------------------------------------------------------------- 非流式
    async def complete(
        self, req: ChatRequest, endpoint: EndpointConfig, *, timeout: float
    ) -> ChatResponse:
        kwargs = self.build_kwargs(req, endpoint, stream=False)
        msg = await self.client.messages.create(timeout=timeout, **kwargs)
        return self._decode(msg, req=req, endpoint=endpoint)

    def _decode(
        self, msg: Any, *, req: ChatRequest, endpoint: EndpointConfig
    ) -> ChatResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in msg.content:
            btype = block.type
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=json.dumps(block.input, ensure_ascii=False),
                    )
                )

        return ChatResponse(
            text="".join(text_parts),
            requested_model=req.requested_model,
            resolved_model=getattr(msg, "model", None) or endpoint.actual_model,
            endpoint_id=endpoint.id,
            provider_id=self.provider.id,
            usage=self._map_usage(msg.usage),
            finish_reason=self._map_stop(getattr(msg, "stop_reason", None), bool(tool_calls)),
            tool_calls=tool_calls,
            upstream_request_id=getattr(msg, "id", None),
        )

    @staticmethod
    def _map_stop(stop_reason: str | None, has_tool: bool) -> FinishReason:
        if has_tool or stop_reason == "tool_use":
            return FinishReason.TOOL_CALLS
        mapping = {
            "end_turn": FinishReason.STOP,
            "max_tokens": FinishReason.LENGTH,
            "stop_sequence": FinishReason.STOP,
        }
        return mapping.get(stop_reason or "", FinishReason.ERROR)

    @staticmethod
    def _map_usage(u: Any) -> TokenUsage:
        if u is None:
            return TokenUsage()
        return TokenUsage(
            input_tokens=getattr(u, "input_tokens", 0),
            output_tokens=getattr(u, "output_tokens", 0),
            cached_tokens=getattr(u, "cache_read_input_tokens", 0),
            reasoning_tokens=0,
        )

    # ---------------------------------------------------------------- 流式
    async def open_stream(
        self, req: ChatRequest, endpoint: EndpointConfig, *, timeout: float
    ) -> AsyncIterator[StreamEvent]:
        kwargs = self.build_kwargs(req, endpoint, stream=True)
        yield StreamEvent.start(
            req.requested_model,
            resolved_model=endpoint.actual_model,
            endpoint_id=endpoint.id,
        )

        model = endpoint.actual_model
        upstream_id: str | None = None
        usage = TokenUsage()
        finish_reason = FinishReason.ERROR
        terminal_seen = False
        # index -> tool_use 累积
        tool_slots: dict[int, dict] = {}

        # async with 保证取消（TTFT 超时/客户端断连）时底层连接被释放
        async with self.client.messages.stream(timeout=timeout, **kwargs) as stream:
            async for evt in stream:
                etype = evt.type

                if etype == "message_start":
                    msg = evt.message
                    model = getattr(msg, "model", model)
                    upstream_id = getattr(msg, "id", None)
                    usage = self._map_usage(msg.usage)
                    # 上游此刻已给出 input/cached tokens：中途中断也要能据此计费
                    yield StreamEvent.usage_snapshot(usage)

                elif etype == "content_block_start":
                    block = evt.content_block
                    if block.type == "tool_use":
                        tool_slots[evt.index] = {
                            "id": block.id,
                            "name": block.name,
                            "partial": "",
                        }

                elif etype == "content_block_delta":
                    delta = evt.delta
                    dtype = delta.type
                    if dtype == "text_delta":
                        yield StreamEvent.delta(delta.text)
                    elif dtype == "input_json_delta":
                        slot = tool_slots.get(evt.index)
                        if slot is not None:
                            slot["partial"] += delta.partial_json

                elif etype == "content_block_stop":
                    slot = tool_slots.pop(evt.index, None)
                    if slot is not None:
                        yield StreamEvent.tool(
                            [ToolCall(id=slot["id"], name=slot["name"], arguments=slot["partial"] or "{}")]
                        )

                elif etype == "message_delta":
                    d = evt.delta
                    stop_reason = getattr(d, "stop_reason", None)
                    if stop_reason:
                        finish_reason = self._map_stop(stop_reason, bool(tool_slots))
                    u = getattr(evt, "usage", None)
                    if u is not None:
                        usage = TokenUsage(
                            input_tokens=usage.input_tokens,
                            output_tokens=getattr(u, "output_tokens", usage.output_tokens),
                            cached_tokens=usage.cached_tokens,
                        )
                        yield StreamEvent.usage_snapshot(usage)

                elif etype == "message_stop":
                    terminal_seen = True
                    yield StreamEvent.finish(
                        finish_reason,
                        resolved_model=model,
                        endpoint_id=endpoint.id,
                        usage=usage,
                        upstream_request_id=upstream_id,
                        requested_model=req.requested_model,
                    )

        # 上游在未发 message_stop 的情况下正常结束连接（截断/代理掐断且未报错）：
        # 连接读完不等于模型完成，必须显式判为中断而非静默成功。
        if not terminal_seen:
            raise GatewayError(
                "UPSTREAM_STREAM_INTERRUPTED",
                "stream closed before anthropic message_stop event",
            )

    # ---------------------------------------------------------------- 错误
    def translate_error(self, exc: Exception) -> GatewayError:
        if isinstance(exc, GatewayError):
            return exc
        if isinstance(exc, anthropic.APITimeoutError):
            return GatewayError("UPSTREAM_TIMEOUT", str(exc))
        if isinstance(exc, anthropic.APIConnectionError):
            return GatewayError("UPSTREAM_CONNECT_ERROR", str(exc))
        if isinstance(exc, anthropic.APIStatusError):
            body = exc.body if isinstance(exc.body, dict) else {}
            err = body.get("error", body)
            upstream_type = err.get("type", "upstream_error") if isinstance(err, dict) else "upstream_error"
            message = err.get("message", "") if isinstance(err, dict) else str(err)
            upstream_id = body.get("request_id") or exc.response.headers.get("request-id")
            return self.status_error(
                status_code=exc.status_code,
                message=message,
                upstream_type=str(upstream_type),
                headers=exc.response.headers,
                upstream_request_id=upstream_id,
            )
        return GatewayError("INTERNAL_ERROR", str(exc))


def _json_guidance(rf_type: str, json_schema: dict | None) -> str:
    """Anthropic 无原生结构化输出时的第一层提示引导（出口仍有本地 Schema 校验兜底）。"""
    if rf_type == "json_schema" and json_schema:
        return (
            "Respond with a single JSON object that strictly conforms to the "
            "following JSON Schema. Output only JSON, with no Markdown fences "
            "or explanation.\n"
            f"JSON Schema: {json.dumps(json_schema, ensure_ascii=False)}"
        )
    return "Respond with a single valid JSON object and no prose."
