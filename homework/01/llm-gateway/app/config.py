"""配置加载：gateway.yaml -> Pydantic v2 强类型配置（加载即校验）。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.core.capabilities import Capability, CapabilitySet, Price

# ${ENV} 或 ${ENV:-default}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: object) -> object:
    """递归把 ${NAME} / ${NAME:-default} 占位符替换为环境变量。"""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            return default if default is not None else ""
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- 基础片段
class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["bearer", "x_api_key"]
    env: str

    def api_key(self) -> SecretStr:
        # 密钥以 SecretStr 持有，避免进入 repr / 日志
        return SecretStr(os.environ.get(self.env, ""))


class EndpointConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    provider_id: str = ""
    actual_model: str
    capabilities: list[Capability] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    data_residency: Literal["cn", "global", "any"] = "global"
    max_output_tokens: int = Field(default=4096, gt=0)
    price: "PriceConfig"

    @property
    def capability_set(self) -> CapabilitySet:
        return CapabilitySet(*self.capabilities)

    def price_model(self) -> Price:
        return Price(
            input_per_1m=self.price.input_per_1m,
            output_per_1m=self.price.output_per_1m,
            cached_per_1m=self.price.cached_per_1m,
        )


class PriceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    cached_per_1m: float = Field(default=0.0, ge=0)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    enabled: bool = True
    protocol: Literal["openai_responses", "anthropic"]
    base_url: str
    api_path: str = ""
    auth: AuthConfig
    endpoints: list[EndpointConfig]
    # 可选 provider 级覆盖（缺省回退全局 resilience 预算）
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    connect_timeout_seconds: float | None = Field(default=None, gt=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class RouteEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: str
    priority: int = Field(ge=0)
    weight: int = Field(default=1, ge=1)


class LogicalModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    endpoints: list[RouteEntry] = Field(min_length=1)


class RateLimitConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    requests_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=10, ge=1)


class TenantConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    budget_limit_usd: float = Field(ge=0)
    allowed_regions: list[str] = Field(default_factory=list)
    data_residency: Literal["any", "cn", "global"] = "any"
    max_concurrency_per_endpoint: int = Field(default=8, ge=1)
    # 租户默认限流（每个逻辑模型独立一个桶）
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    # 按逻辑模型覆盖（如 premium-chat 给更小配额）；缺省回退 rate_limit
    model_rate_limits: dict[str, RateLimitConfig] = Field(default_factory=dict)


class GatewayAuthConfig(BaseModel):
    """网关自身接入鉴权。enabled=False 时完全放行（便于本地开发）。"""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    # 从这些环境变量读取允许的 key；单个变量可用逗号分隔多个 key
    api_key_envs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 韧性
class RetryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(ge=0)
    retryable_status: list[int]
    backoff_base_ms: int = Field(gt=0)
    backoff_max_ms: int = Field(gt=0)

    @field_validator("retryable_status")
    @classmethod
    def _all_http(cls, v: list[int]) -> list[int]:
        if any(not (400 <= s < 600) for s in v):
            raise ValueError("retryable_status must be valid 4xx/5xx status codes")
        return v


class CircuitConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    failure_threshold: int = Field(ge=1)
    cooldown_seconds: float = Field(gt=0)
    half_open_probe: int = Field(ge=1)


class ResilienceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(ge=1)
    total_timeout_ms: int = Field(gt=0)
    connect_timeout_ms: int = Field(gt=0)
    ttft_timeout_ms: int = Field(gt=0)
    retry: RetryConfig
    circuit: CircuitConfig


class ScoringConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    weight_health: float = Field(default=0.4, ge=0)
    weight_priority: float = Field(default=0.3, ge=0)
    weight_cost: float = Field(default=0.2, ge=0)
    weight_load: float = Field(default=0.1, ge=0)


class RoutingConfig(BaseModel):
    """首选端点选择策略。fallback 链始终按 priority/score 锁定，不受分摊影响。"""

    model_config = ConfigDict(frozen=True)

    # score：综合评分取最高；weighted_round_robin：同优先级内按 weight 平滑加权轮询
    strategy: Literal["score", "weighted_round_robin"] = "score"


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = "data/gateway.db"


# ---------------------------------------------------------------- 根配置
class RawConfig(BaseModel):
    """与 gateway.yaml 一一对应的原始结构。"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    auth: GatewayAuthConfig = Field(default_factory=GatewayAuthConfig)
    # 非流式结构化输出校验失败后的修复重生成次数（仅非流式）
    structured_output_retries: int = Field(default=1, ge=0, le=3)
    providers: list[ProviderConfig]
    logical_models: list[LogicalModelConfig]
    tenants: list[TenantConfig]
    resilience: ResilienceConfig
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)


class GatewayConfig:
    """派生索引：RawConfig 校验通过后构建，供运行时按 id 快速查找。"""

    def __init__(self, raw: RawConfig, base_dir: Path | None = None) -> None:
        self.raw = raw
        base_dir = base_dir or Path.cwd()
        db_path = Path(raw.database.path)
        self.db_path = str(db_path if db_path.is_absolute() else (base_dir / db_path).resolve())
        self.server_host = raw.server.host
        self.server_port = raw.server.port
        self.resilience = raw.resilience
        self.scoring = raw.scoring
        self.routing = raw.routing
        self.auth = raw.auth
        self.structured_output_retries = raw.structured_output_retries

        self.providers: dict[str, ProviderConfig] = {}
        self.endpoints: dict[str, EndpointConfig] = {}
        for prov in raw.providers:
            for ep in prov.endpoints:
                resolved = ep.model_copy(update={"provider_id": prov.id})
                if resolved.id in self.endpoints:
                    raise ValueError(f"duplicate endpoint id: {resolved.id}")
                self.endpoints[resolved.id] = resolved
            self.providers[prov.id] = prov.model_copy(
                update={"endpoints": [self.endpoints[e.id] for e in prov.endpoints]}
            )

        self.logical_models: dict[str, LogicalModelConfig] = {
            lm.id: lm for lm in raw.logical_models
        }
        self.tenants: dict[str, TenantConfig] = {t.id: t for t in raw.tenants}

        self._cross_validate()

    def _cross_validate(self) -> None:
        # 逻辑模型引用的 endpoint 必须存在
        for lm in self.raw.logical_models:
            for r in lm.endpoints:
                if r.endpoint not in self.endpoints:
                    raise ValueError(
                        f"logical model {lm.id!r} references unknown endpoint {r.endpoint!r}"
                    )
        # 默认租户必须存在
        if "default" not in self.tenants:
            raise ValueError("tenant 'default' is required")

    def tenant(self, tenant_id: str) -> TenantConfig:
        return self.tenants.get(tenant_id) or self.tenants["default"]

    def provider_of(self, endpoint_id: str) -> ProviderConfig:
        return self.providers[self.endpoints[endpoint_id].provider_id]


def load_config(path: str | Path = "gateway.yaml") -> GatewayConfig:
    config_path = Path(path).resolve()
    with open(config_path, "r", encoding="utf-8") as f:
        raw_yaml = yaml.safe_load(f)
    raw_yaml = _expand_env(raw_yaml)
    raw = RawConfig.model_validate(raw_yaml)
    return GatewayConfig(raw, base_dir=config_path.parent)
