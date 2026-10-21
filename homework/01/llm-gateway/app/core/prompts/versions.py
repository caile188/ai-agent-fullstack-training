"""Prompt 即合约：一个可发布版本是可复现、可评估的行为契约，而非字符串。

不可变：发布后内容不可改，变更只能出新版本；版本由 (name, version) 唯一标识。
每次实际发送给模型的内容指纹（渲染后 system+user）在调用时计算并落 trace。
"""
from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.models import GenerationParams


class PromptStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class FewShotExample(BaseModel):
    """一条静态示范：可输入+输出成对，也可只有输入或只有输出，不带对话角色。"""

    model_config = ConfigDict(frozen=True)

    input: str | None = None
    output: str | None = None

    @model_validator(mode="after")
    def _require_one_side(self) -> "FewShotExample":
        if not (self.input or self.output):
            raise ValueError("few-shot example requires input or output")
        return self


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
    """完整行为契约：模板、变量契约、范例、输出 schema、默认模型与预算等。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    status: PromptStatus = PromptStatus.DRAFT
    system_template: str        # 长期规则（静态可信，不含运行时变量）
    user_template: str          # 本次任务与运行时数据（不可信变量只允许出现在这里）
    variables_schema: dict[str, Any] = Field(default_factory=dict)   # JSON Schema
    few_shot: list[FewShotExample] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    tool_versions: list[ToolVersionRef] = Field(default_factory=list)
    default_logical_model: str | None = None
    generation_params: GenerationParams = Field(default_factory=GenerationParams)
    context_budget: int | None = Field(default=None, gt=0)        # system+user 帧总 token 预算
    max_variable_chars: int = Field(default=20_000, gt=0)         # 单个变量的字符上限
    eval_set: list[EvalCase] = Field(default_factory=list)
    eval_threshold: float | None = Field(default=None, ge=0, le=1)
    changelog: str = ""
