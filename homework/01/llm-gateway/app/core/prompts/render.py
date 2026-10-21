"""Prompt 渲染：变量 Schema 校验 -> Jinja2 沙箱渲染 -> context 预算检查。

规则与数据分层：
  - system 帧（render_system）只渲染静态 system_template + few-shot 范例，
    不接收任何运行时变量；模板里误写 {{ var }} 会立即失败。
  - user 帧（render_user）承载全部运行时变量，是不可信数据的唯一去处，
    标签分区由 user_template 作者在模板内写明。

使用 SandboxedEnvironment 杜绝模板内任意属性/越权访问（Prompt 内容可来自外部发布），
StrictUndefined 让任何未提供变量立即失败，绝不静默把占位符留在文本里或拼入未定义内容。
"""
from __future__ import annotations

import json
from typing import Any

import jinja2
import jsonschema
import tiktoken
from jinja2.sandbox import SandboxedEnvironment, SecurityError

from app.core.errors import GatewayError
from app.core.prompts.versions import FewShotExample, PromptVersion

# 只读沙箱：屏蔽危险内置/属性访问；未定义变量即抛 UndefinedError
_ENV = SandboxedEnvironment(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)

# token 计数编码器：模块级单例（加载 rank 表有成本，只初始化一次）。
# 用 BPE 真实分词计数，替代过去按字符数的粗估；cl100k_base 作为跨模型的通用基线。
_ENCODER = tiktoken.get_encoding("cl100k_base")


def validate_variables(pv: PromptVersion, variables: dict[str, Any]) -> None:
    schema = pv.variables_schema or {}

    # 严格变量契约：除非作者显式 additionalProperties:true，否则只允许 properties
    # 中声明的键。空 schema 意味着不接受任何变量，多传即失败（不靠模板是否引用决定）。
    if schema.get("additionalProperties", False) is not True:
        allowed = set((schema.get("properties") or {}).keys())
        unexpected = sorted(set(variables.keys()) - allowed)
        if unexpected:
            raise GatewayError(
                "PROMPT_VARIABLE_UNEXPECTED",
                f"undeclared prompt variables: {', '.join(unexpected)}",
                details={"unexpected": unexpected, "allowed": sorted(allowed)},
            )

    try:
        jsonschema.validate(instance=variables, schema=schema)
    except jsonschema.ValidationError as e:
        raise GatewayError(
            "PROMPT_VARIABLE_TYPE",
            e.message,
            details={"path": list(e.absolute_path)},
        ) from e

    # 单变量长度预算：字符串按字符数，容器按序列化字符数，防止整篇外部资料无界注入
    for name, value in variables.items():
        size = len(value) if isinstance(value, str) else len(
            json.dumps(value, ensure_ascii=False)
        )
        if size > pv.max_variable_chars:
            raise GatewayError(
                "PROMPT_CONTEXT_BUDGET_EXCEEDED",
                f"variable {name!r} length {size} exceeds limit {pv.max_variable_chars}",
                details={"variable": name, "length": size, "limit": pv.max_variable_chars},
            )


def estimate_tokens(text: str) -> int:
    """用 tiktoken(cl100k_base) BPE 真实分词计算 token 数；空文本为 0。"""
    if not text:
        return 0
    return len(_ENCODER.encode(text))


def _sandbox_render(template: str, variables: dict[str, Any]) -> str:
    try:
        return _ENV.from_string(template).render(**variables)
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


def render_few_shot(examples: list[FewShotExample]) -> str | None:
    """把静态范例渲染成带标签分区的文本块；范例不做 Jinja 渲染。缺 input/output 的标签省略。"""
    if not examples:
        return None
    blocks: list[str] = []
    for ex in examples:
        parts = ["<example>"]
        if ex.input:
            parts.append(f"<input>\n{ex.input}\n</input>")
        if ex.output:
            parts.append(f"<output>\n{ex.output}\n</output>")
        parts.append("</example>")
        blocks.append("\n".join(parts))
    return "<few_shot_examples>\n" + "\n".join(blocks) + "\n</few_shot_examples>"


def render_system(pv: PromptVersion) -> str:
    """渲染静态规则帧：system_template 不接收变量，并追加 few-shot 范例区块。"""
    rendered = _sandbox_render(pv.system_template, {})
    few_shot = render_few_shot(pv.few_shot)
    if few_shot:
        rendered = f"{rendered}\n\n{few_shot}"
    return rendered


def render_user(pv: PromptVersion, variables: dict[str, Any]) -> str:
    """渲染运行时数据帧：先做变量 Schema/长度校验，再沙箱渲染 user_template。"""
    validate_variables(pv, variables)
    return _sandbox_render(pv.user_template, variables)


def enforce_context_budget(
    pv: PromptVersion, system_text: str, user_text: str = ""
) -> None:
    """总预算校验：合约产出的 system（含 few-shot）+ user 数据帧合计 token 不得超预算。"""
    if pv.context_budget is None:
        return
    used = estimate_tokens(system_text) + estimate_tokens(user_text)
    if used > pv.context_budget:
        raise GatewayError(
            "PROMPT_CONTEXT_BUDGET_EXCEEDED",
            f"rendered prompt ~{used} tokens exceeds budget {pv.context_budget}",
            details={"estimated_tokens": used, "budget": pv.context_budget},
        )
