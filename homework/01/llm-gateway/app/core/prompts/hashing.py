"""实发内容指纹：对一次调用真正渲染出的两帧文本计算 sha256。

系统只保留这一个 hash——它标识“本次实际发送给模型的内容”，用于事故排查、
相同实发输入聚类与事后复算核对。合约版本本身以 (name, version) 唯一标识，
不再单列合约定义 hash。
"""
from __future__ import annotations

import hashlib
import json


def compute_rendered_hash(system_text: str, user_text: str = "") -> str:
    blob = json.dumps(
        {"system": system_text, "user": user_text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
