"""内容寻址：对影响行为的全部字段做规范化序列化后计算 sha256。

content_hash 纳入：模板 / 变量 Schema / few-shot / 输出 Schema /
工具版本 / 默认模型 / 生成参数 / context 预算。状态与 changelog 不计入
（它们不改变模型行为），故同内容重新发布 hash 一致，可去重。
"""
from __future__ import annotations

import copy
import hashlib
import json

from app.core.prompts.versions import PromptVersion


def _canonical(pv: PromptVersion) -> dict:
    data = pv.model_dump(mode="json")
    for key in ("status", "changelog", "content_hash"):
        data.pop(key, None)
    return copy.deepcopy(data)


def compute_hash(pv: PromptVersion) -> str:
    blob = json.dumps(_canonical(pv), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def with_hash(pv: PromptVersion) -> PromptVersion:
    if pv.content_hash:
        return pv
    return pv.model_copy(update={"content_hash": compute_hash(pv)})
