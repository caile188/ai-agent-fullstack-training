"""单次端点调用的韧性包装：非流式总超时，流式"首包门控"。

传输本身由各适配器内的官方 SDK 客户端完成；本模块不认识任何 HTTP 细节。
关键边界：
  - SDK 连接错误 / 超时 / 429 / 5xx 在首包前抛出 -> 可重试可 fallback
  - 流式：在首个内容事件(delta/tool_call)之前，任何失败都可 fallback；
          一旦首个内容事件已产生，门打开，之后中断只记 STREAM_INTERRUPTED，
          绝不重试/拼接文本。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from app.config import EndpointConfig, ResilienceConfig
from app.core.errors import GatewayError
from app.core.models import ChatRequest, ChatResponse
from app.core.streaming import FIRST_OUTPUT_TYPES, StreamEvent
from app.services.adapters.base import LLMAdapter
from app.services.execution.budget import TimeoutBudget


class Invoker:
    def __init__(self, config: ResilienceConfig) -> None:
        self.config = config

    def _timeout(self, adapter: LLMAdapter, budget: TimeoutBudget, *, stream: bool) -> float:
        total = budget.request_timeout(stream=stream)
        if adapter.provider.request_timeout_seconds:
            total = min(total, adapter.provider.request_timeout_seconds)
        return total

    # ---------------------------------------------------------------- 非流式
    async def invoke_nonstream(
        self,
        adapter: LLMAdapter,
        req: ChatRequest,
        endpoint: EndpointConfig,
        budget: TimeoutBudget,
    ) -> ChatResponse:
        timeout = self._timeout(adapter, budget, stream=False)
        try:
            return await asyncio.wait_for(
                adapter.complete(req, endpoint, timeout=timeout), timeout
            )
        except GatewayError:
            raise
        except asyncio.TimeoutError as e:
            raise GatewayError("UPSTREAM_TIMEOUT", "upstream request timeout") from e
        except Exception as e:
            raise adapter.translate_error(e) from e

    # ---------------------------------------------------------------- 流式
    async def stream(
        self,
        adapter: LLMAdapter,
        req: ChatRequest,
        endpoint: EndpointConfig,
        budget: TimeoutBudget,
    ) -> AsyncIterator[StreamEvent]:
        """首包门控流。门未开前抛 GatewayError（可 fallback）；门开后只向前产出。"""
        timeout = self._timeout(adapter, budget, stream=True)
        ttft_timeout = self.config.ttft_timeout_ms / 1000.0

        async def _gen() -> AsyncIterator[StreamEvent]:
            gate_open = False
            buffered: list[StreamEvent] = []
            async with contextlib.aclosing(
                adapter.open_stream(req, endpoint, timeout=timeout)
            ) as event_iter:
                while True:
                    # 首输出前施加 TTFT 边界；门开后用剩余预算作为空闲上限
                    wait = ttft_timeout if not gate_open else max(1.0, budget.remaining_ms() / 1000.0)
                    try:
                        evt = await asyncio.wait_for(event_iter.__anext__(), timeout=wait)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as e:
                        if not gate_open:
                            raise GatewayError("UPSTREAM_TTFT_TIMEOUT", "first token timeout") from e
                        raise GatewayError(
                            "UPSTREAM_STREAM_INTERRUPTED", "idle timeout during generation"
                        ) from e
                    except GatewayError as e:
                        # 门未开：保持原错误码（连接/429/5xx），交引擎 fallback
                        if not gate_open or e.code == "CLIENT_CANCELLED":
                            raise
                        raise GatewayError("UPSTREAM_STREAM_INTERRUPTED", e.message) from e
                    except Exception as e:
                        if not gate_open:
                            raise adapter.translate_error(e) from e
                        raise GatewayError("UPSTREAM_STREAM_INTERRUPTED", str(e)) from e

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

        return _gen()
