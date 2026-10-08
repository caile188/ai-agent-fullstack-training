"""Prompt 合约管理：创建版本 / 发布 / 列表 / 渲染预览。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.prompts.hashing import compute_hash
from app.core.prompts.render import render_system
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
            "status": saved.status.value, "content_hash": saved.content_hash}


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
    rendered = render_system(pv, body.variables)
    return {
        "name": name,
        "version": pv.version,
        "content_hash": pv.content_hash,
        "rendered_system": rendered,
        "hash_recomputed": compute_hash(pv),
    }
