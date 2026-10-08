"""Trace 上下文：trace / run / step / call 四层 ID。

  trace_id : 整次用户请求
  run_id   : 一次编排（fallback 链共享，不随重试变）
  step_id  : 每个候选端点尝试
  call_id  : 每次真实 HTTP 调用（一次重试产生新 call_id）
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


def new_call_id() -> str:
    return _new_id("call")


def new_step_id() -> str:
    return _new_id("step")


class TraceContext(BaseModel):
    trace_id: str
    run_id: str
    tenant_id: str = "default"
    step_id: str | None = None
    call_id: str | None = None

    @classmethod
    def begin(cls, tenant_id: str = "default") -> "TraceContext":
        return cls(trace_id=_new_id("trace"), run_id=_new_id("run"), tenant_id=tenant_id)

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
