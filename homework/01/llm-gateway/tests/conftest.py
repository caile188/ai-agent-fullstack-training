from __future__ import annotations

import pytest_asyncio

from app.api.deps import build_services
from app.api.schemas import ChatCompletionRequest, IncomingMessage
from app.services.db.connection import Database
from tests.fake_upstream import FakeUpstream


@pytest_asyncio.fixture
async def harness(tmp_path):
    svc = build_services("gateway.yaml")
    svc.db = Database(str(tmp_path / "test.db"))
    svc.repo.db = svc.db
    await svc.db.connect()
    # 关掉同端点重试：失败即按锁定链 fallback，便于清晰观察边界
    object.__setattr__(svc.config.resilience.retry, "max_retries", 0)

    fake = FakeUpstream()
    svc.engine._client = fake.client()
    try:
        yield svc, fake
    finally:
        await svc.engine.shutdown()
        await svc.db.close()


def make_request(model: str, *, stream: bool = False, content: str = "hi", **kw) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[IncomingMessage(role="user", content=content)],
        stream=stream,
        **kw,
    )
