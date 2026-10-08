"""Prompt 即合约：一个可发布版本是可复现、可评估的行为契约，而非字符串。

不可变 + 内容寻址(content_hash)。发布后内容不可改，变更只能出新版本。
"""
from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.models import GenerationParams


class PromptStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class FewShotExample(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str = "user"
    content: str


class ToolVersionRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str


class EvalCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    variables: dict[str, Any] = Field(default_factory=dict)
    expect_json_schema: dict[str, Any] | None = None   # 输出须满足
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)


class PromptVersion(BaseModel):
    """完整行为契约。content_hash 由 hashing 计算覆盖全部影响行为的字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    status: PromptStatus = PromptStatus.DRAFT
    system_template: str
    variables_schema: dict[str, Any] = Field(default_factory=dict)   # JSON Schema
    few_shot: list[FewShotExample] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    tool_versions: list[ToolVersionRef] = Field(default_factory=list)
    default_logical_model: str | None = None
    generation_params: GenerationParams = Field(default_factory=GenerationParams)
    context_budget: int | None = Field(default=None, gt=0)
    eval_set: list[EvalCase] = Field(default_factory=list)
    eval_threshold: float | None = Field(default=None, ge=0, le=1)
    changelog: str = ""
    content_hash: str | None = None
