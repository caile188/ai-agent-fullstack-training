"""统一内部流式事件协议（Pydantic v2）。

适配器把各家 SSE 翻译为这些中性事件；api/sse.py 再编码为出口 SSE 帧。
协议差异被收敛在适配器层，韧性/记账逻辑只认内部事件。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import FinishReason, TokenUsage, ToolCall

# 事件类型
START = "start"
DELTA = "delta"
TOOL_CALL = "tool_call"
USAGE = "usage"
FINISH = "finish"
ERROR = "error"

EventType = Literal["start", "delta", "tool_call", "usage", "finish", "error"]

# TTFT（首 token）判定事件：delta / tool_call 均视为已有首输出
# （usage 不是内容，不参与首包门判定）
FIRST_OUTPUT_TYPES = frozenset({DELTA, TOOL_CALL})


class StreamEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EventType
    # delta
    text: str = ""
    # finish
    finish_reason: FinishReason | None = None
    usage: TokenUsage | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # 模型三层标识
    requested_model: str | None = None
    resolved_model: str | None = None
    endpoint_id: str | None = None
    upstream_request_id: str | None = None
    # error
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None

    @staticmethod
    def start(
        requested_model: str,
        *,
        resolved_model: str | None = None,
        endpoint_id: str | None = None,
    ) -> "StreamEvent":
        # 流开始时尚未读取上游字节，resolved_model 为配置中的实际模型名
        # （endpoint.actual_model）；上游回传的精确版本串要到 finish 帧才有
        return StreamEvent(
            type=START,
            requested_model=requested_model,
            resolved_model=resolved_model,
            endpoint_id=endpoint_id,
        )

    @staticmethod
    def delta(text: str) -> "StreamEvent":
        return StreamEvent(type=DELTA, text=text)

    @staticmethod
    def tool(tool_calls: list[ToolCall]) -> "StreamEvent":
        return StreamEvent(type=TOOL_CALL, tool_calls=tool_calls)

    @staticmethod
    def usage_snapshot(usage: TokenUsage) -> "StreamEvent":
        # 累计快照语义：上游随时可能分片段给出用量（如 Anthropic message_start
        # 只给 input tokens），门开后中断时网关据此对已处理 token 计费
        return StreamEvent(type=USAGE, usage=usage)

    @staticmethod
    def finish(
        finish_reason: FinishReason,
        *,
        resolved_model: str,
        endpoint_id: str,
        usage: TokenUsage | None = None,
        upstream_request_id: str | None = None,
        requested_model: str | None = None,
    ) -> "StreamEvent":
        return StreamEvent(
            type=FINISH,
            finish_reason=finish_reason,
            resolved_model=resolved_model,
            endpoint_id=endpoint_id,
            usage=usage,
            upstream_request_id=upstream_request_id,
            requested_model=requested_model,
        )

    @staticmethod
    def error(code: str, message: str) -> "StreamEvent":
        return StreamEvent(type=ERROR, error_code=code, error_message=message)
