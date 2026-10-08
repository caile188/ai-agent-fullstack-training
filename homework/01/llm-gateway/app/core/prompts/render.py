"""Prompt 渲染：变量 Schema 校验 -> Jinja2 沙箱渲染 -> context 预算检查。

使用 SandboxedEnvironment 杜绝模板内任意属性/越权访问（Prompt 内容可来自外部发布），
StrictUndefined 让任何未提供变量立即失败，绝不静默把占位符留在文本里或拼入未定义内容。
"""
from __future__ import annotations

from typing import Any

import jinja2
import jsonschema
from jinja2.sandbox import SandboxedEnvironment, SecurityError

from app.core.errors import GatewayError
from app.core.prompts.versions import PromptVersion

# 只读沙箱：屏蔽危险内置/属性访问；未定义变量即抛 UndefinedError
_ENV = SandboxedEnvironment(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)


def validate_variables(pv: PromptVersion, variables: dict[str, Any]) -> None:
    if not pv.variables_schema:
        return
    try:
        jsonschema.validate(instance=variables, schema=pv.variables_schema)
    except jsonschema.ValidationError as e:
        raise GatewayError(
            "PROMPT_VARIABLE_TYPE",
            e.message,
            details={"path": list(e.absolute_path)},
        ) from e


def estimate_tokens(text: str) -> int:
    """粗估 token：中文约 1 字/token，英文约 4 字符/token，取两者上界估计。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def render_system(pv: PromptVersion, variables: dict[str, Any]) -> str:
    validate_variables(pv, variables)
    try:
        template = _ENV.from_string(pv.system_template)
        rendered = template.render(**variables)
    except jinja2.UndefinedError as e:
        # StrictUndefined：缺变量 / 访问未定义字段
        key = getattr(e, "name", None) or str(e)
        raise GatewayError(
            "PROMPT_VARIABLE_MISSING",
            str(e),
            details={"variable": str(key)},
        ) from e
    except SecurityError as e:
        # 沙箱拦截了越权/危险操作
        raise GatewayError("PROMPT_VARIABLE_MISSING", f"template blocked by sandbox: {e}") from e
    except jinja2.TemplateSyntaxError as e:
        raise GatewayError("PROMPT_VARIABLE_MISSING", f"template syntax error: {e}") from e

    if pv.context_budget is not None:
        used = estimate_tokens(rendered)
        if used > pv.context_budget:
            raise GatewayError(
                "PROMPT_CONTEXT_BUDGET_EXCEEDED",
                f"rendered prompt ~{used} tokens exceeds budget {pv.context_budget}",
                details={"estimated_tokens": used, "budget": pv.context_budget},
            )
    return rendered
