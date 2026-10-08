"""Jinja2 沙箱渲染：表达式/条件/循环可用，未定义即失败，越权访问被拦截，预算受控。"""
from __future__ import annotations

import pytest

from app.core.errors import GatewayError
from app.core.prompts.render import render_system
from app.core.prompts.versions import PromptVersion


def _pv(template: str, *, schema=None, budget=None) -> PromptVersion:
    return PromptVersion(
        name="t",
        version="1.0.0",
        system_template=template,
        variables_schema=schema or {},
        default_logical_model="standard-chat",
        context_budget=budget,
        changelog="x",
    )


def test_jinja_expressions_and_loop():
    pv = _pv(
        "Hi {{ name|upper }}{% if items %}: {% for it in items %}{{ it }}{% if not loop.last %},{% endif %}{% endfor %}{% endif %}."
    )
    out = render_system(pv, {"name": "sam", "items": ["a", "b"]})
    assert out == "Hi SAM: a,b."


def test_strict_undefined_missing_variable():
    pv = _pv("Hi {{ name }}")
    with pytest.raises(GatewayError) as ei:
        render_system(pv, {})
    assert ei.value.code == "PROMPT_VARIABLE_MISSING"


def test_sandbox_blocks_unsafe_access():
    # __class__ 逃逸链在沙箱中被拒绝
    pv = _pv('{{ "".__class__.__mro__ }}')
    with pytest.raises(GatewayError) as ei:
        render_system(pv, {})
    assert ei.value.code == "PROMPT_VARIABLE_MISSING"


def test_context_budget_still_enforced():
    pv = _pv("{{ text }}", budget=2)
    with pytest.raises(GatewayError) as ei:
        render_system(pv, {"text": "abcdefghijklmnop"})
    assert ei.value.code == "PROMPT_CONTEXT_BUDGET_EXCEEDED"
