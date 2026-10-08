"""策略过滤：租户预算 / 区域 / 数据驻留 / 健康状态 / 并发余量。

静态策略来自 config；运行时状态（健康、负载、已花预算）通过 Protocol 注入，
保证 core 不依赖 services 的具体实现。
"""
from __future__ import annotations

from typing import Protocol

from app.config import EndpointConfig, TenantConfig


class HealthReader(Protocol):
    def is_circuit_open(self, endpoint_id: str) -> bool: ...

    def health_score(self, endpoint_id: str) -> float: ...

    def load_ratio(self, endpoint_id: str) -> float: ...      # 0~1

    def concurrency_available(self, endpoint_id: str) -> bool: ...


class BudgetReader(Protocol):
    def spent_usd(self, tenant_id: str) -> float: ...


def region_allowed(ep: EndpointConfig, tenant: TenantConfig) -> bool:
    if not tenant.allowed_regions:
        return True
    return any(r in tenant.allowed_regions for r in ep.regions)


def residency_allowed(ep: EndpointConfig, tenant: TenantConfig) -> bool:
    if tenant.data_residency == "any":
        return True
    # 要求数据留在境内，则端点也必须是境内驻留；global 端点视为可流动，不满足 cn 要求
    return ep.data_residency == tenant.data_residency


def budget_remaining(tenant: TenantConfig, spent: float) -> float:
    return max(0.0, tenant.budget_limit_usd - spent)
