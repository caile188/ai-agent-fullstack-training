"""错误治理：按调用生命周期分类分级的稳定错误码。

生命周期：
  认证 → 请求校验 → Prompt渲染 → 能力路由 → 排队
    → 建立连接 → 等待首Token → 持续生成 → 输出校验 → 记账

每个错误码固定：所属阶段 / HTTP 状态 / 是否可重试 / 是否可 fallback。
这样重试与 fallback 策略由错误分类驱动，而不是靠临时判断。
"""
from __future__ import annotations

import enum
import time
from email.utils import parsedate_to_datetime

from pydantic import BaseModel, ConfigDict


class LifecycleStage(str, enum.Enum):
    AUTH = "auth"
    VALIDATE = "validate"
    PROMPT_RENDER = "prompt_render"
    ROUTE = "route"
    QUEUE = "queue"
    CONNECT = "connect"
    TTFT = "ttft"
    GENERATE = "generate"
    OUTPUT_VALIDATE = "output_validate"
    LEDGER = "ledger"


class ErrorSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    stage: LifecycleStage
    http_status: int
    retryable: bool
    fallbackable: bool


def _es(code: str, stage: LifecycleStage, http_status: int, retryable: bool, fallbackable: bool) -> ErrorSpec:
    return ErrorSpec(
        code=code,
        stage=stage,
        http_status=http_status,
        retryable=retryable,
        fallbackable=fallbackable,
    )


# 稳定错误码表（code 即落库/对外的稳定标识，不复用 HTTP 杂讯文案）
SPECS: dict[str, ErrorSpec] = {
    # 认证
    "AUTH_MISSING_KEY": _es("AUTH_MISSING_KEY", LifecycleStage.AUTH, 401, False, False),
    "AUTH_INVALID_KEY": _es("AUTH_INVALID_KEY", LifecycleStage.AUTH, 401, False, False),
    "AUTH_FORBIDDEN": _es("AUTH_FORBIDDEN", LifecycleStage.AUTH, 403, False, False),
    # 请求校验
    "VALIDATION_BAD_REQUEST": _es("VALIDATION_BAD_REQUEST", LifecycleStage.VALIDATE, 400, False, False),
    "VALIDATION_UNSUPPORTED_CAPABILITY": _es("VALIDATION_UNSUPPORTED_CAPABILITY", LifecycleStage.VALIDATE, 400, False, False),
    "IDEMPOTENCY_CONFLICT": _es("IDEMPOTENCY_CONFLICT", LifecycleStage.VALIDATE, 409, False, False),
    "IDEMPOTENCY_IN_PROGRESS": _es("IDEMPOTENCY_IN_PROGRESS", LifecycleStage.VALIDATE, 409, False, False),
    # Prompt 渲染
    "PROMPT_NOT_FOUND": _es("PROMPT_NOT_FOUND", LifecycleStage.PROMPT_RENDER, 404, False, False),
    "PROMPT_VERSION_NOT_PUBLISHED": _es("PROMPT_VERSION_NOT_PUBLISHED", LifecycleStage.PROMPT_RENDER, 409, False, False),
    "PROMPT_VARIABLE_MISSING": _es("PROMPT_VARIABLE_MISSING", LifecycleStage.PROMPT_RENDER, 400, False, False),
    "PROMPT_VARIABLE_TYPE": _es("PROMPT_VARIABLE_TYPE", LifecycleStage.PROMPT_RENDER, 400, False, False),
    "PROMPT_CONTEXT_BUDGET_EXCEEDED": _es("PROMPT_CONTEXT_BUDGET_EXCEEDED", LifecycleStage.PROMPT_RENDER, 413, False, False),
    # 路由
    "ROUTE_MODEL_NOT_FOUND": _es("ROUTE_MODEL_NOT_FOUND", LifecycleStage.ROUTE, 404, False, False),
    "ROUTE_NO_CAPABLE_ENDPOINT": _es("ROUTE_NO_CAPABLE_ENDPOINT", LifecycleStage.ROUTE, 503, False, False),
    "ROUTE_POLICY_BLOCKED": _es("ROUTE_POLICY_BLOCKED", LifecycleStage.ROUTE, 503, False, False),
    "ROUTE_BUDGET_EXHAUSTED": _es("ROUTE_BUDGET_EXHAUSTED", LifecycleStage.ROUTE, 429, False, False),
    # 排队
    "QUEUE_TIMEOUT": _es("QUEUE_TIMEOUT", LifecycleStage.QUEUE, 503, True, True),
    "QUEUE_CONCURRENCY_LIMIT": _es("QUEUE_CONCURRENCY_LIMIT", LifecycleStage.QUEUE, 429, False, True),
    # 网关自身按租户限流：租户级，重试/fallback 都无意义，直接拒绝
    "RATE_LIMITED": _es("RATE_LIMITED", LifecycleStage.QUEUE, 429, False, False),
    # 建立连接
    "UPSTREAM_CONNECT_ERROR": _es("UPSTREAM_CONNECT_ERROR", LifecycleStage.CONNECT, 502, True, True),
    "UPSTREAM_TIMEOUT": _es("UPSTREAM_TIMEOUT", LifecycleStage.CONNECT, 504, True, True),
    "UPSTREAM_429": _es("UPSTREAM_429", LifecycleStage.CONNECT, 429, True, True),
    "UPSTREAM_5XX": _es("UPSTREAM_5XX", LifecycleStage.CONNECT, 502, True, True),
    "UPSTREAM_4XX": _es("UPSTREAM_4XX", LifecycleStage.CONNECT, 400, False, False),
    "UPSTREAM_AUTH_ERROR": _es("UPSTREAM_AUTH_ERROR", LifecycleStage.CONNECT, 401, False, False),
    # 等待首 token（流式特有边界：过了这里就不再 fallback）
    "UPSTREAM_TTFT_TIMEOUT": _es("UPSTREAM_TTFT_TIMEOUT", LifecycleStage.TTFT, 504, False, True),
    # 持续生成（中途断流不重试，避免重复收费/文本拼接）
    "UPSTREAM_STREAM_INTERRUPTED": _es("UPSTREAM_STREAM_INTERRUPTED", LifecycleStage.GENERATE, 502, False, False),
    # 输出校验
    "OUTPUT_INVALID_JSON": _es("OUTPUT_INVALID_JSON", LifecycleStage.OUTPUT_VALIDATE, 422, False, False),
    "OUTPUT_SCHEMA_VIOLATION": _es("OUTPUT_SCHEMA_VIOLATION", LifecycleStage.OUTPUT_VALIDATE, 422, False, False),
    # 记账
    "LEDGER_WRITE_FAILED": _es("LEDGER_WRITE_FAILED", LifecycleStage.LEDGER, 200, False, False),
    # 取消 / 兜底
    "CLIENT_CANCELLED": _es("CLIENT_CANCELLED", LifecycleStage.GENERATE, 499, False, False),
    "INTERNAL_ERROR": _es("INTERNAL_ERROR", LifecycleStage.VALIDATE, 500, False, False),
}


