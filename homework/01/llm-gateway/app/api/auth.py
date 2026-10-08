"""接入认证与租户解析：多 Key 常量时间比较 + key 指纹。

允许的 key 来自配置 auth.api_key_envs 指向的环境变量（逗号分隔多个）。
auth.enabled=False 时完全放行（本地开发）。
"""
from __future__ import annotations

import hashlib
import hmac
import os

from fastapi import Header, Request

from app.core.errors import GatewayError


def key_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _configured_keys(request: Request) -> list[str]:
    auth = request.app.state.services.config.auth
    keys: list[str] = []
    for env_name in auth.api_key_envs:
        raw = os.environ.get(env_name, "")
        keys.extend(k.strip() for k in raw.split(",") if k.strip())
    return keys


async def resolve_tenant(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> str:
    auth = request.app.state.services.config.auth
    if not auth.enabled:
        return x_tenant_id or "default"

    supplied = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()

    configured = _configured_keys(request)
    if not supplied:
        raise GatewayError("AUTH_MISSING_KEY", "missing API key")
    # 常量时间比较，避免时序侧信道
    if not any(hmac.compare_digest(supplied, candidate) for candidate in configured):
        raise GatewayError("AUTH_INVALID_KEY", "invalid API key")
    return x_tenant_id or "default"
