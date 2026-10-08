"""FastAPI 装配：生命周期、异常治理、路由注册。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import routes_admin, routes_chat, routes_eval, routes_prompts
from app.api.deps import build_services
from app.core.errors import GatewayError


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = os.environ.get("GATEWAY_CONFIG", "gateway.yaml")
    services = build_services(config_path)
    await services.db.connect()
    await services.engine.startup()
    app.state.services = services
    try:
        yield
    finally:
        await services.engine.shutdown()
        await services.db.close()


app = FastAPI(title="Governed LLM Gateway", version="0.1.0", lifespan=lifespan)


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


app.include_router(routes_chat.router)
app.include_router(routes_prompts.router)
app.include_router(routes_eval.router)
app.include_router(routes_admin.router)


@app.get("/")
async def root() -> dict:
    return {"service": "governed-llm-gateway", "status": "running"}
