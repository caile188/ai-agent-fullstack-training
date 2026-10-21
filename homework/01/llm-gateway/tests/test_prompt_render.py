"""Prompt 渲染：system 静态规则 + few-shot 区块；user 帧沙箱渲染变量、未定义即失败。"""
from __future__ import annotations

import pytest

from app.core.errors import GatewayError
from app.core.prompts.render import (
    enforce_context_budget,
    render_few_shot,
    render_system,
    render_user,
)
from app.core.prompts.versions import FewShotExample, PromptVersion


def _pv(
    *,
    system: str = "You are an assistant.",
    user: str | None = None,
    schema=None,
    budget=None,
    few_shot=None,
    max_variable_chars: int = 20_000,
) -> PromptVersion:
    return PromptVersion(
        name="t",
        version="1.0.0",
        system_template=system,
        user_template=user if user is not None else "",
        variables_schema=schema if schema is not None else {},
        few_shot=few_shot or [],
        default_logical_model="standard-chat",
        context_budget=budget,
        max_variable_chars=max_variable_chars,
        changelog="x",
    )


# ----------------------------------------------------------------- few-shot
def test_few_shot_block_pair_input_output():
    block = render_few_shot([FewShotExample(input="冒烟", output="priority=urgent")])
    assert block is not None
    assert "<few_shot_examples>" in block
    assert "<input>\n冒烟\n</input>" in block
    assert "<output>\npriority=urgent\n</output>" in block


def test_few_shot_block_output_only_omits_input_tag():
    block = render_few_shot([FewShotExample(output="只输出 JSON")])
    assert "<output>" in block
    assert "<input>" not in block


def test_few_shot_block_input_only_omits_output_tag():
    block = render_few_shot([FewShotExample(input="东西坏了快处理")])
    assert "<input>" in block
    assert "<output>" not in block


def test_few_shot_empty_returns_none_and_system_unchanged():
    pv = _pv(system="规则")
    assert render_few_shot(pv.few_shot) is None
    assert render_system(pv) == "规则"


def test_few_shot_appended_to_system_and_not_jinja_rendered():
    # 范例是静态可信内容，其中的 {{ 不得被当作模板表达式
    pv = _pv(
        system="规则",
        few_shot=[FewShotExample(input="{{ name }}", output="hi {{ x }}")],
    )
    out = render_system(pv)
    assert out.startswith("规则\n\n<few_shot_examples>")
    assert "{{ name }}" in out and "{{ x }}" in out


def test_few_shot_requires_input_or_output():
    with pytest.raises(ValueError):
        FewShotExample()


# ------------------------------------------------------------- system 静态性
def test_system_template_with_variable_placeholder_fails():
    # system 永不接收运行时变量：误写占位符立即失败，而不是静默留空
    pv = _pv(system="Hi {{ name }}")
    with pytest.raises(GatewayError) as ei:
        render_system(pv)
    assert ei.value.code == "PROMPT_VARIABLE_MISSING"


def test_system_sandbox_blocks_unsafe_access():
    pv = _pv(system='{{ "".__class__.__mro__ }}')
    with pytest.raises(GatewayError) as ei:
        render_system(pv)
    assert ei.value.code == "PROMPT_VARIABLE_MISSING"


def test_context_budget_covers_system_and_user_frames():
    pv = _pv(
        system="规则",                       # ~2 tokens
        user="{{ text }}",
        schema={"type": "object", "properties": {"text": {"type": "string"}}},
        budget=3,                            # system+user 合计超 3
    )
    with pytest.raises(GatewayError) as ei:
        enforce_context_budget(pv, render_system(pv), render_user(pv, {"text": "规则规则规则"}))
    assert ei.value.code == "PROMPT_CONTEXT_BUDGET_EXCEEDED"
    assert ei.value.details["estimated_tokens"] > 3


def test_context_budget_none_means_unlimited():
    pv = _pv(system="规则", user="{{ text }}", budget=None,
             schema={"type": "object", "properties": {"text": {"type": "string"}}})
    enforce_context_budget(pv, render_system(pv), render_user(pv, {"text": "x"}))


# ------------------------------------------------------------------ user 帧
def test_user_jinja_expressions_and_loop():
    pv = _pv(
        user="Hi {{ name|upper }}{% if items %}: {% for it in items %}{{ it }}"
        "{% if not loop.last %},{% endif %}{% endfor %}{% endif %}.",
        schema={"type": "object", "properties": {"name": {}, "items": {}}},
    )
    assert render_user(pv, {"name": "sam", "items": ["a", "b"]}) == "Hi SAM: a,b."


def test_undeclared_variable_rejected_even_if_template_unused():
    # 多传未声明变量必须失败，与模板是否引用无关（空 schema 不接受任何变量）
    pv = _pv(user="static text")
    with pytest.raises(GatewayError) as ei:
        render_user(pv, {"injected": "evil"})
    assert ei.value.code == "PROMPT_VARIABLE_UNEXPECTED"
    assert ei.value.details["unexpected"] == ["injected"]


def test_explicit_additional_properties_true_allows_extra_keys():
    pv = _pv(
        user="{{ known }}{% if extra %}-{{ extra }}{% endif %}",
        schema={
            "type": "object",
            "properties": {"known": {"type": "string"}, "extra": {"type": "string"}},
            "required": ["known"],
            "additionalProperties": True,
        },
    )
    out = render_user(pv, {"known": "a", "extra": "b", "rogue": "c"})
    assert out == "a-b"


def test_single_variable_length_limit_enforced():
    pv = _pv(
        user="{{ text }}",
        schema={"type": "object", "properties": {"text": {"type": "string"}}},
        max_variable_chars=5,
    )
    with pytest.raises(GatewayError) as ei:
        render_user(pv, {"text": "abcdef"})
    assert ei.value.code == "PROMPT_CONTEXT_BUDGET_EXCEEDED"
    assert ei.value.details["variable"] == "text"
    assert ei.value.details["limit"] == 5


def test_structured_variable_length_measured_as_json():
    pv = _pv(
        user="{{ blob }}",
        schema={"type": "object", "properties": {"blob": {"type": "object"}}},
        max_variable_chars=4,
    )
    with pytest.raises(GatewayError) as ei:
        render_user(pv, {"blob": {"k": "abcdef"}})
    assert ei.value.code == "PROMPT_CONTEXT_BUDGET_EXCEEDED"


def test_user_strict_undefined_missing_variable():
    pv = _pv(user="Hi {{ name }}", schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    })
    with pytest.raises(GatewayError) as ei:
        render_user(pv, {})
    assert ei.value.code in {"PROMPT_VARIABLE_MISSING", "PROMPT_VARIABLE_TYPE"}


def test_user_variable_type_validation():
    pv = _pv(user="{{ n }}", schema={
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    })
    with pytest.raises(GatewayError) as ei:
        render_user(pv, {"n": "not-int"})
    assert ei.value.code == "PROMPT_VARIABLE_TYPE"


def test_user_sandbox_blocks_unsafe_access():
    pv = _pv(user='{{ "".__class__.__mro__ }}')
    with pytest.raises(GatewayError) as ei:
        render_user(pv, {})
    assert ei.value.code == "PROMPT_VARIABLE_MISSING"
