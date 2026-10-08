"""总编排：认证→校验→Prompt渲染→路由→排队执行→输出校验→记账。

串起 Router / ExecutionEngine / PromptStore / Trace / Cost / Repository，
把可观测与韧性约束落到一次调用的完整生命周期上。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.api.schemas import ChatCompletionRequest
from app.config import GatewayConfig
from app.core.errors import GatewayError
from app.core.models import (
    ChatRequest,
    ChatResponse,
    GenerationParams,
    Message,
    ResponseFormat,
    Role,
    ToolSpec,
)
from app.core.prompts.render import render_system
from app.core.prompts.versions import PromptVersion
from app.core.router.decision import RouteDecision
from app.core.router.pipeline import RouterPipeline
from app.core.streaming import DELTA, ERROR, FINISH, USAGE, StreamEvent
from app.core.validation.structured import repair_instruction, strip_fence, validate_output
from app.core.validation.streaming_json import JsonPrefix, classify_json_prefix
from app.services.db.repositories import Repository
from app.services.execution.budget import TimeoutBudget
from app.services.execution.concurrency import ConcurrencyLimiter
from app.services.execution.engine import AttemptRecord, ExecutionEngine
from app.services.execution.health import CircuitBreaker
from app.services.execution.runtime import RuntimeHealth
from app.services.observability.context import TraceContext, new_call_id
from app.services.observability.cost import build_cost_record
from app.services.observability.pricing import PriceCatalog
from app.services.observability.records import CallRecord, TraceRecord
from app.services.observability.redaction import fingerprint
from app.services.observability.tracer import Tracer
from app.services.prompts_store import PromptStore
from app.services.registry import AdapterRegistry


class GatewayService:
    def __init__(
        self,
        config: GatewayConfig,
        repo: Repository,
        registry: AdapterRegistry,
        engine: ExecutionEngine,
        breaker: CircuitBreaker,
        limiter: ConcurrencyLimiter,
        prompts: PromptStore,
    ) -> None:
        self.config = config
        self.repo = repo
        self.registry = registry
        self.engine = engine
        self.breaker = breaker
        self.limiter = limiter
        self.prompts = prompts
        self.health = RuntimeHealth(breaker, limiter)
        self.pricing = PriceCatalog(config)
        self.router = RouterPipeline(config, self.health, _AsyncBudget(repo))

    # ==================================================== 请求准备
    async def prepare(self, payload: ChatCompletionRequest, tenant_id: str) -> tuple[ChatRequest, PromptVersion | None]:
        model = payload.model
        params_kwargs: dict[str, Any] = {
            "temperature": payload.temperature,
            "top_p": payload.top_p,
            "max_output_tokens": payload.max_tokens,
        }
        prompt_version: PromptVersion | None = None

        if payload.prompt:
            # Prompt 合约独占 system：禁止调用方再用 messages 塞第二套 system prompt
            if any(m.role == "system" for m in payload.messages):
                raise GatewayError(
                    "VALIDATION_BAD_REQUEST",
                    "must not supply system messages when a prompt template is used; "
                    "system prompt is owned by the prompt contract",
                    details={"reason": "system_message_conflicts_with_prompt"},
                )
            prompt_version = await self.prompts.resolve(payload.prompt.name, payload.prompt.version)
            system_text = render_system(prompt_version, payload.prompt.variables)
            messages = [Message(role=Role.SYSTEM, content=system_text)]
            # default_logical_model 仅作兜底：调用方显式传模型时尊重调用方；
            # 传占位值 "auto" 表示交由 Prompt 的默认模型决定
            if model == "auto":
                if prompt_version.default_logical_model:
                    model = prompt_version.default_logical_model
                else:
                    raise GatewayError(
                        "VALIDATION_BAD_REQUEST",
                        "model='auto' requires the prompt to declare default_logical_model",
                        details={"reason": "prompt_without_default_model"},
                    )
            # 生成参数：请求显式值覆盖 prompt 默认
            gp = prompt_version.generation_params
            for key in ("temperature", "top_p", "max_output_tokens"):
                if params_kwargs[key] is None:
                    params_kwargs[key] = getattr(gp, key)
            if prompt_version.output_schema and payload.response_format is None:
                params_kwargs["response_format"] = ResponseFormat(
                    type="json_schema", json_schema=prompt_version.output_schema
                )
        else:
            messages = []

        messages.extend(_convert_messages(payload))

        if payload.response_format and params_kwargs.get("response_format") is None:
            params_kwargs["response_format"] = ResponseFormat(
                type=payload.response_format.type,
                json_schema=payload.response_format.json_schema,
            )

        tools: list[ToolSpec] = []
        for t in payload.tools or []:
            fn = t.function
            tools.append(ToolSpec(
                name=fn.get("name", ""),
                description=fn.get("description", ""),
                parameters_json_schema=fn.get("parameters", {}),
            ))

        req = ChatRequest(
            requested_model=model,
            messages=messages,
            stream=payload.stream,
            params=GenerationParams(**{k: v for k, v in params_kwargs.items() if v is not None}),
            tools=tools,
            tenant_id=tenant_id,
            prompt_name=payload.prompt.name if payload.prompt else None,
            prompt_version=prompt_version.version if prompt_version else None,
            prompt_hash=prompt_version.content_hash if prompt_version else None,
            idempotency_key=payload.idempotency_key,
        )
        return req, prompt_version

    # ==================================================== 非流式
    async def chat(self, payload: ChatCompletionRequest, tenant_id: str = "default") -> ChatResponse:
        ctx = TraceContext.begin(tenant_id)
        budget = self.engine.new_budget()
        tracer = Tracer(ctx, payload.model, False, self.config.resilience.total_timeout_ms)
        prompt_meta = (None, None, None)
        records: list[AttemptRecord] = []
        decision: RouteDecision | None = None
        req: ChatRequest | None = None
        idem_key: str | None = None

        tracer.mark("queued")
        try:
            # Prompt 解析/渲染纳入 Trace：渲染期失败也要落 trace
            tracer.mark("render_start")
            req, pv = await self.prepare(payload, tenant_id)
            tracer.mark("render_done")
            tracer.requested_model = req.requested_model
            prompt_meta = (req.prompt_name, req.prompt_version, req.prompt_hash)

            # 幂等去重（仅非流式支持结果重放）：同键同指纹命中缓存，同键异指纹拒绝
            idem_key = req.idempotency_key
            if idem_key:
                replay, acquired = await self._idempotency_begin(
                    tenant_id, idem_key, _request_fingerprint(req), ctx.trace_id
                )
                if replay is not None:
                    return replay
                idem_key = acquired

            await self.router.budget.refresh(tenant_id)
            decision = self.router.route(req)
            tracer.mark("routed")
            await self._persist_decision(ctx, decision)
            if decision.chosen is None:
                raise self._no_endpoint_error(decision)

            resp = await self.engine.execute_nonstream(
                req, decision.fallback_chain, budget, records
            )

            # 非流式结构化输出：有限次 repair 闭环（不换端点、不拼接文本）
            validation, resp, repairs = await self._validate_with_repair(
                req, resp, budget, records
            )
            tracer.structured_repairs = repairs
            tracer.attempts = records
            tracer.mark("end")

            # 先落 trace 以满足 trace_calls 外键，再落 calls/cost，最后覆写最终 trace
            shell = tracer.build_success(resp, output_validation="pending",
                                         route_snapshot=decision.to_snapshot(), prompt=prompt_meta)
            await self._safe_insert_trace(shell)
            await self._record_calls(ctx, req, records)

            trace = tracer.build_success(resp, output_validation=validation,
                                         route_snapshot=decision.to_snapshot(), prompt=prompt_meta)
            await self._safe_insert_trace(trace)
            if idem_key:
                await self.repo.complete_idempotency(tenant_id, idem_key, resp.model_dump_json())
            return resp
        except GatewayError as err:
            # 执行失败时释放幂等占位，允许客户端用同一键重试（冲突类错误不释放）
            if idem_key and err.code not in ("IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_IN_PROGRESS"):
                await self._safe_release_idempotency(tenant_id, idem_key)
            # repair 调用记录为 retried 且无 fallback 来源，以此与引擎重试区分
            tracer.structured_repairs = sum(
                1 for r in records if r.retried and r.fallback_from is None
            )
            tracer.attempts = records
            tracer.close_render_segment()
            # 验证最终失败 / repair 中超时等：已产生的上游调用与费用仍须落库
            if req is not None and records and any(r.http_status == 200 for r in records):
                await self._safe_insert_trace(
                    tracer.build_success(None, output_validation="pending",
                                         route_snapshot=decision.to_snapshot() if decision else None,
                                         prompt=prompt_meta)
                )
                await self._record_calls(ctx, req, records)
            await self._fail(ctx, tracer, err, decision, prompt_meta)
            raise

    # ==================================================== 流式
    async def stream_chat(
        self,
        payload: ChatCompletionRequest,
        tenant_id: str = "default",
        *,
        is_disconnected: Callable[[], Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        ctx = TraceContext.begin(tenant_id)
        budget = self.engine.new_budget()
        tracer = Tracer(ctx, payload.model, True, self.config.resilience.total_timeout_ms)
        prompt_meta = (None, None, None)
        records: list[AttemptRecord] = []

        tracer.mark("queued")
        try:
            tracer.mark("render_start")
            req, pv = await self.prepare(payload, tenant_id)
            tracer.mark("render_done")
            tracer.requested_model = req.requested_model
            prompt_meta = (req.prompt_name, req.prompt_version, req.prompt_hash)

            await self.router.budget.refresh(tenant_id)
            decision = self.router.route(req)
            tracer.mark("routed")
            await self._persist_decision(ctx, decision)
            if decision.chosen is None:
                raise self._no_endpoint_error(decision)
        except GatewayError as err:
            tracer.close_render_segment()
            await self._fail(ctx, tracer, err, None, prompt_meta)
            raise

        pipe = await self.engine.execute_stream(req, decision.fallback_chain, budget, records)
        tracer.attempts = records

        async def _out() -> AsyncIterator[StreamEvent]:
            # 流式结构化：中途 JSON 不完整绝不校验/执行，先攒批，在终态对完整文本做一次出口校验
            rf = req.params.response_format
            parts: list[str] = []
            latest_usage = _empty_usage()
            finished: StreamEvent | None = None
            validation = "skipped:stream" if rf is None else "pending"
            terminal_err: GatewayError | None = None
            cancel_exc: asyncio.CancelledError | None = None
            try:
                async for evt in pipe:
                    # 主动探活：上游事件之间发现客户端已断连，立即按取消终态收尾
                    if is_disconnected is not None and await _maybe_await(is_disconnected()):
                        # break 而非 return：收尾后仍须把取消错误抛给调用方
                        terminal_err = GatewayError(
                            "CLIENT_CANCELLED", "client disconnected before stream finished"
                        )
                        break
                    if evt.type == USAGE and evt.usage is not None:
                        # 上游用量为累计快照；门开后中断时据此对已处理 token 计费
                        latest_usage = evt.usage
                    if evt.type == DELTA and evt.text:
                        parts.append(evt.text)
                        # 增量可行性检查：仅识别"确定不可恢复"以提前止损；
                        # 不完整前缀继续等待，Schema 校验仍只在终态做一次
                        if rf is not None and classify_json_prefix("".join(parts)) is JsonPrefix.BROKEN:
                            terminal_err = GatewayError(
                                "OUTPUT_INVALID_JSON",
                                "streamed JSON became syntactically unrecoverable",
                            )
                            yield StreamEvent.error(
                                terminal_err.code, terminal_err.message or "invalid JSON"
                            )
                            return
                    if evt.type == FINISH:
                        finished = evt
                        tracer.mark("first_token")
                        if rf is not None:
                            result = validate_output("".join(parts), rf)
                            if result is not None and not result.ok:
                                code = ("OUTPUT_INVALID_JSON" if result.error
                                        and result.error.startswith("invalid JSON")
                                        else "OUTPUT_SCHEMA_VIOLATION")
                                terminal_err = GatewayError(code, result.error)
                                # 脏输出已计费：先记账，再以唯一错误终态告知客户端（不发 finish）
                                yield StreamEvent.error(terminal_err.code,
                                                        terminal_err.message or "output validation failed")
                                return
                            validation = "pass"
                    if evt.type == ERROR and finished is None:
                        # 适配器解析到上游 error 事件：唯一错误终态，不再继续消费
                        terminal_err = GatewayError(
                            evt.error_code or "UPSTREAM_STREAM_INTERRUPTED",
                            evt.error_message or "stream error",
                        )
                        yield evt
                        return
                    yield evt
            except GatewayError as err:
                # 门开后的连接/超时中断：与脏输出一样进入统一收尾，随后抛出由 SSE 层编码
                terminal_err = err
            except asyncio.CancelledError as err:
                # ASGI 层在客户端断连时向生成器抛入取消：落 CLIENT_CANCELLED 终态与
                # 已产出 token 的账，随后保持取消语义重新抛出
                terminal_err = GatewayError(
                    "CLIENT_CANCELLED", "client disconnected while streaming"
                )
                cancel_exc = err
            finally:
                # 立即关闭上游管道（释放并发闸、断上游连接），再统一收尾
                with contextlib.suppress(Exception):
                    await pipe.aclose()
                await self._finalize_stream(
                    ctx, req, records, tracer, decision, prompt_meta,
                    finished, latest_usage, validation, terminal_err,
                )
            if cancel_exc is not None:
                raise cancel_exc
            if terminal_err is not None:
                raise terminal_err

        return _out()

    # ==================================================== 记账辅助
    async def _persist_decision(self, ctx: TraceContext, decision: RouteDecision) -> None:
        try:
            await self.repo.insert_route_decision(ctx.trace_id, decision)
        except Exception:
            pass  # 决策落库失败不阻断主链路

    async def _record_calls(self, ctx, req, records: list[AttemptRecord]) -> None:
        """逐次真实调用记账：凡 HTTP 成功（含后被输出校验拒绝、repair 调用）都计费。"""
        request_hash = fingerprint(json.dumps(req.model_dump(mode="json"), ensure_ascii=False))
        for rec in records:
            call_id = new_call_id()
            step_id = f"step_{rec.attempt}_{rec.endpoint_id}"
            await self._safe_insert_call(CallRecord(
                call_id=call_id, trace_id=ctx.trace_id, run_id=ctx.run_id, step_id=step_id,
                attempt=rec.attempt, endpoint_id=rec.endpoint_id,
                request_hash=request_hash,
                usage=rec.usage or _empty_usage(),
                connect_ms=rec.connect_ms,
                ttft_ms=rec.ttft_ms,
                http_status=rec.http_status,
                upstream_request_id=rec.upstream_request_id,
                error_code=rec.error_code,
            ))
            # 上游确实产出 token 的调用都进入成本台账（含被校验拒绝的脏输出）
            if rec.http_status == 200 and rec.usage is not None:
                cost = build_cost_record(self.registry, rec.endpoint_id, rec.usage,
                                         call_id=call_id, trace_id=ctx.trace_id, tenant_id=ctx.tenant_id)
                cost.price_version = self.pricing.version
                await self._safe_insert_cost(cost)

    async def _idempotency_begin(
        self, tenant_id: str, key: str, request_hash: str, trace_id: str
    ) -> tuple[ChatResponse | None, str | None]:
        """返回 (重放响应, 占位成功的 key)。命中冲突直接抛稳定错误码。"""
        existing = await self.repo.get_idempotency(tenant_id, key)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise GatewayError(
                    "IDEMPOTENCY_CONFLICT",
                    "same idempotency key reused with a different request body",
                    details={"idempotency_key": key},
                )
            if existing["status"] == "completed" and existing["response_json"]:
                return ChatResponse.model_validate_json(existing["response_json"]), None
            raise GatewayError(
                "IDEMPOTENCY_IN_PROGRESS",
                "request with this idempotency key is still being processed",
                details={"idempotency_key": key},
            )
        if await self.repo.insert_idempotency_pending(tenant_id, key, request_hash, trace_id):
            return None, key
        # 并发占位竞争：重读一次按既有记录裁决
        existing = await self.repo.get_idempotency(tenant_id, key)
        if existing and existing["request_hash"] != request_hash:
            raise GatewayError("IDEMPOTENCY_CONFLICT",
                               "same idempotency key reused with a different request body")
        if existing and existing["status"] == "completed" and existing["response_json"]:
            return ChatResponse.model_validate_json(existing["response_json"]), None
        raise GatewayError("IDEMPOTENCY_IN_PROGRESS", "idempotency key contested")

    async def _safe_release_idempotency(self, tenant_id: str, key: str) -> None:
        try:
            await self.repo.fail_idempotency(tenant_id, key)
        except Exception:
            pass

    async def _validate_with_repair(
        self,
        req: ChatRequest,
        resp: ChatResponse,
        budget: TimeoutBudget,
        records: list[AttemptRecord],
    ) -> tuple[str, ChatResponse, int]:
        """非流式结构化输出校验 + 有限次 repair。repair 锁定同一端点，不重试、不换端点、不拼文本。"""
        rf = req.params.response_format
        result = validate_output(resp.text, rf)
        if result is None:
            return "not_required", resp, 0

        current = resp
        messages = list(req.messages)
        repairs = 0
        last_result = result
        while not last_result.ok:
            if repairs >= self.config.structured_output_retries or budget.expired():
                # 标记被拒输出对应 attempt，便于 call 与错误码关联
                self._mark_validation_failure(records, current, last_result.error)
                code = ("OUTPUT_INVALID_JSON" if last_result.error
                        and last_result.error.startswith("invalid JSON")
                        else "OUTPUT_SCHEMA_VIOLATION")
                raise GatewayError(code, last_result.error or "output validation failed")

            # 反馈式修复：附上上轮输出 + 校验错误，要求仅返回合规 JSON
            messages = messages + [
                Message(role=Role.ASSISTANT, content=strip_fence(current.text)),
                Message(role=Role.USER, content=repair_instruction(last_result.error, rf)),
            ]
            repaired_req = req.model_copy(update={"messages": messages})
            current = await self.engine.repair_once(
                repaired_req, current.endpoint_id, budget, records
            )
            repairs += 1
            last_result = validate_output(current.text, rf)

        current.output_valid = True
        current.output_validation_error = None
        label = "pass" if repairs == 0 else f"pass_after_repair_{repairs}"
        return label, current, repairs

    @staticmethod
    def _mark_validation_failure(records: list[AttemptRecord], resp: ChatResponse, error: str | None) -> None:
        for rec in reversed(records):
            if rec.endpoint_id == resp.endpoint_id and rec.http_status == 200:
                rec.error_code = (
                    "OUTPUT_INVALID_JSON" if error and error.startswith("invalid JSON")
                    else "OUTPUT_SCHEMA_VIOLATION"
                )
                return

    async def _finalize_stream(
        self, ctx, req, records, tracer, decision, prompt_meta,
        finished: StreamEvent | None, latest_usage, validation: str,
        terminal_err: GatewayError | None,
    ) -> None:
        """流式唯一收尾：无论正常 finish / 脏输出 / 门开后中断，trace/call/cost 都落库。

        门开后的在途尝试是 records 中最后一条无 error_code 的记录；已产出内容或
        已拿到用量快照即视为可计费。中断时用配置中的实际模型构造影子响应。
        """
        tracer.mark("end")
        live = next((r for r in reversed(records) if r.error_code is None), None)
        has_usage = bool(
            latest_usage and (latest_usage.input_tokens or latest_usage.output_tokens)
        )
        if live is None:
            # 理论上不会发生：进入 _out 时首包门已开；兜底落失败 trace
            if terminal_err is not None:
                await self._fail(ctx, tracer, terminal_err, decision, prompt_meta)
            return
        if finished is None and not has_usage:
            # 门已开但拿不到任何用量快照：无法计费，仍须落唯一失败终态
            if terminal_err is not None:
                await self._fail(ctx, tracer, terminal_err, decision, prompt_meta)
            return

        tracer.mark("first_token")
        if finished is not None:
            usage = finished.usage or latest_usage
            shadow = _shadow_response(req, finished)
        else:
            usage = latest_usage
            shadow = _interrupted_shadow(req, live.endpoint_id, usage, self.registry)

        # 先落 trace 壳以满足 trace_calls 外键，再落 calls/cost，最后覆写终态 trace
        await self._safe_insert_trace(
            tracer.build_success(shadow, output_validation="pending",
                                 route_snapshot=decision.to_snapshot(), prompt=prompt_meta)
        )
        await self._record_stream_outcome(ctx, req, records, live.endpoint_id, usage,
                                          finished)
        if terminal_err is not None:
            await self._fail(ctx, tracer, terminal_err, decision, prompt_meta)
        else:
            trace = tracer.build_success(
                shadow, output_validation=validation,
                route_snapshot=decision.to_snapshot(), prompt=prompt_meta,
            )
            await self._safe_insert_trace(trace)

    async def _record_stream_outcome(
        self, ctx, req, records, live_endpoint_id: str, usage,
        finished: StreamEvent | None,
    ) -> None:
        """门开后在途端点的调用一律按 200 + 真实用量记账（含被校验拒绝、中断的脏输出）。"""
        for rec in records:
            call_id = new_call_id()
            is_live = rec.error_code is None and rec.endpoint_id == live_endpoint_id
            await self._safe_insert_call(CallRecord(
                call_id=call_id, trace_id=ctx.trace_id, run_id=ctx.run_id,
                step_id=f"step_{rec.attempt}_{rec.endpoint_id}",
                attempt=rec.attempt, endpoint_id=rec.endpoint_id,
                request_hash=fingerprint(json.dumps(req.model_dump(mode="json"), ensure_ascii=False)),
                usage=usage if is_live else _empty_usage(),
                connect_ms=rec.connect_ms,
                ttft_ms=rec.ttft_ms,
                http_status=200 if is_live else None,
                upstream_request_id=(
                    finished.upstream_request_id if is_live and finished is not None else None
                ),
                error_code=rec.error_code,
            ))
            if is_live:
                cost = build_cost_record(self.registry, rec.endpoint_id, usage,
                                         call_id=call_id, trace_id=ctx.trace_id, tenant_id=ctx.tenant_id)
                cost.price_version = self.pricing.version
                await self._safe_insert_cost(cost)

    @staticmethod
    def _no_endpoint_error(decision: RouteDecision) -> GatewayError:
        reasons = {r.reason_code for r in decision.rejections}
        if any("budget" in r for r in reasons):
            return GatewayError("ROUTE_BUDGET_EXHAUSTED", "tenant budget exhausted")
        return GatewayError(
            "ROUTE_NO_CAPABLE_ENDPOINT",
            "no endpoint passed capability and policy filters",
            details={"rejections": [r.model_dump() for r in decision.rejections]},
        )

    async def _fail(self, ctx, tracer: Tracer, err: GatewayError,
                    decision: RouteDecision | None, prompt_meta) -> None:
        snapshot = decision.to_snapshot() if decision else None
        trace = tracer.build_error(err, route_snapshot=snapshot, prompt=prompt_meta)
        await self._safe_insert_trace(trace)

    async def _safe_insert_trace(self, t: TraceRecord) -> None:
        try:
            await self.repo.insert_trace(t)
        except Exception:
            pass

    async def _safe_insert_call(self, c: CallRecord) -> None:
        try:
            await self.repo.insert_call(c)
        except Exception:
            pass

    async def _safe_insert_cost(self, c) -> None:
        try:
            await self.repo.ensure_price_version(self.pricing.version, self.pricing.snapshot_json())
            await self.repo.insert_cost(c)
        except Exception:
            pass


def _request_fingerprint(req: ChatRequest) -> str:
    """规范化请求体指纹：排除 idempotency_key 本身，使"同键同体"可比较。"""
    data = req.model_dump(mode="json", exclude={"idempotency_key"})
    return fingerprint(json.dumps(data, ensure_ascii=False, sort_keys=True))


def _empty_usage():
    from app.core.models import TokenUsage
    return TokenUsage()


async def _maybe_await(value):
    """is_disconnected 探测可能返回 bool 或 coroutine（FastAPI 为协程），统一求值。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _shadow_response(req: ChatRequest, f: StreamEvent) -> ChatResponse:
    from app.core.models import FinishReason
    return ChatResponse(
        text="",
        requested_model=req.requested_model,
        resolved_model=f.resolved_model or req.requested_model,
        endpoint_id=f.endpoint_id or "",
        provider_id="",
        usage=f.usage or _empty_usage(),
        finish_reason=f.finish_reason or FinishReason.STOP,
        upstream_request_id=f.upstream_request_id,
    )


