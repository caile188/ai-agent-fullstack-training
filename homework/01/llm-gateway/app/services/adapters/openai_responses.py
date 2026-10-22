"""OpenAI Responses SDK 适配器（openai.AsyncOpenAI，/responses）。

用于 DeepSeek v4 pro。与 Chat Completions 不同：
  - system 走顶层 instructions；消息体为 input 数组（content 为部件数组）
  - 结构化输出走 text.format；工具是扁平 tools 数组
  - 流式事件：response.output_text.delta / response.output_item.done /
    response.completed 等
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import openai

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


class OpenAIResponsesAdapter(LLMAdapter):
    protocol = "openai_responses"

    # ---------------------------------------------------------------- 参数
    def build_kwargs(
        self, req: ChatRequest, endpoint: EndpointConfig, *, stream: bool
    ) -> dict:
        system_text, input_items = self._messages_to_input(req.messages)

        kwargs: dict[str, Any] = {
            "model": endpoint.actual_model,
            "input": input_items,
            "stream": stream,
            "store": False,
        }
        if system_text:
            kwargs["instructions"] = system_text

        p = req.params
        if p.temperature is not None:
            kwargs["temperature"] = p.temperature
        if p.top_p is not None:
            kwargs["top_p"] = p.top_p
        if p.max_output_tokens is not None:
            kwargs["max_output_tokens"] = p.max_output_tokens
        if p.response_format is not None:
            kwargs["text"] = {"format": self._map_format(p.response_format)}

        if req.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters_json_schema,
                }
                for t in req.tools
            ]

        if req.idempotency_key:
            kwargs["extra_headers"] = {"Idempotency-Key": req.idempotency_key}
        return kwargs

    @staticmethod
    def _map_format(rf) -> dict:
        if rf.type == "json_object":
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": rf.json_schema or {},
            "strict": rf.strict,
        }

    @staticmethod
    def _messages_to_input(messages: list[Message]) -> tuple[str | None, list[dict]]:
        system_parts: list[str] = []
        items: list[dict] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                system_parts.append(m.content)
                continue
            if m.role == Role.TOOL:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.tool_call_id,
                        "output": m.content,
                    }
                )
                continue
            item: dict = {"role": m.role.value, "content": []}
            if m.content:
                item["content"].append(
                    {"type": "input_text" if m.role == Role.USER else "output_text", "text": m.content}
                )
            if m.tool_calls:
                # 上一轮 assistant function_call 作为 content 部件补充
                for tc in m.tool_calls:
                    item["content"].append(
                        {"type": "function_call", "call_id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    )
            items.append(item)
        return ("\n\n".join(system_parts) if system_parts else None), items

    # ---------------------------------------------------------------- 非流式
    async def complete(
        self, req: ChatRequest, endpoint: EndpointConfig, *, timeout: float
    ) -> ChatResponse:
        kwargs = self.build_kwargs(req, endpoint, stream=False)
        resp = await self.client.responses.create(timeout=timeout, **kwargs)
        return self._decode(resp, req=req, endpoint=endpoint)

    def _decode(
        self, resp: Any, *, req: ChatRequest, endpoint: EndpointConfig
    ) -> ChatResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in resp.output:
            if item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        text_parts.append(part.text)
            elif item.type == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=item.call_id,
                        name=item.name,
                        arguments=item.arguments or "",
                    )
                )

        return ChatResponse(
            text="".join(text_parts),
            requested_model=req.requested_model,
            resolved_model=getattr(resp, "model", None) or endpoint.actual_model,
            endpoint_id=endpoint.id,
            provider_id=self.provider.id,
            usage=self._map_usage(getattr(resp, "usage", None)),
            finish_reason=self._map_status(getattr(resp, "status", None), resp, tool_calls),
            tool_calls=tool_calls,
            upstream_request_id=getattr(resp, "id", None),
        )

    @staticmethod
    def _map_status(status: str | None, resp: Any, tool_calls: list[ToolCall]) -> FinishReason:
        if tool_calls:
            return FinishReason.TOOL_CALLS
        if status == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", "")
            if reason == "max_output_tokens":
                return FinishReason.LENGTH
            return FinishReason.ERROR
        if status == "completed":
            return FinishReason.STOP
        return FinishReason.ERROR

    @staticmethod
    def _map_usage(u: Any) -> TokenUsage:
        if u is None:
            return TokenUsage()
        in_details = getattr(u, "input_tokens_details", None)
        out_details = getattr(u, "output_tokens_details", None)
        return TokenUsage(
            input_tokens=getattr(u, "input_tokens", 0),
            output_tokens=getattr(u, "output_tokens", 0),
            cached_tokens=getattr(in_details, "cached_tokens", 0),
            reasoning_tokens=getattr(out_details, "reasoning_tokens", 0),
        )

    # ---------------------------------------------------------------- 流式
    async def open_stream(
        self, req: ChatRequest, endpoint: EndpointConfig, *, timeout: float
    ) -> AsyncIterator[StreamEvent]:
        kwargs = self.build_kwargs(req, endpoint, stream=True)
        # SDK 的 .stream() 便捷方法内部固定 stream=True，签名不接受 stream 入参
        # （与 .create() 不同）；重复传入会抛 unexpected keyword argument 'stream'
        kwargs.pop("stream", None)
        yield StreamEvent.start(
            req.requested_model,
            resolved_model=endpoint.actual_model,
            endpoint_id=endpoint.id,
        )

        # async with 保证取消（TTFT 超时/客户端断连）时底层连接被释放
        terminal_seen = False
        async with self.client.responses.stream(timeout=timeout, **kwargs) as stream:
            async for evt in stream:
                etype = evt.type

                if etype == "response.output_text.delta":
                    yield StreamEvent.delta(evt.delta)

                elif etype == "response.output_item.done":
                    item = evt.item
                    if item.type == "function_call":
                        yield StreamEvent.tool(
                            [ToolCall(
                                id=item.call_id,
                                name=item.name,
                                arguments=item.arguments or "",
                            )]
                        )

                elif etype == "response.completed":
                    terminal_seen = True
                    resp = evt.response
                    tool_calls = []
                    for item in resp.output:
                        if item.type == "function_call":
                            tool_calls.append(
                                ToolCall(
                                    id=item.call_id,
                                    name=item.name,
                                    arguments=item.arguments or "",
                                )
                            )
                    final_usage = self._map_usage(resp.usage)
                    # Responses 协议仅在 completed 给用量：finish 前补一个累计快照
                    yield StreamEvent.usage_snapshot(final_usage)
                    yield StreamEvent.finish(
                        self._map_status(resp.status, resp, tool_calls),
                        resolved_model=resp.model or endpoint.actual_model,
                        endpoint_id=endpoint.id,
                        usage=final_usage,
                        upstream_request_id=resp.id,
                        requested_model=req.requested_model,
                    )

        # 上游在未发 response.completed 的情况下正常结束连接（截断/代理掐断且未报错）：
        # 连接读完不等于模型完成，必须显式判为中断而非静默成功。
        if not terminal_seen:
            raise GatewayError(
                "UPSTREAM_STREAM_INTERRUPTED",
                "stream closed before response.completed event",
            )

    # ---------------------------------------------------------------- 错误
    def translate_error(self, exc: Exception) -> GatewayError:
        if isinstance(exc, GatewayError):
            return exc
        if isinstance(exc, openai.APITimeoutError):
            return GatewayError("UPSTREAM_TIMEOUT", str(exc))
        if isinstance(exc, openai.APIConnectionError):
            return GatewayError("UPSTREAM_CONNECT_ERROR", str(exc))
        if isinstance(exc, openai.APIStatusError):
            body = exc.body if isinstance(exc.body, dict) else {}
            err = body.get("error", {})
            upstream_type = "upstream_error"
            message = ""
            if isinstance(err, dict):
                upstream_type = str(err.get("code") or err.get("type") or "upstream_error")
                message = str(err.get("message", ""))
            upstream_id = exc.response.headers.get("x-request-id")
            return self.status_error(
                status_code=exc.status_code,
                message=message,
                upstream_type=upstream_type,
                headers=exc.response.headers,
                upstream_request_id=upstream_id,
            )
        return GatewayError("INTERNAL_ERROR", str(exc))
