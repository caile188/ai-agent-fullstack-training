"""Eval 回放：对已发布 Prompt 版本跑评估集。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.auth import resolve_tenant

router = APIRouter()


@router.post("/v1/eval/{name}")
async def run_eval(
    name: str,
    request: Request,
    version: str | None = None,
    tenant_id: str = Depends(resolve_tenant),
):
    svc = request.app.state.services
    return await svc.evaluator.run(name, version, tenant_id)
