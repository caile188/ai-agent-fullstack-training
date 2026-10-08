"""单次端点调用（httpx）：非流式返回 ChatResponse，流式做"首包门控"。

关键边界：
  - 连接错误 / 超时 / 429 / 5xx 在连接或首包前抛出 -> 可重试可 fallback
  - 流式：在首个内容事件(delta/tool_call)之前，任何失败都可 fallback；
          一旦首个内容事件已产生，门打开，之后中断只记 STREAM_INTERRUPTED，
          绝不重试/拼接文本。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx

from app.config import EndpointConfig, ResilienceConfig
from app.core.errors import GatewayError, parse_retry_after, spec_for_http_status
from app.core.models import ChatRequest, ChatResponse
from app.core.streaming import FIRST_OUTPUT_TYPES, StreamEvent
from app.services.adapters.base import LLMAdapter
from app.services.execution.budget import TimeoutBudget


class Invoker:
    def __init__(self, config: ResilienceConfig) -> None:
        self.config = config

    def _connect_timeout(self, adapter: LLMAdapter) -> float:
        return (
            adapter.provider.connect_timeout_seconds
            or self.config.connect_timeout_ms / 1000.0
        )

    # ---------------------------------------------------------------- 非流式
    async def invoke_nonstream(
        self,
        client: httpx.AsyncClient,
        adapter: LLMAdapter,
        req: ChatRequest,
        endpoint: EndpointConfig,
        budget: TimeoutBudget,
        record: "AttemptRecord | None" = None,
    ) -> ChatResponse:
        method, url, headers, body = adapter.build_http(req, endpoint, stream=False)
        total = budget.request_timeout(stream=False)
        if adapter.provider.request_timeout_seconds:
            total = min(total, adapter.provider.request_timeout_seconds)
        timeout = httpx.Timeout(total, connect=self._connect_timeout(adapter))
        # 用 stream 上下文拿到"响应头到达"时刻作为 connect 指标，再读完 body
        cm = client.stream(method, url, headers=headers, json=body, timeout=timeout)
        t0 = time.monotonic()
        try:
            resp = await cm.__aenter__()
        except httpx.ConnectError as e:
            raise GatewayError("UPSTREAM_CONNECT_ERROR", str(e)) from e
        except httpx.TimeoutException as e:
            raise GatewayError("UPSTREAM_TIMEOUT", str(e)) from e
        except httpx.HTTPError as e:
            raise GatewayError("UPSTREAM_CONNECT_ERROR", str(e)) from e
        if record is not None:
            record.connect_ms = round((time.monotonic() - t0) * 1000.0, 2)

        try:
            if resp.status_code >= 400:
                raw = await resp.aread()
                raise self._http_error(adapter, resp, raw=raw)
            raw = await resp.aread()
            return adapter.parse_json(resp.json(), req=req, endpoint=endpoint)
        finally:
            await cm.__aexit__(None, None, None)

    # ---------------------------------------------------------------- 流式
    async def stream(
        self,
        client: httpx.AsyncClient,
        adapter: LLMAdapter,
        req: ChatRequest,
        endpoint: EndpointConfig,
        budget: TimeoutBudget,
        record: "AttemptRecord | None" = None,
    ) -> AsyncIterator[StreamEvent]:
        """首包门控流。门未开前抛 GatewayError（可 fallback）；门开后只向前产出。"""
        method, url, headers, body = adapter.build_http(req, endpoint, stream=True)
        connect_timeout = self._connect_timeout(adapter)
        ttft_timeout = self.config.ttft_timeout_ms / 1000.0

        try:
            ctx = client.stream(method, url, headers=headers, json=body)
            t0 = time.monotonic()
            resp = await ctx.__aenter__()
            if record is not None:
                record.connect_ms = round((time.monotonic() - t0) * 1000.0, 2)
        except httpx.ConnectError as e:
            raise GatewayError("UPSTREAM_CONNECT_ERROR", str(e)) from e
        except httpx.TimeoutException as e:
            raise GatewayError("UPSTREAM_TIMEOUT", str(e)) from e
        except httpx.HTTPError as e:
            raise GatewayError("UPSTREAM_CONNECT_ERROR", str(e)) from e

        if resp.status_code >= 400:
            raw = await resp.aread()
            await ctx.__aexit__(None, None, None)
            raise self._http_error(adapter, resp, raw=raw)

        async def _gen() -> AsyncIterator[StreamEvent]:
            gate_open = False
            buffered: list[StreamEvent] = []
            try:
                event_iter = adapter.parse_sse(
                    resp.aiter_lines(), req=req, endpoint=endpoint
                )
                while True:
                    # 首输出前施加 TTFT 边界；门开后用剩余预算作为空闲上限
                    wait = ttft_timeout if not gate_open else max(1.0, budget.remaining_ms() / 1000.0)
                    try:
                        evt = await asyncio.wait_for(event_iter.__anext__(), timeout=wait)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        if not gate_open:
                            raise GatewayError("UPSTREAM_TTFT_TIMEOUT", "first token timeout")
                        raise GatewayError("UPSTREAM_STREAM_INTERRUPTED", "idle timeout during generation")

                    is_first_output = evt.type in FIRST_OUTPUT_TYPES
                    if not gate_open:
                        buffered.append(evt)
                        if is_first_output:
                            gate_open = True
                            for b in buffered:
                                yield b
                            buffered = []
                    else:
                        yield evt
                # 门始终未开且正常结束（异常协议：无内容），仍把缓冲交出去
                if not gate_open:
                    for b in buffered:
                        yield b
            except GatewayError:
                raise
            except httpx.HTTPError as e:
                # 无论门是否打开都抛 GatewayError：门未开时由引擎 fallback，
                # 门开后由 gateway 统一落 trace/计费并编码为唯一 error 终态。
                # 不就地 yield error 事件，避免同类故障走出两条终态路径。
                if not gate_open:
                    raise GatewayError("UPSTREAM_CONNECT_ERROR", str(e)) from e
                raise GatewayError("UPSTREAM_STREAM_INTERRUPTED", str(e)) from e
            finally:
                await ctx.__aexit__(None, None, None)

        return _gen()

    # ---------------------------------------------------------------- 错误映射
    @staticmethod
    def _http_error(
        adapter: LLMAdapter,
        resp: httpx.Response,
        raw: bytes | None = None,
    ) -> GatewayError:
        raw = raw if raw is not None else resp.content
        err_type, message, upstream_id = adapter.parse_error(resp.status_code, raw)
        code = spec_for_http_status(resp.status_code)
        return GatewayError(
            code,
            message or err_type,
            upstream_request_id=upstream_id or resp.headers.get("x-request-id"),
            details={"upstream_type": err_type},
            retry_after_ms=parse_retry_after(resp.headers.get("retry-after"))
            if resp.status_code == 429 else None,
        )
