"""可观测 ID 上下文：request / step / call 三层 ID。

  request_id : 一次网关模型调用（内部含 fallback 链，重试/fallback 不改变它）。
               对应未来 Agent 模型中一次 Request；Agent Run 的 trace_id 是它的上层。
  step_id    : 一次候选端点尝试
  call_id    : 每次真实 HTTP 调用（一次重试产生新 call_id）

供应商响应 ID（upstream_request_id）由适配器从上游响应提取，不在此处生成。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

from pydantic import BaseModel


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def new_id(prefix: str) -> str:
    """生成全局稳定唯一 ID（不依赖进程随机哈希）。"""
    return _new_id(prefix)


def new_request_id() -> str:
    return _new_id("req")


def new_call_id() -> str:
    return _new_id("call")


def new_step_id() -> str:
    return _new_id("step")


class TraceContext(BaseModel):
    request_id: str
    tenant_id: str = "default"
    step_id: str | None = None
    call_id: str | None = None

    @classmethod
    def begin(cls, tenant_id: str = "default") -> "TraceContext":
        return cls(request_id=new_request_id(), tenant_id=tenant_id)

    def new_step(self) -> str:
        self.step_id = _new_id("step")
        self.call_id = None
        return self.step_id

    def new_call(self) -> str:
        self.call_id = _new_id("call")
        return self.call_id


_CURRENT: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


def set_current(ctx: TraceContext) -> None:
    _CURRENT.set(ctx)


def current() -> TraceContext | None:
    return _CURRENT.get()
