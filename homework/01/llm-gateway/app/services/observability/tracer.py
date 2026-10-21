"""这是一次请求的"秒表 +
  汇总器"：在请求各生命周期阶段打时间戳、收集每次尝试记录，最后在成功或失败时汇总成一个 TraceRecord（落库模型，records.py:37）。
  Tracer 本身只做内存计时和数据组装，不碰数据库——落库由 repositories 完成"""
from __future__ import annotations

import time

from app.core.errors import GatewayError
from app.core.models import ChatResponse, TokenUsage
from app.services.execution.engine import AttemptRecord
from app.services.observability.context import TraceContext
from app.services.observability.records import TraceRecord


class Tracer:
    def __init__(self, ctx: TraceContext, requested_model: str, stream: bool, timeout_budget_ms: int) -> None:
        self.ctx = ctx  # TraceContext：request_id/tenant_id
        self.requested_model = requested_model
        self.stream = stream
        self.t0 = time.monotonic()  # 请求起点
        self._marks: dict[str, float] = {"begin": self.t0}  # 各阶段时间戳表
        self.attempts: list[AttemptRecord] = []  # 执行引擎每次尝试的记录
        self.structured_repairs = 0  # 结构化修复次数
        self.timeout_budget_ms = timeout_budget_ms

    def mark(self, stage: str) -> None:
        """
        打时间点
        如 mark("render_start")、mark("render_done")、mark("routed")、mark("first_token")、mark("end")。
        所有分段耗时都由这些点相减得到
        单一时间源保证各段可拼接、不重叠
        """
        self._marks[stage] = time.monotonic()

    def close_render_segment(self) -> None:
        """渲染失败时也要闭合 render_start->render_done 段，便于记 render_ms。"""
        if "render_start" in self._marks and "render_done" not in self._marks:
            self.mark("render_done")

    def _ms(self, a: str, b: str) -> float | None:
        """
        算两个标记间的毫秒数
        """
        if a in self._marks and b in self._marks:
            return round((self._marks[b] - self._marks[a]) * 1000.0, 2)
        return None

    @property
    def total_ms(self) -> float:
        end = self._marks.get("end", time.monotonic())
        return round((end - self.t0) * 1000.0, 2)

    def _sum_attempt_field(self, name: str) -> float | None:
        """各 attempt 某计时字段之和；无尝试记录时为 None（该阶段尚未发生）。"""
        values = [getattr(a, name) for a in self.attempts if getattr(a, name) is not None]
        if not values:
            return None
        return round(sum(values), 2)

    def _success_attempt(self) -> AttemptRecord | None:
        """成功 attempt：http_status==200 的最后一条（可能是 repair）。"""
        for a in reversed(self.attempts):
            if a.http_status == 200:
                return a
        return None

    @staticmethod
    def _usage_tokens(usage: TokenUsage | None) -> tuple[int, int, int, int]:
        if usage is None:
            return 0, 0, 0, 0
        return (
            usage.input_tokens, usage.output_tokens,
            usage.cached_tokens, usage.reasoning_tokens,
        )

    def _last_ttft(self) -> float | None:
        ttfts = [a.ttft_ms for a in self.attempts if a.ttft_ms is not None]
        return ttfts[-1] if ttfts else None

    def _generation_ms(self, success: AttemptRecord | None) -> float | None:
        if self.stream:
            # 流式：首内容事件到 finish（在 gateway 收尾时打标）
            return self._ms("first_token", "end")
        # 非流式：成功 attempt 的纯调用耗时，不含闸等待、退避与失败尝试
        if success is not None and success.latency_ms is not None:
            return round(success.latency_ms, 2)
        return None

    def build_success(
        self,
        resp: ChatResponse | None,
        *,
        output_validation: str | None,
        prompt: tuple[str | None, str | None, str | None],
    ) -> TraceRecord:
        self.mark("end")
        last = self.attempts[-1] if self.attempts else None
        success = self._success_attempt()
        usage = success.usage if success is not None else None
        input_t, output_t, cached_t, reasoning_t = self._usage_tokens(usage)
        return TraceRecord(
            request_id=self.ctx.request_id,
            tenant_id=self.ctx.tenant_id,
            requested_model=self.requested_model,
            stream=self.stream,
            provider_id=resp.provider_id if resp else None,
            endpoint_id=(resp.endpoint_id if resp else None) or (last.endpoint_id if last else None),
            resolved_model=resp.resolved_model if resp else None,
            prompt_name=prompt[0], prompt_version=prompt[1], prompt_hash=prompt[2],
            queue_ms=self._sum_attempt_field("wait_ms"),
            render_ms=self._ms("render_start", "render_done"),
            route_ms=self._ms("render_done", "routed"),
            ttft_ms=self._last_ttft(),
            generation_ms=self._generation_ms(success),
            total_ms=self.total_ms,
            backoff_ms=self._sum_attempt_field("backoff_ms"),
            input_tokens=input_t, output_tokens=output_t,
            cached_tokens=cached_t, reasoning_tokens=reasoning_t,
            attempts=self.attempts,
            retries=sum(1 for a in self.attempts if a.retried and a.kind == "attempt"),
            structured_repairs=self.structured_repairs,
            fallback_from=last.fallback_from if last else None,
            timeout_budget_ms=self.timeout_budget_ms,
            finish_reason=resp.finish_reason.value if resp else None,
            terminal_status="success",
            output_validation=output_validation,
            upstream_request_id=resp.upstream_request_id if resp else None,
        )

    def build_error(
        self, err: GatewayError, *,
        prompt: tuple[str | None, str | None, str | None],
    ) -> TraceRecord:
        self.mark("end")
        last = self.attempts[-1] if self.attempts else None
        # 失败终态也可能已产生成功 attempt（如输出校验拒绝、门开后取消）：用量照实汇总
        success = self._success_attempt()
        input_t, output_t, cached_t, reasoning_t = self._usage_tokens(
            success.usage if success is not None else None
        )
        return TraceRecord(
            request_id=self.ctx.request_id,
            tenant_id=self.ctx.tenant_id,
            requested_model=self.requested_model,
            stream=self.stream,
            endpoint_id=last.endpoint_id if last else None,
            prompt_name=prompt[0], prompt_version=prompt[1], prompt_hash=prompt[2],
            queue_ms=self._sum_attempt_field("wait_ms"),
            render_ms=self._ms("render_start", "render_done"),
            route_ms=self._ms("render_done", "routed"),
            ttft_ms=None,
            total_ms=self.total_ms,
            backoff_ms=self._sum_attempt_field("backoff_ms"),
            input_tokens=input_t, output_tokens=output_t,
            cached_tokens=cached_t, reasoning_tokens=reasoning_t,
            attempts=self.attempts,
            retries=sum(1 for a in self.attempts if a.retried and a.kind == "attempt"),
            structured_repairs=self.structured_repairs,
            fallback_from=last.fallback_from if last else None,
            timeout_budget_ms=self.timeout_budget_ms,
            terminal_status="cancelled" if err.code == "CLIENT_CANCELLED" else "failed",
            error_code=err.code,
            error_stage=err.stage.value,
            http_status=err.http_status,
            upstream_request_id=err.upstream_request_id,
        )