class GatewayError(Exception):
    """全链路统一异常。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        upstream_request_id: str | None = None,
        details: dict | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        spec = SPECS[code]
        self.spec = spec
        self.code = code
        self.message = message or code
        self.upstream_request_id = upstream_request_id
        self.details = details or {}
        # 上游 429 Retry-After 解析结果（毫秒）；None 表示上游未指示
        self.retry_after_ms = retry_after_ms
        super().__init__(f"[{code}] {self.message}")

    @property
    def http_status(self) -> int:
        return self.spec.http_status

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    @property
    def fallbackable(self) -> bool:
        return self.spec.fallbackable

    @property
    def stage(self) -> LifecycleStage:
        return self.spec.stage

    def to_dict(self) -> dict:
        error = {
            "code": self.code,
            "type": self.stage.value,
            "message": self.message,
            "details": self.details,
        }
        if self.retry_after_ms is not None:
            error["retry_after_ms"] = self.retry_after_ms
        return {"error": error}


def spec_for_http_status(status_code: int) -> str:
    """把上游 HTTP 状态映射到稳定错误码。"""
    if status_code == 429:
        return "UPSTREAM_429"
    if status_code in (401, 403):
        return "UPSTREAM_AUTH_ERROR"
    if status_code == 408:
        return "UPSTREAM_TIMEOUT"
    if 500 <= status_code < 600:
        return "UPSTREAM_5XX"
    return "UPSTREAM_4XX"


def parse_retry_after(value: str | None) -> int | None:
    """Retry-After 头 -> 毫秒。支持 delta-seconds 与 HTTP-date；无法解析返回 None。"""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value) * 1000
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    delta = (dt.timestamp() - time.time()) * 1000.0
    return max(0, int(delta))
