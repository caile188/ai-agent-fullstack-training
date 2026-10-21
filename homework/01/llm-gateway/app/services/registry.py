"""能力注册表：endpoint_id -> (适配器实例, 能力集合, 定价)。

适配器按 provider.protocol 实例化，并持有该 provider 的官方 SDK 客户端；
路由与执行只通过注册表拿到可调用对象，不直接 import 具体适配器，
符合"按能力/协议注册而非按名称硬编码"。
"""
from __future__ import annotations

from typing import Any

import anthropic
import openai

from app.config import EndpointConfig, GatewayConfig, ProviderConfig
from app.core.capabilities import CapabilitySet, Price
from app.services.adapters.anthropic import AnthropicAdapter
from app.services.adapters.base import LLMAdapter
from app.services.adapters.openai_responses import OpenAIResponsesAdapter

_PROTOCOL_CLASSES: dict[str, type[LLMAdapter]] = {
    "openai_responses": OpenAIResponsesAdapter,
    "anthropic": AnthropicAdapter,
}


def _build_sdk_client(provider: ProviderConfig) -> Any:
    """按协议创建官方 SDK 客户端。重试统一关闭（max_retries=0），
    重试/fallback/熔断/预算全部由执行引擎在外层控制。"""
    api_key = provider.auth.api_key().get_secret_value()
    if provider.protocol == "anthropic":
        return anthropic.AsyncAnthropic(
            base_url=provider.base_url,
            api_key=api_key,
            default_headers=provider.extra_headers,
            max_retries=0,
        )
    return openai.AsyncOpenAI(
        base_url=provider.base_url,
        api_key=api_key,
        default_headers=provider.extra_headers,
        max_retries=0,
    )


class AdapterRegistry:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._clients: dict[str, Any] = {}
        self._adapters: dict[str, LLMAdapter] = {}
        for provider in config.providers.values():
            cls = _PROTOCOL_CLASSES.get(provider.protocol)
            if cls is None:
                raise ValueError(f"unsupported protocol: {provider.protocol}")
            client = _build_sdk_client(provider)
            self._clients[provider.id] = client
            self._adapters[provider.id] = cls(provider, client)

    def replace_client(self, provider_id: str, client: Any) -> None:
        """替换 SDK 客户端（测试注入内存假实现）。"""
        self._clients[provider_id] = client
        self._adapters[provider_id].client = client

    async def aclose(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    def adapter_for(self, endpoint_id: str) -> LLMAdapter:
        provider_id = self.config.endpoints[endpoint_id].provider_id
        return self._adapters[provider_id]

    def endpoint(self, endpoint_id: str) -> EndpointConfig:
        return self.config.endpoints[endpoint_id]

    def provider(self, endpoint_id: str) -> ProviderConfig:
        return self.config.provider_of(endpoint_id)

    def capabilities(self, endpoint_id: str) -> CapabilitySet:
        return self.config.endpoints[endpoint_id].capability_set

    def price(self, endpoint_id: str) -> Price:
        return self.config.endpoints[endpoint_id].price_model()
