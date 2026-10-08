"""OpenAI Responses API 适配器（/responses）。

用于 DeepSeek v4 pro。与 Chat Completions 不同：
  - 鉴权：Authorization: Bearer
  - system 走顶层 instructions；消息体为 input 数组（content 为部件数组）
  - 结构化输出走 text.format；工具是扁平 tools 数组
  - SSE 事件类型：response.output_text.delta / response.completed 等
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from app.config import EndpointConfig
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
from app.services.adapters.base import LLMAdapter, PreparedRequest


class OpenAIResponsesAdapter(LLMAdapter):
    protocol = "openai_responses"

    # ---------------------------------------------------------------- 请求
    def build_http(
        self, req: ChatRequest, endpoint: EndpointConfig, *, stream: bool
    ) -> PreparedRequest:
        system_text, input_items = self._messages_to_input(req.messages)

        body: dict = {
            "model": endpoint.actual_model,
            "input": input_items,
            "stream": stream,
            "store": False,
        }
        if system_text:
            body["instructions"] = system_text

        p = req.params
        if p.temperature is not None:
            body["temperature"] = p.temperature
        if p.top_p is not None:
            body["top_p"] = p.top_p
        if p.max_output_tokens is not None:
            body["max_output_tokens"] = p.max_output_tokens
        if p.response_format is not None:
            body["text"] = {"format": self._map_format(p.response_format)}

        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters_json_schema,
                }
                for t in req.tools
            ]

        headers = {
            "Authorization": f"Bearer {self.provider.auth.api_key().get_secret_value()}",
            "Content-Type": "application/json",
            **self.provider.extra_headers,
        }
        if req.idempotency_key:
            headers["Idempotency-Key"] = req.idempotency_key
        return "POST", self._url(), headers, body

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
            item: dict = {"role": m.role.value, "content": []}
            if m.content:
                item["content"].append(
                    {"type": "input_text" if m.role == Role.USER else "output_text", "text": m.content}
                )
            if m.tool_calls:
                # Responses 的 assistant function_call 作为独立 item，由 parse 侧处理更常见；
                # 请求侧这里把上一轮工具调用以 function_call 部件补充
                for tc in m.tool_calls:
                    item["content"].append(
                        {"type": "function_call", "call_id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    )
            if m.role == Role.TOOL:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.tool_call_id,
                        "output": m.content,
                    }
                )
            else:
                items.append(item)
        return ("\n\n".join(system_parts) if system_parts else None), items

    # ---------------------------------------------------------------- 非流式
    def parse_json(
        self, raw: dict, *, req: ChatRequest, endpoint: EndpointConfig
    ) -> ChatResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in raw.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text", ""))
            elif item.get("type") == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=item.get("call_id", ""),
                        name=item.get("name", ""),
                        arguments=item.get("arguments", ""),
                    )
                )

        finish = self._map_status(raw.get("status"), raw, tool_calls)
        return ChatResponse(
            text="".join(text_parts),
            requested_model=req.requested_model,
            resolved_model=raw.get("model", endpoint.actual_model),
            endpoint_id=endpoint.id,
            provider_id=self.provider.id,
            usage=self._map_usage(raw.get("usage")),
            finish_reason=finish,
            tool_calls=tool_calls,
            upstream_request_id=raw.get("id"),
        )

    @staticmethod
    def _map_status(status: str | None, raw: dict, tool_calls: list[ToolCall]) -> FinishReason:
        if tool_calls:
            return FinishReason.TOOL_CALLS
        if status == "incomplete":
            reason = (raw.get("incomplete_details") or {}).get("reason", "")
            if reason == "max_output_tokens":
                return FinishReason.LENGTH
            return FinishReason.ERROR
        if status == "completed":
            return FinishReason.STOP
        return FinishReason.ERROR

    @staticmethod
    def _map_usage(u: dict | None) -> TokenUsage:
        if not u:
            return TokenUsage()
        in_details = u.get("input_tokens_details") or {}
        out_details = u.get("output_tokens_details") or {}
        return TokenUsage(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cached_tokens=in_details.get("cached_tokens", 0),
            reasoning_tokens=out_details.get("reasoning_tokens", 0),
        )

    # ---------------------------------------------------------------- 流式
    async def parse_sse(
        self, lines: AsyncIterator[bytes], *, req: ChatRequest, endpoint: EndpointConfig
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent.start(
            req.requested_model,
            resolved_model=endpoint.actual_model,
            endpoint_id=endpoint.id,
        )

        pending_tool: dict[str, dict] = {}
        async for payload in self.iter_sse_payloads(lines):
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "")

            if etype == "response.output_text.delta":
                yield StreamEvent.delta(evt.get("delta", ""))

            elif etype == "response.function_call_arguments.delta":
                item_id = evt.get("item_id", "")
                slot = pending_tool.setdefault(item_id, {"args": ""})
                slot["args"] += evt.get("delta", "")

            elif etype == "response.output_item.done":
                item = evt.get("item", {})
                if item.get("type") == "function_call":
                    yield StreamEvent.tool(
                        [ToolCall(
                            id=item.get("call_id", ""),
                            name=item.get("name", ""),
                            arguments=item.get("arguments", ""),
                        )]
                    )

            elif etype == "response.completed":
                resp = evt.get("response", {})
                tool_calls = []
                for item in resp.get("output", []):
                    if item.get("type") == "function_call":
                        tool_calls.append(
                            ToolCall(
                                id=item.get("call_id", ""),
                                name=item.get("name", ""),
                                arguments=item.get("arguments", ""),
                            )
                        )
                final_usage = self._map_usage(resp.get("usage"))
                # Responses 协议仅在 completed 给用量：finish 前补一个累计快照
                yield StreamEvent.usage_snapshot(final_usage)
                yield StreamEvent.finish(
                    self._map_status(resp.get("status"), resp, tool_calls),
                    resolved_model=resp.get("model", endpoint.actual_model),
                    endpoint_id=endpoint.id,
                    usage=final_usage,
                    upstream_request_id=resp.get("id"),
                    requested_model=req.requested_model,
                )

            elif etype == "error":
                err = evt.get("error", {})
                yield StreamEvent.error("UPSTREAM_STREAM_INTERRUPTED", err.get("message", "stream error"))

    # ---------------------------------------------------------------- 错误
    def parse_error(self, status_code: int, raw: bytes) -> tuple[str, str, str | None]:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ("upstream_error", raw.decode("utf-8", "ignore")[:500], None)
        err = data.get("error", data)
        if isinstance(err, dict):
            return (
                str(err.get("type", "upstream_error")),
                str(err.get("message", "")),
                err.get("request_id") or data.get("request_id"),
            )
        return ("upstream_error", str(err), data.get("request_id"))
