"""路由决策数据结构（Pydantic v2）：可解释、可持久化、可回放。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RejectedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint_id: str
    stage: str                       # capability | policy
    reason_code: str                 # 如 capability_missing:structured_output
    detail: str = ""


class CandidateScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint_id: str
    health: float = Field(ge=0, le=1)
    priority: int
    cost_estimate: float = 0.0
    load: float = Field(default=0.0, ge=0, le=1)
    total_score: float = 0.0
    breakdown: dict[str, float] = Field(default_factory=dict)


class ScoredCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint_id: str
    route_priority: int
    route_weight: int = 1
    score: CandidateScore | None = None


class RouteDecision(BaseModel):
    """一次路由的完整产物。chosen 为空表示无可用端点。"""

    logical_model: str
    required_capabilities: list[str] = Field(default_factory=list)
    tenant_id: str

    candidate_endpoint_ids: list[str] = Field(default_factory=list)
    rejections: list[RejectedCandidate] = Field(default_factory=list)
    survivors: list[ScoredCandidate] = Field(default_factory=list)
    chosen: ScoredCandidate | None = None

    @property
    def chosen_endpoint_id(self) -> str | None:
        return self.chosen.endpoint_id if self.chosen else None

    @property
    def fallback_chain(self) -> list[str]:
        """锁定的 fallback 顺序：执行阶段只能在此列表内切换。"""
        return [c.endpoint_id for c in self.survivors]

    def to_snapshot(self) -> dict:
        """完整决策快照（可落库 / 离线 Eval 回放）。"""
        return self.model_dump(mode="json")

    def snapshot_json(self) -> str:
        return self.model_dump_json()
