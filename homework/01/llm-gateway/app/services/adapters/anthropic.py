"""Anthropic Messages API 适配器（/v1/messages）。

用于 DeepSeek v4 flash。与 OpenAI Responses 差异点：
  - 鉴权：x-api_key 头 + anthropic-version，而不是 Bearer
  - system 为顶层字符串；messages.content 为字符串或部件数组
  - 工具参数字段名为 input_schema；结构化输出无原生 json_schema，
    采用"提示约束 + 网关出口 JSON Schema 校验"
  - stop_reason: end_turn / max_tokens / tool_use
  - SSE：message_start / content_block_delta / message_delta / message_stop
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

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(LLMAdapter):
    protocol = "anthropic"

    # ---------------------------------------------------------------- 请求
    def build_http(
        self, req: ChatRequest, endpoint: EndpointConfig, *, stream: bool
    ) -> PreparedRequest:
        system_text, messages = self._messages_to_anthropic(req.messages)

        body: dict = {
            "model": endpoint.actual_model,
            "max_tokens": req.params.max_output_tokens or endpoint.max_output_tokens,
            "messages": messages,
            "stream": stream,
        }
        if system_text:
            body["system"] = system_text
        if req.params.temperature is not None:
            body["temperature"] = req.params.temperature
        if req.params.top_p is not None:
            body["top_p"] = req.params.top_p
        if req.tools:
            body["tools"] = [
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
            body["system"] = (
                (system_text + "\n" if system_text else "")
                + _json_guidance(rf.type, rf.json_schema)
            )

        headers = {
            "x-api-key": self.provider.auth.api_key().get_secret_value(),
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            **self.provider.extra_headers,
        }
        if req.idempotency_key:
            headers["Idempotency-Key"] = req.idempotency_key
        return "POST", self._url(), headers, body

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
    def parse_json(
        self, raw: dict, *, req: ChatRequest, endpoint: EndpointConfig
    ) -> ChatResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in raw.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=json.dumps(block.get("input", {}), ensure_ascii=False),
                    )
                )

        return ChatResponse(
            text="".join(text_parts),
            requested_model=req.requested_model,
            resolved_model=raw.get("model", endpoint.actual_model),
            endpoint_id=endpoint.id,
            provider_id=self.provider.id,
            usage=self._map_usage(raw.get("usage")),
            finish_reason=self._map_stop(raw.get("stop_reason"), bool(tool_calls)),
            tool_calls=tool_calls,
            upstream_request_id=self._request_id(raw),
        )

    @staticmethod
    def _request_id(raw: dict) -> str | None:
        meta = raw.get("metadata") or {}
        return meta.get("request_id") or raw.get("id")

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
    def _map_usage(u: dict | None) -> TokenUsage:
        if not u:
            return TokenUsage()
        return TokenUsage(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cached_tokens=u.get("cache_read_input_tokens", 0),
            reasoning_tokens=0,
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

        model = endpoint.actual_model
        upstream_id: str | None = None
        usage = TokenUsage()
        finish_reason = FinishReason.ERROR
        # index -> tool_use 累积
        tool_slots: dict[int, dict] = {}

        async for payload in self.iter_sse_payloads(lines):
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "")

            if etype == "message_start":
                msg = evt.get("message", {})
                model = msg.get("model", model)
                upstream_id = msg.get("id")
                usage = self._map_usage(msg.get("usage"))
                # 上游此刻已给出 input/cached tokens：中途中断也要能据此计费
                yield StreamEvent.usage_snapshot(usage)

            elif etype == "content_block_start":
                block = evt.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_slots[evt.get("index")] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "partial": "",
                    }

            elif etype == "content_block_delta":
                delta = evt.get("delta", {})
                dtype = delta.get("type")
                if dtype == "text_delta":
                    yield StreamEvent.delta(delta.get("text", ""))
                elif dtype == "input_json_delta":
                    slot = tool_slots.get(evt.get("index"))
                    if slot is not None:
                        slot["partial"] += delta.get("partial_json", "")

            elif etype == "content_block_stop":
                slot = tool_slots.pop(evt.get("index"), None)
                if slot is not None:
                    yield StreamEvent.tool(
                        [ToolCall(id=slot["id"], name=slot["name"], arguments=slot["partial"] or "{}")]
                    )

            elif etype == "message_delta":
                d = evt.get("delta", {})
                if d.get("stop_reason"):
                    finish_reason = self._map_stop(d["stop_reason"], bool(tool_slots))
                u = evt.get("usage")
                if u:
                    usage = TokenUsage(
                        input_tokens=usage.input_tokens,
                        output_tokens=u.get("output_tokens", usage.output_tokens),
                        cached_tokens=usage.cached_tokens,
                    )
                    yield StreamEvent.usage_snapshot(usage)

            elif etype == "message_stop":
                yield StreamEvent.finish(
                    finish_reason,
                    resolved_model=model,
                    endpoint_id=endpoint.id,
                    usage=usage,
                    upstream_request_id=upstream_id,
                    requested_model=req.requested_model,
                )

            elif etype == "error":
                err = evt.get("error", {})
                yield StreamEvent.error(
                    "UPSTREAM_STREAM_INTERRUPTED", str(err.get("message", "stream error"))
                )

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
                data.get("request_id"),
            )
        return ("upstream_error", str(err), data.get("request_id"))


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
