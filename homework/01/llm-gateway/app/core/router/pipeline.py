"""四步路由流水线：

  1) 候选展开：逻辑模型 -> 候选 endpoint
  2) 能力过滤：所需能力必须为端点能力子集，否则记录 capability_missing
  3) 策略过滤：供应商启用 / 区域 / 数据驻留 / 租户预算 / 熔断 / 并发余量
  4) 评分排序：健康、优先级、成本、负载，输出得分明细，锁定 fallback 链

每个被剔除候选都持久化拒绝原因；整条决策可离线回放。
"""
from __future__ import annotations

from app.config import GatewayConfig
from app.core.capabilities import Capability, CapabilitySet
from app.core.errors import GatewayError
from app.core.models import ChatRequest
from app.core.router.decision import (
    RejectedCandidate,
    RouteDecision,
    ScoredCandidate,
)
from app.core.router.policy import (
    BudgetReader,
    HealthReader,
    budget_remaining,
    region_allowed,
    residency_allowed,
)
from app.core.router.scoring import score_candidates
from app.core.router.selector import SmoothWeightedRR


def _required_capabilities(req: ChatRequest) -> CapabilitySet:
    caps: list[Capability] = []
    if req.stream:
        caps.append(Capability.STREAMING)
    if req.requires_structured_output:
        caps.append(Capability.STRUCTURED_OUTPUT)
    if req.tools:
        caps.append(Capability.TOOL_CALLING)
    return CapabilitySet(*caps)


def _estimate_cost_usd(req: ChatRequest, endpoint_id: str, config: GatewayConfig) -> float:
    """粗估单次调用成本用于排序：输入按字符/4，输出按 max_output_tokens。"""
    ep = config.endpoints[endpoint_id]
    price = ep.price_model()
    input_chars = sum(len(m.content) for m in req.messages)
    est_input_tokens = max(1, input_chars // 4)
    est_output_tokens = req.params.max_output_tokens or 512
    return (
        est_input_tokens * price.input_per_1m / 1_000_000
        + est_output_tokens * price.output_per_1m / 1_000_000
    )


class RouterPipeline:
    def __init__(
        self,
        config: GatewayConfig,
        health: HealthReader,
        budget: BudgetReader,
    ) -> None:
        self.config = config
        self.health = health
        self.budget = budget
        self.strategy = config.routing.strategy
        self._wrr = SmoothWeightedRR()

    def route(self, req: ChatRequest) -> RouteDecision:
        logical = req.requested_model
        lm = self.config.logical_models.get(logical)
        if lm is None:
            raise GatewayError(
                "ROUTE_MODEL_NOT_FOUND",
                f"unknown logical model: {logical}",
                details={"model": logical},
            )

        required = _required_capabilities(req)
        tenant = self.config.tenant(req.tenant_id)
        remaining_budget = budget_remaining(tenant, self.budget.spent_usd(req.tenant_id))

        decision = RouteDecision(
            logical_model=logical,
            required_capabilities=required.values(),
            tenant_id=req.tenant_id,
            candidate_endpoint_ids=[r.endpoint for r in lm.endpoints],
        )

        # ---- Step 3 前置：预算一旦耗尽，对所有候选记一次策略拒绝
        budget_ok = remaining_budget > 0

        survivors: list[str] = []
        route_priority: dict[str, int] = {}
        route_weight: dict[str, int] = {}

        for entry in lm.endpoints:
            eid = entry.endpoint
            ep = self.config.endpoints[eid]
            provider = self.config.provider_of(eid)
            route_priority[eid] = entry.priority
            route_weight[eid] = entry.weight

            # ---- Step 2 能力过滤
            caps = ep.capability_set
            missing = caps.missing(required)
            if missing:
                reason = "capability_missing:" + ",".join(m.value for m in missing)
                decision.rejections.append(
                    RejectedCandidate(endpoint_id=eid, stage="capability", reason_code=reason)
                )
                continue

            # ---- Step 3 策略过滤
            if not provider.enabled:
                decision.rejections.append(
                    RejectedCandidate(endpoint_id=eid, stage="policy", reason_code="provider_disabled")
                )
                continue
            if not region_allowed(ep, tenant):
                decision.rejections.append(
                    RejectedCandidate(
                        endpoint_id=eid,
                        stage="policy",
                        reason_code="region_not_allowed",
                        detail=f"endpoint regions={list(ep.regions)}",
                    )
                )
                continue
            if not residency_allowed(ep, tenant):
                decision.rejections.append(
                    RejectedCandidate(
                        endpoint_id=eid,
                        stage="policy",
                        reason_code="data_residency_violation",
                        detail=f"endpoint={ep.data_residency} tenant={tenant.data_residency}",
                    )
                )
                continue
            if not budget_ok:
                decision.rejections.append(
                    RejectedCandidate(
                        endpoint_id=eid,
                        stage="policy",
                        reason_code="tenant_budget_exhausted",
                        detail=f"limit={tenant.budget_limit_usd}",
                    )
                )
                continue
            if self.health.is_circuit_open(eid):
                decision.rejections.append(
                    RejectedCandidate(endpoint_id=eid, stage="policy", reason_code="circuit_open")
                )
                continue
            if not self.health.concurrency_available(eid):
                decision.rejections.append(
                    RejectedCandidate(endpoint_id=eid, stage="policy", reason_code="concurrency_full")
                )
                continue

            survivors.append(eid)

        # ---- Step 4 评分排序
        if survivors:
            cost_estimate = {eid: _estimate_cost_usd(req, eid, self.config) for eid in survivors}
            scores = score_candidates(survivors, route_priority, cost_estimate, self.config, self.health)
            ordered = [
                ScoredCandidate(
                    endpoint_id=s.endpoint_id,
                    route_priority=route_priority[s.endpoint_id],
                    route_weight=route_weight[s.endpoint_id],
                    score=s,
                )
                for s in scores
            ]
            decision.survivors = self._apply_strategy(logical, ordered)
            decision.chosen = decision.survivors[0]

        return decision

    def _apply_strategy(self, logical: str, ordered: list[ScoredCandidate]) -> list[ScoredCandidate]:
        if self.strategy != "weighted_round_robin":
            return ordered
        # 仅在最高优先级（priority 最小）候选池内做加权分摊；其余保持评分序作 fallback
        top_priority = min(c.route_priority for c in ordered)
        pool = [c for c in ordered if c.route_priority == top_priority]
        if len(pool) == 1:
            return ordered
        weights = {c.endpoint_id: c.route_weight for c in pool}
        chosen_id = self._wrr.pick(logical, weights)
        chosen = next(c for c in ordered if c.endpoint_id == chosen_id)
        rest = [c for c in ordered if c.endpoint_id != chosen_id]
        return [chosen, *rest]
