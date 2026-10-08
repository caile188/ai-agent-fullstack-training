"""执行引擎：在 Router 锁定的候选链内完成 重试 / Fallback / 熔断 / 并发联动。

Fallback 铁律：
  - 只能沿 RouteDecision.fallback_chain 顺序切换，绝不重新路由、绝不拼接文本；
  - 非流式：连接失败 / 过载 / 429超预算 / 熔断 / 首包前错误 才切换；
  - 流式：仅在"首包门"打开前（建连~首 token）可切换；首个内容事件产出后，
          任何中断只终止流，不切换、不重试。
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
from pydantic import BaseModel, Field

from app.config import GatewayConfig
from app.core.errors import GatewayError
from app.core.models import ChatRequest, ChatResponse, TokenUsage
from app.core.streaming import StreamEvent
from app.services.execution.budget import TimeoutBudget
from app.services.execution.concurrency import ConcurrencyLimiter
from app.services.execution.health import CircuitBreaker
from app.services.execution.invoker import Invoker
from app.services.execution.retry import RetryPolicy
from app.services.registry import AdapterRegistry


class AttemptRecord(BaseModel):
    """每次真实尝试（供 Trace 持久化）。"""

    attempt: int
    endpoint_id: str
    stream: bool
    retried: bool = False
    fallback_from: str | None = None
    error_code: str | None = None
    connect_ms: float | None = None
    ttft_ms: float | None = None
    usage: TokenUsage | None = None
    http_status: int | None = None
    upstream_request_id: str | None = None


class ExecutionEngine:
    def __init__(
        self,
        config: GatewayConfig,
        registry: AdapterRegistry,
        breaker: CircuitBreaker,
        limiter: ConcurrencyLimiter,
    ) -> None:
        self.config = config
        self.registry = registry
        self.breaker = breaker
        self.limiter = limiter
        self.invoker = Invoker(config.resilience)
        self.retry = RetryPolicy(config.resilience)
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        self._client = httpx.AsyncClient()

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "engine not started"
        return self._client

    def new_budget(self) -> TimeoutBudget:
        r = self.config.resilience
        return TimeoutBudget(r.total_timeout_ms, r.connect_timeout_ms, r.ttft_timeout_ms)

    # ============================================================== 非流式
    async def execute_nonstream(
        self,
        req: ChatRequest,
        chain: list[str],
        budget: TimeoutBudget,
        records: list[AttemptRecord],
    ) -> ChatResponse:
        attempt = 0
        last_err: GatewayError | None = None
        fallback_from: str | None = None

        for eid in chain:
            adapter = self.registry.adapter_for(eid)
            endpoint = self.registry.endpoint(eid)
            retried_this_endpoint = False

            while attempt < self.config.resilience.max_attempts:
                attempt += 1
                await self.limiter.acquire(eid)
                rec = AttemptRecord(
                    attempt=attempt, endpoint_id=eid, stream=False,
                    retried=retried_this_endpoint, fallback_from=fallback_from,
                )
                records.append(rec)
                try:
                    resp = await self.invoker.invoke_nonstream(
                        self.client, adapter, req, endpoint, budget, record=rec
                    )
                    self.breaker.record_success(eid)
                    rec.usage = resp.usage
                    rec.http_status = 200
                    rec.upstream_request_id = resp.upstream_request_id
                    return resp
                except GatewayError as err:
                    rec.error_code = err.code
                    last_err = err
                    self.breaker.record_failure(eid)
                    if self.retry.should_retry(attempt, err, budget):
                        if await self.retry.sleep(attempt, err, budget):
                            retried_this_endpoint = True
                            continue
                        # 等待超出总预算（如 429 Retry-After 太长）：不重试，落入 fallback
                    if err.spec.fallbackable and budget.remaining_ms() > 0:
                        fallback_from = eid
                        break                       # 推进到链上下一个端点
                    raise err
                finally:
                    self.limiter.release(eid)

        assert last_err is not None
        raise last_err

    async def repair_once(
        self,
        req: ChatRequest,
        endpoint_id: str,
        budget: TimeoutBudget,
        records: list[AttemptRecord],
    ) -> ChatResponse:
        """结构化输出修复：只在已成功的端点上重新请求一次，不重试、不换端点。"""
        if budget.expired():
            raise GatewayError("UPSTREAM_TIMEOUT", "timeout budget exhausted before repair")
        adapter = self.registry.adapter_for(endpoint_id)
        endpoint = self.registry.endpoint(endpoint_id)
        attempt = len(records) + 1
        await self.limiter.acquire(endpoint_id)
        rec = AttemptRecord(
            attempt=attempt, endpoint_id=endpoint_id, stream=False, retried=True
        )
        records.append(rec)
        try:
            resp = await self.invoker.invoke_nonstream(
                self.client, adapter, req, endpoint, budget, record=rec
            )
            self.breaker.record_success(endpoint_id)
            rec.usage = resp.usage
            rec.http_status = 200
            rec.upstream_request_id = resp.upstream_request_id
            return resp
        except GatewayError as err:
            rec.error_code = err.code
            self.breaker.record_failure(endpoint_id)
            raise
        finally:
            self.limiter.release(endpoint_id)

    # ============================================================== 流式
    async def execute_stream(
        self,
        req: ChatRequest,
        chain: list[str],
        budget: TimeoutBudget,
        records: list[AttemptRecord],
    ) -> AsyncIterator[StreamEvent]:
        """返回"已通过首包门"的事件流；门控阶段在候选链间 fallback。"""
        attempt = 0
        last_err: GatewayError | None = None
        fallback_from: str | None = None

        for eid in chain:
            attempt += 1
            adapter = self.registry.adapter_for(eid)
            endpoint = self.registry.endpoint(eid)
            await self.limiter.acquire(eid)
            rec = AttemptRecord(
                attempt=attempt, endpoint_id=eid, stream=True,
                fallback_from=fallback_from,
            )
            records.append(rec)
            t0 = time.monotonic()

            try:
                gen = await self.invoker.stream(
                    self.client, adapter, req, endpoint, budget, record=rec
                )
                # 取首事件：成功即代表 invoker 内部首包门已开（已见首内容）
                first = await gen.__anext__()
                rec.ttft_ms = (time.monotonic() - t0) * 1000.0
                self.breaker.record_success(eid)
            except GatewayError as err:
                rec.error_code = err.code
                last_err = err
                self.breaker.record_failure(eid)
                self.limiter.release(eid)
                if err.spec.fallbackable and budget.remaining_ms() > 0 and attempt < len(chain):
                    fallback_from = eid
                    continue
                raise err

            # 门已开：透传，之后不再切换端点
            async def _pipe(_first=first, _gen=gen, _eid=eid) -> AsyncIterator[StreamEvent]:
                try:
                    yield _first
                    async for evt in _gen:
                        yield evt
                finally:
                    self.limiter.release(_eid)

            return _pipe()

        assert last_err is not None
        raise last_err
