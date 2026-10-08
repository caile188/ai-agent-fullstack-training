"""能力注册表：endpoint_id -> (适配器实例, 能力集合, 定价)。

适配器按 provider.protocol 实例化；路由与执行只通过注册表拿到可调用对象，
不直接 import 具体适配器，符合"按能力/协议注册而非按名称硬编码"。
"""
from __future__ import annotations

from app.config import EndpointConfig, GatewayConfig, ProviderConfig
from app.core.capabilities import CapabilitySet, Price
from app.services.adapters.anthropic import AnthropicAdapter
from app.services.adapters.base import LLMAdapter
from app.services.adapters.openai_responses import OpenAIResponsesAdapter

_PROTOCOL_CLASSES: dict[str, type[LLMAdapter]] = {
    "openai_responses": OpenAIResponsesAdapter,
    "anthropic": AnthropicAdapter,
}


class AdapterRegistry:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._adapters: dict[str, LLMAdapter] = {}
        for provider in config.providers.values():
            cls = _PROTOCOL_CLASSES.get(provider.protocol)
            if cls is None:
                raise ValueError(f"unsupported protocol: {provider.protocol}")
            self._adapters[provider.id] = cls(provider)

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
