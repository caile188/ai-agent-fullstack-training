"""适配器抽象基类（基于官方 SDK 的出站端口）。

职责（屏蔽两家协议差异）：
  1) build_kwargs : ChatRequest -> 具体 SDK 调用参数（纯映射，可单测）
  2) complete     : 非流式调用 -> ChatResponse（中性）
  3) open_stream  : 流式调用 -> StreamEvent 异步流（中性）
  4) translate_error : SDK 异常 -> GatewayError（稳定错误码）

usage/finish/模型版本字段的归一化在各适配器内完成。
适配器不做重试、不做路由、不碰数据库；传输级超时/首包门控由 Invoker 负责。
"""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Any

from app.config import EndpointConfig, ProviderConfig
from app.core.errors import (
    GatewayError,
    parse_retry_after,
    spec_for_http_status,
)
from app.core.models import ChatRequest, ChatResponse
from app.core.streaming import StreamEvent


class LLMAdapter(abc.ABC):
    protocol: str = "base"

    def __init__(self, provider: ProviderConfig, client: Any) -> None:
        self.provider = provider
        self.client = client

    # ------------------------------------------------------------- 构造参数
    @abc.abstractmethod
    def build_kwargs(
        self, req: ChatRequest, endpoint: EndpointConfig, *, stream: bool
    ) -> dict:
        ...

    # ------------------------------------------------------------- 非流式
    @abc.abstractmethod
    async def complete(
        self, req: ChatRequest, endpoint: EndpointConfig, *, timeout: float
    ) -> ChatResponse:
        ...

    # ------------------------------------------------------------- 流式
    @abc.abstractmethod
    def open_stream(
        self, req: ChatRequest, endpoint: EndpointConfig, *, timeout: float
    ) -> AsyncIterator[StreamEvent]:
        ...

    # ------------------------------------------------------------- 错误归一化
    @abc.abstractmethod
    def translate_error(self, exc: Exception) -> GatewayError:
        ...

    # ------------------------------------------------------------- 共用工具
    @staticmethod
    def status_error(
        *,
        status_code: int,
        message: str,
        upstream_type: str,
        headers: Any,
        upstream_request_id: str | None = None,
    ) -> GatewayError:
        """把供应商 4xx/5xx 响应统一翻译成网关稳定错误码。"""
        retry_after_ms = None
        if status_code == 429:
            get_header = getattr(headers, "get", None)
            if callable(get_header):
                retry_after_ms = parse_retry_after(get_header("retry-after"))
        return GatewayError(
            spec_for_http_status(status_code),
            message or upstream_type,
            upstream_request_id=upstream_request_id,
            details={"upstream_type": upstream_type},
            retry_after_ms=retry_after_ms,
        )
