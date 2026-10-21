"""落库记录模型（Pydantic v2），字段与 migrations 表对齐。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.models import CostBreakdown, TokenUsage
from app.services.execution.engine import AttemptRecord


class CallRecord(BaseModel):
    call_id: str
    request_id: str
    step_id: str
    attempt: int
    endpoint_id: str
    kind: str = "attempt"              # attempt | repair
    provider_id: str | None = None
    request_hash: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    wait_ms: float | None = None       # 并发闸 acquire 等待
    latency_ms: float | None = None    # 非流式=invoke 全程；流式=开始到 finish
    backoff_ms: float | None = None    # 本次调用前的退避睡眠
    ttft_ms: float | None = None       # 仅流式有值；非流式无首 token 概念
    http_status: int | None = None
    upstream_request_id: str | None = None
    error_code: str | None = None


class CostRecord(BaseModel):
    call_id: str
    request_id: str
    tenant_id: str
    cost: CostBreakdown
    currency: str = "USD"
    price_version: str = "config"


class TraceRecord(BaseModel):
    request_id: str
    tenant_id: str
    requested_model: str
    stream: bool = False

    provider_id: str | None = None
    endpoint_id: str | None = None
    resolved_model: str | None = None

    prompt_name: str | None = None
    prompt_version: str | None = None
    # 本次实际渲染内容（system 含 few-shot + user 数据帧）的指纹，非合约定义 hash
    prompt_hash: str | None = None
    # 记录格式版本（本行数据的结构版本），与 prompt/schema 无关
    record_schema_version: str = "1.0"

    queue_ms: float | None = None
    render_ms: float | None = None
    route_ms: float | None = None
    ttft_ms: float | None = None
    generation_ms: float | None = None
    total_ms: float | None = None
    backoff_ms: float | None = None    # 各 attempt 退避睡眠之和

    # 用量汇总：取成功 attempt（http 200 的最后一条）的 usage
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

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
