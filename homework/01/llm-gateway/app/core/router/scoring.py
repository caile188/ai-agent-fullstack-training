"""明确评分：健康 / 优先级 / 成本 / 负载 加权，输出各因子明细。

评分原则（不是选参数量最大的模型）：
  越健康、优先级数字越小、成本越低、负载越低 -> 得分越高。
"""
from __future__ import annotations

from app.config import GatewayConfig, ScoringConfig
from app.core.router.decision import CandidateScore
from app.core.router.policy import HealthReader


def _norm(values: list[float], value: float, *, lower_is_better: bool) -> float:
    if not values:
        return 0.0
    lo, hi = min(values), max(values)
    if hi == lo:
        return 0.0 if lower_is_better else 1.0
    ratio = (value - lo) / (hi - lo)
    return ratio if not lower_is_better else 1.0 - ratio


def score_candidates(
    endpoint_ids: list[str],
    route_priority: dict[str, int],
    cost_estimate: dict[str, float],
    config: GatewayConfig,
    health: HealthReader,
) -> list[CandidateScore]:
    weights: ScoringConfig = config.scoring

    priorities = [route_priority[e] for e in endpoint_ids]
    costs = [cost_estimate.get(e, 0.0) for e in endpoint_ids]

    results: list[CandidateScore] = []
    for eid in endpoint_ids:
        health_v = health.health_score(eid)
        load_v = health.load_ratio(eid)
        prio_v = _norm([float(p) for p in priorities], float(route_priority[eid]), lower_is_better=True)
        cost_v = _norm(costs, cost_estimate.get(eid, 0.0), lower_is_better=True)

        total = (
            weights.weight_health * health_v
            + weights.weight_priority * prio_v
            + weights.weight_cost * cost_v
            + weights.weight_load * (1.0 - load_v)
        )
        results.append(
            CandidateScore(
                endpoint_id=eid,
                health=round(health_v, 4),
                priority=route_priority[eid],
                cost_estimate=round(cost_estimate.get(eid, 0.0), 8),
                load=round(load_v, 4),
                total_score=round(total, 6),
                breakdown={
                    "health_term": round(weights.weight_health * health_v, 6),
                    "priority_term": round(weights.weight_priority * prio_v, 6),
                    "cost_term": round(weights.weight_cost * cost_v, 6),
                    "load_term": round(weights.weight_load * (1.0 - load_v), 6),
                },
            )
        )
    # 分数降序；同分按配置 priority 升序兜底，保证确定性
    results.sort(key=lambda s: (-s.total_score, s.priority, s.endpoint_id))
    return results
