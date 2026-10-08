"""成本记账：按端点配置价格把 usage 折算为分项成本。"""
from __future__ import annotations

from app.core.models import TokenUsage
from app.services.observability.records import CostRecord
from app.services.registry import AdapterRegistry


def price_usage(registry: AdapterRegistry, endpoint_id: str, usage: TokenUsage):
    return registry.price(endpoint_id).cost(usage)


def build_cost_record(
    registry: AdapterRegistry,
    endpoint_id: str,
    usage: TokenUsage,
    *,
    call_id: str,
    trace_id: str,
    tenant_id: str,
) -> CostRecord:
    breakdown = price_usage(registry, endpoint_id, usage)
    return CostRecord(
        call_id=call_id,
        trace_id=trace_id,
        tenant_id=tenant_id,
        cost=breakdown,
    )
