"""适配器抽象基类。

职责（屏蔽三家差异中的两家协议）：
  1) build_http  : ChatRequest -> 具体协议的 (method, url, headers, json_body)
  2) parse_json  : 非流式上游响应 -> ChatResponse（中性）
  3) parse_sse   : 上游 SSE 原始字节行 -> StreamEvent（中性）
  4) usage/finish/模型版本字段的归一化

适配器不做重试、不做路由、不碰数据库。
"""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from app.config import EndpointConfig, ProviderConfig
from app.core.models import ChatRequest, ChatResponse
from app.core.streaming import StreamEvent

# 一次 HTTP 调用需要的全部素材
PreparedRequest = tuple[str, str, dict[str, str], dict]


class LLMAdapter(abc.ABC):
    protocol: str = "base"

    def __init__(self, provider: ProviderConfig) -> None:
        self.provider = provider

    # ------------------------------------------------------------- 构造请求
    @abc.abstractmethod
    def build_http(
        self, req: ChatRequest, endpoint: EndpointConfig, *, stream: bool
    ) -> PreparedRequest:
        ...

    # ------------------------------------------------------------- 非流式
    @abc.abstractmethod
    def parse_json(
        self, raw: dict, *, req: ChatRequest, endpoint: EndpointConfig
    ) -> ChatResponse:
        ...

    # ------------------------------------------------------------- 流式
    @abc.abstractmethod
    async def parse_sse(
        self, lines: AsyncIterator[bytes], *, req: ChatRequest, endpoint: EndpointConfig
    ) -> AsyncIterator[StreamEvent]:
        ...

    # ------------------------------------------------------------- 错误体解析
    @abc.abstractmethod
    def parse_error(self, status_code: int, raw: bytes) -> tuple[str, str, str | None]:
        """返回 (供应商错误类型, 消息, upstream_request_id)。"""
        ...

    # ------------------------------------------------------------- 共用工具
    def _url(self) -> str:
        return f"{self.provider.base_url}{self.provider.api_path}"

    @staticmethod
    async def iter_sse_payloads(lines: AsyncIterator[bytes]) -> AsyncIterator[str]:
        """把 httpx 的行流解析为 SSE data 负载（跳过 event:/:/注释/空行）。"""
        async for line in lines:
            decoded = line if isinstance(line, str) else line.decode("utf-8")
            decoded = decoded.strip()
            if not decoded or decoded.startswith(":") or decoded.startswith("event:"):
                continue
            if decoded.startswith("data:"):
                data = decoded[5:].strip()
                if data and data != "[DONE]":
                    yield data
