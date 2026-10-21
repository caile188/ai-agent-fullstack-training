"""Prompt 装配：规则/数据分层 + few-shot 静态区块 + 空消息过滤。

经 prepare 直接观察中性 ChatRequest（不触上游），验证消息布局：
  system(静态规则+few-shot) -> user(运行时数据帧) -> 业务消息
"""
from __future__ import annotations

from app.api.schemas import ChatCompletionRequest, IncomingMessage, PromptRef
from app.core.prompts.versions import FewShotExample, PromptVersion


def _version(**over) -> PromptVersion:
    base = dict(
        name="greeter",
        version="1.0.0",
        system_template=(
            "你是一个问候助手。只根据用户消息中给出的主题，生成一句简短、友好的问候。"
        ),
        user_template="<user_message>\n{{ topic }}\n</user_message>",
        variables_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
        default_logical_model="standard-chat",
    )
    base.update(over)
    return PromptVersion(**base)


def _payload(**kw) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="auto",
        prompt=PromptRef(name="greeter", variables={"topic": "coffee"}),
        **kw,
    )


async def _prepared(svc, payload: ChatCompletionRequest):
    req, _ = await svc.gateway.prepare(payload, "default")
    return req


async def test_system_static_user_carries_variables(harness):
    svc, _ = harness
    await svc.prompts.create_version(_version())
    await svc.prompts.publish("greeter", "1.0.0")

    req = await _prepared(svc, _payload(messages=[]))
    system_msg, user_msg = req.messages
    assert system_msg.role.value == "system"
    assert "coffee" not in system_msg.content           # 变量绝不进 system
    assert user_msg.role.value == "user"
    assert user_msg.content == "<user_message>\ncoffee\n</user_message>"


async def test_few_shot_block_inside_system_before_data(harness):
    svc, _ = harness
    pv = _version(few_shot=[
        FewShotExample(input="冒烟", output="priority=urgent"),
        FewShotExample(output="只输出一句话"),
    ])
    await svc.prompts.create_version(pv)
    await svc.prompts.publish("greeter", "1.0.0")

    req = await _prepared(svc, _payload(messages=[]))
    system_msg = req.messages[0]
    assert "<few_shot_examples>" in system_msg.content
    assert "priority=urgent" in system_msg.content
    # few-shot 区块追加在静态规则之后，同属 system 帧
    assert system_msg.content.index("<few_shot_examples>") > system_msg.content.index("友好的问候。")
    # 数据帧紧随其后
    assert req.messages[1].content == "<user_message>\ncoffee\n</user_message>"


async def test_business_messages_come_after_data_frame(harness):
    svc, _ = harness
    await svc.prompts.create_version(_version())
    await svc.prompts.publish("greeter", "1.0.0")

    req = await _prepared(svc, _payload(messages=[
        IncomingMessage(role="user", content="再正式一点"),
    ]))
    # system / data-frame / business
    assert [m.role.value for m in req.messages] == ["system", "user", "user"]
    assert req.messages[-1].content == "再正式一点"


async def test_blank_business_messages_are_filtered(harness):
    svc, _ = harness
    await svc.prompts.create_version(_version())
    await svc.prompts.publish("greeter", "1.0.0")

    # EvalRunner 会注入一条空 user 消息：不应产生空帧
    req = await _prepared(svc, _payload(messages=[
        IncomingMessage(role="user", content="   "),
    ]))
    assert len(req.messages) == 2
    assert all(m.content.strip() for m in req.messages)
