"""统一中性数据模型（Pydantic v2）：与任何供应商协议、与 FastAPI 都无关。

适配器负责：供应商请求/响应  <->  这些模型。

模型标识约定（三层，缺一不可，Trace 全链路都要带）：
  requested_model : 用户请求里的模型名（逻辑模型，如 premium-chat）
  resolved_model  : 供应商响应里实际返回的模型版本字符串（如 deepseek-v4-pro-2508）
  endpoint_id     : 网关内部端点标识（路由/熔断/记账的最小单元）
"""
from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Role(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolSpec(_Frozen):
    name: str
    description: str = ""
    parameters_json_schema: dict[str, Any] = Field(default_factory=dict)


class ResponseFormat(_Frozen):
    type: str                        # "json_object" | "json_schema"
    json_schema: dict[str, Any] | None = None
    strict: bool = True


class GenerationParams(_Frozen):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_output_tokens: int | None = Field(default=None, gt=0)
    response_format: ResponseFormat | None = None


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    arguments: str = "{}"            # 原始 JSON 字符串，交由上层解析


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    """网关内部统一请求（已经过 Prompt 渲染）。"""

    model_config = ConfigDict(extra="forbid")

    requested_model: str             # 用户请求的逻辑模型名
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    params: GenerationParams = Field(default_factory=GenerationParams)
    tools: list[ToolSpec] = Field(default_factory=list)
    tenant_id: str = "default"
    # Prompt 合约溯源
    prompt_name: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    # 幂等：贯穿重试与 fallback
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def logical_model(self) -> str:
        """别名：requested_model 即路由使用的逻辑模型。"""
        return self.requested_model

    @property
    def requires_structured_output(self) -> bool:
        return self.params.response_format is not None


# ---------------------------------------------------------------- 用量与成本
class TokenUsage(_Frozen):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    def merge(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


class CostBreakdown(_Frozen):
    input_cost: float = 0.0
    output_cost: float = 0.0
    cached_cost: float = 0.0

    @property
    def total(self) -> float:
        return self.input_cost + self.output_cost + self.cached_cost

    def as_dict(self) -> dict[str, float]:
        return {
            "input_cost": round(self.input_cost, 8),
            "output_cost": round(self.output_cost, 8),
            "cached_cost": round(self.cached_cost, 8),
            "total_cost": round(self.total, 8),
        }


# ---------------------------------------------------------------- 终态
class FinishReason(str, enum.Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class ChatResponse(BaseModel):
    """统一非流式响应。requested_model 与 resolved_model 必须同时保留。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    requested_model: str             # 用户请求的逻辑模型
    resolved_model: str              # 供应商实际返回的模型版本字符串
    endpoint_id: str                 # 网关内部端点
    provider_id: str
    usage: TokenUsage
    finish_reason: FinishReason
    tool_calls: list[ToolCall] = Field(default_factory=list)
    upstream_request_id: str | None = None
    output_valid: bool | None = None
    output_validation_error: str | None = None
