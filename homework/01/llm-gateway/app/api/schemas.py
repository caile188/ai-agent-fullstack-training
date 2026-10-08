"""入站 / 出站 API 模型（OpenAI 兼容形态 + 网关 Prompt 扩展）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 治理子集：外部协议在 API Layer 终止，未知字段立即拒绝而非静默忽略
_GOVENED_MODEL = ConfigDict(extra="forbid")


class IncomingMessage(BaseModel):
    model_config = _GOVENED_MODEL

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[Any] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class IncomingTool(BaseModel):
    model_config = _GOVENED_MODEL

    type: str = "function"
    function: dict[str, Any]


class IncomingResponseFormat(BaseModel):
    model_config = _GOVENED_MODEL

    type: Literal["json_object", "json_schema"]
    json_schema: dict[str, Any] | None = None


class PromptRef(BaseModel):
    """通过已发布 Prompt 版本发起调用。"""

    model_config = _GOVENED_MODEL

    name: str
    version: str | None = None           # 缺省取当前 published
    variables: dict[str, Any] = Field(default_factory=dict)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    model: str                          # 逻辑模型；给 prompt 时可被默认模型覆盖
    messages: list[IncomingMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    tools: list[IncomingTool] | None = None
    response_format: IncomingResponseFormat | None = None
    prompt: PromptRef | None = None
    idempotency_key: str | None = Field(default=None, alias="idempotency_key")
