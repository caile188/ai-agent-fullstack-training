"""能力声明：Router 按能力匹配，而不是按模型名匹配。"""
from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict

from app.core.models import CostBreakdown, TokenUsage


class Capability(str, enum.Enum):
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"


class CapabilitySet(frozenset):
    """端点声明的不可变能力集合（纯集合语义，构造即校验枚举合法性）。"""

    def __new__(cls, *caps: "str | Capability") -> "CapabilitySet":
        return super().__new__(cls, {Capability(c) for c in caps})

    def supports_all(self, required: "CapabilitySet") -> bool:
        return set(required) <= set(self)

    def missing(self, required: "CapabilitySet") -> list[Capability]:
        return sorted(set(required) - set(self), key=lambda c: c.value)

    def values(self) -> list[str]:
        return sorted(c.value for c in self)


class Price(BaseModel):
    """单位：美元 / 百万 token。"""

    model_config = ConfigDict(frozen=True)

    input_per_1m: float = 0.0
    output_per_1m: float = 0.0
    cached_per_1m: float = 0.0

    def cost(self, usage: TokenUsage) -> CostBreakdown:
        return CostBreakdown(
            input_cost=usage.input_tokens * self.input_per_1m / 1_000_000,
            output_cost=usage.output_tokens * self.output_per_1m / 1_000_000,
            cached_cost=usage.cached_tokens * self.cached_per_1m / 1_000_000,
        )