def _interrupted_shadow(req: ChatRequest, endpoint_id: str, usage, registry) -> ChatResponse:
    """门开后中断、没有 finish 事件时的影子响应：用配置实际模型与已收用量落 trace。"""
    from app.core.models import FinishReason
    return ChatResponse(
        text="",
        requested_model=req.requested_model,
        resolved_model=registry.endpoint(endpoint_id).actual_model,
        endpoint_id=endpoint_id,
        provider_id=registry.provider(endpoint_id).id,
        usage=usage or _empty_usage(),
        finish_reason=FinishReason.ERROR,
        upstream_request_id=None,
    )


def _convert_messages(payload: ChatCompletionRequest) -> list[Message]:
    out: list[Message] = []
    for m in payload.messages:
        content = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
        out.append(Message(
            role=Role(m.role), content=content or "",
            tool_call_id=m.tool_call_id, name=m.name,
        ))
    return out


class _AsyncBudget:
    """路由内读取已花预算：直接 await 不友好（pipeline 为同步纯逻辑），故缓存为近似实时值。"""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def spent_usd(self, tenant_id: str) -> float:
        return getattr(self, "_cache", {}).get(tenant_id, 0.0)

    async def refresh(self, tenant_id: str) -> None:
        if not hasattr(self, "_cache"):
            self._cache = {}
        self._cache[tenant_id] = await self.repo.spent_usd(tenant_id)
