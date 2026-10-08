"""Prompt 合约：版本不可变/hash/变量校验/渲染/发布调用/Eval。"""
from __future__ import annotations

import pytest

from app.api.schemas import ChatCompletionRequest, IncomingMessage, PromptRef
from app.core.errors import GatewayError
from app.core.prompts.versions import EvalCase, PromptVersion
from app.core.prompts.hashing import compute_hash


def _version(**over) -> PromptVersion:
    base = dict(
        name="greeter",
        version="1.0.0",
        system_template="You greet about {{ topic }}.",
        variables_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
        default_logical_model="standard-chat",
        context_budget=1000,
        eval_set=[EvalCase(variables={"topic": "tea"}, must_contain=["answer"])],
        eval_threshold=1.0,
        changelog="init",
    )
    base.update(over)
    return PromptVersion(**base)


async def test_version_hash_and_render(harness):
    svc, _ = harness
    pv = await svc.prompts.create_version(_version())
    assert pv.content_hash and len(pv.content_hash) == 64
    assert compute_hash(pv) == pv.content_hash
    await svc.prompts.publish("greeter", "1.0.0")
    resolved = await svc.prompts.resolve("greeter", None)
    assert resolved.version == "1.0.0"


async def test_missing_variable_fails(harness):
    svc, _ = harness
    await svc.prompts.create_version(_version())
    await svc.prompts.publish("greeter", "1.0.0")
    with pytest.raises(GatewayError) as ei:
        payload = ChatCompletionRequest(
            model="standard-chat",
            messages=[IncomingMessage(role="user", content="")],
            prompt=PromptRef(name="greeter", variables={}),  # 缺 topic
        )
        await svc.gateway.chat(payload)
    assert ei.value.code == "PROMPT_VARIABLE_TYPE"


async def test_chat_via_published_prompt_records_lineage(harness):
    svc, _ = harness
    pv = await svc.prompts.create_version(_version())
    await svc.prompts.publish("greeter", "1.0.0")
    payload = ChatCompletionRequest(
        model="auto",
        messages=[IncomingMessage(role="user", content="")],
        prompt=PromptRef(name="greeter", variables={"topic": "coffee"}),
    )
    resp = await svc.gateway.chat(payload)
    # 默认逻辑模型来自 Prompt 合约
    assert resp.requested_model == "standard-chat"
    recent = await svc.repo.recent_traces(1)
    t = recent[0]
    assert t["prompt_name"] == "greeter"
    assert t["prompt_version"] == "1.0.0"
    assert t["prompt_hash"] == pv.content_hash


async def test_eval_run(harness):
    svc, _ = harness
    await svc.prompts.create_version(_version())
    await svc.prompts.publish("greeter", "1.0.0")
    result = await svc.evaluator.run("greeter")
    assert result["cases_total"] == 1
    # 假上游对带 system 的请求返回含 answer 的 JSON
    assert result["cases_passed"] == 1
    assert result["passed"] is True
