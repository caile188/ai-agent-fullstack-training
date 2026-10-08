"""落库记录模型（Pydantic v2），字段与 migrations 表对齐。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.models import CostBreakdown, TokenUsage
from app.services.execution.engine import AttemptRecord


class CallRecord(BaseModel):
    call_id: str
    trace_id: str
    run_id: str
    step_id: str
    attempt: int
    endpoint_id: str
    request_hash: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    connect_ms: float | None = None
    ttft_ms: float | None = None
    http_status: int | None = None
    upstream_request_id: str | None = None
    error_code: str | None = None


class CostRecord(BaseModel):
    call_id: str
    trace_id: str
    tenant_id: str
    cost: CostBreakdown
    currency: str = "USD"
    price_version: str = "config"


class TraceRecord(BaseModel):
    trace_id: str
    run_id: str
    tenant_id: str
    requested_model: str
    stream: bool = False

    provider_id: str | None = None
    endpoint_id: str | None = None
    resolved_model: str | None = None

    prompt_name: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    schema_version: str = "1.0"

    queue_ms: float | None = None
    render_ms: float | None = None
    route_ms: float | None = None
    connect_ms: float | None = None
    ttft_ms: float | None = None
    generation_ms: float | None = None
    total_ms: float | None = None

    attempts: list[AttemptRecord] = Field(default_factory=list)
    retries: int = 0
    structured_repairs: int = 0
    fallback_from: str | None = None
    timeout_budget_ms: int | None = None

    finish_reason: str | None = None
    terminal_status: str = "success"      # success | failed | cancelled
    output_validation: str | None = None

    error_code: str | None = None
    error_stage: str | None = None
    http_status: int | None = None
    upstream_request_id: str | None = None

    route_snapshot: dict[str, Any] | None = None
