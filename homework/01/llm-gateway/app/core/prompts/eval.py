"""Eval 判定（纯函数）：对一次输出评估是否通过用例门槛。

实际"跑用例"由 services/eval_runner 调用 Gateway 完成；这里只做可离线回放的判定。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.core.prompts.versions import EvalCase


class CaseResult(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)


def evaluate_output(text: str, case: EvalCase) -> CaseResult:
    reasons: list[str] = []

    for needle in case.must_contain:
        if needle not in text:
            reasons.append(f"missing required text: {needle!r}")
    for needle in case.must_not_contain:
        if needle in text:
            reasons.append(f"forbidden text present: {needle!r}")

    if case.expect_json_schema is not None:
        import jsonschema

        try:
            payload = json.loads(text)
            jsonschema.validate(instance=payload, schema=case.expect_json_schema)
        except json.JSONDecodeError as e:
            reasons.append(f"invalid JSON: {e.msg}")
        except jsonschema.ValidationError as e:
            reasons.append(f"schema violation: {e.message}")

    return CaseResult(passed=not reasons, reasons=reasons)
