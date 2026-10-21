"""Prompt 合约管理：创建版本 / 发布 / 列表 / 渲染预览。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.prompts.hashing import compute_rendered_hash
from app.core.prompts.render import enforce_context_budget, render_system, render_user
from app.core.prompts.versions import PromptVersion

router = APIRouter(prefix="/v1/prompts")


def _services(request: Request):
    return request.app.state.services


class RenderRequest(BaseModel):
    version: str | None = None
    variables: dict[str, Any] = {}


@router.post("")
async def create_version(payload: PromptVersion, request: Request):
    svc = _services(request)
    saved = await svc.prompts.create_version(payload)
    return {"name": saved.name, "version": saved.version,
            "status": saved.status.value}


@router.post("/{name}/versions/{version}/publish")
async def publish(name: str, version: str, request: Request):
    svc = _services(request)
    await svc.prompts.publish(name, version)
    return {"name": name, "version": version, "status": "published"}


@router.get("/{name}/versions")
async def list_versions(name: str, request: Request):
    svc = _services(request)
    return {"name": name, "versions": await svc.repo.list_versions(name)}


@router.post("/{name}/render")
async def render(name: str, body: RenderRequest, request: Request):
    svc = _services(request)
    pv = await svc.prompts.resolve(name, body.version)
    rendered_system = render_system(pv)
    rendered_user = render_user(pv, body.variables)
    enforce_context_budget(pv, rendered_system, rendered_user)
    return {
        "name": name,
        "version": pv.version,
        "rendered_system": rendered_system,
        "rendered_user": rendered_user,
        "rendered_hash": compute_rendered_hash(rendered_system, rendered_user),
    }
