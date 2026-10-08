"""结构化输出：出口二次校验，不信任供应商是否真的遵守 response_format。

即使上游声明支持 structured_output，网关仍对最终文本做 JSON / JSON Schema 校验，
供应商违约时给出稳定错误码而非把脏数据返回给调用方。
"""
from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

from app.core.models import ResponseFormat

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def strip_fence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


def repair_instruction(error: str, rf: ResponseFormat) -> str:
    schema = rf.json_schema
    return (
        "Your previous response failed output validation. Return only corrected JSON, "
        "with no Markdown fences or explanation.\n"
        f"Validation error: {error}\n"
        f"Required schema: {json.dumps(schema, ensure_ascii=False) if schema else '(valid JSON object)'}"
    )


class OutputValidationResult:
    def __init__(self, ok: bool, payload: Any = None, error: str | None = None) -> None:
        self.ok = ok
        self.payload = payload
        self.error = error


def validate_output(text: str, rf: ResponseFormat | None) -> OutputValidationResult | None:
    """无 response_format 时返回 None（不校验）。"""
    if rf is None:
        return None

    try:
        payload = json.loads(strip_fence(text))
    except json.JSONDecodeError as e:
        return OutputValidationResult(False, error=f"invalid JSON: {e.msg}")

    if rf.type == "json_object" and not isinstance(payload, dict):
        return OutputValidationResult(False, error="top-level JSON value must be an object")

    if rf.type == "json_schema" and rf.json_schema:
        try:
            jsonschema.validate(instance=payload, schema=rf.json_schema)
        except jsonschema.ValidationError as e:
            return OutputValidationResult(
                False, payload=payload, error=f"schema violation at {list(e.absolute_path)}: {e.message}"
            )

    return OutputValidationResult(True, payload=payload)
