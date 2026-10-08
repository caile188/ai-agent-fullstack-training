"""管理与可观测查询：熔断状态、Trace、路由决策、成本。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/v1/admin")


@router.get("/health")
async def health(request: Request):
    svc = request.app.state.services
    return {"status": "ok", "circuits": svc.breaker.status()}


@router.get("/traces")
async def list_traces(request: Request, limit: int = 50):
    svc = request.app.state.services
    return {"traces": await svc.repo.recent_traces(min(limit, 200))}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    svc = request.app.state.services
    trace = await svc.repo.get_trace(trace_id)
    if not trace:
        raise HTTPException(404, "trace not found")
    trace["calls"] = await svc.repo.get_trace_calls(trace_id)
    trace["decision"] = await svc.repo.get_decision(trace_id)
    return trace
