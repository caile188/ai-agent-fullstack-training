"""流式 JSON 增量可行性检查（不做 Schema 校验）。

原则：流式 JSON 必须增量解析，不能等所有 chunk 到齐；但部分 JSON 无法做
Schema 校验。本模块对"截至目前的前缀"做语法可行性分类：

  - INCOMPLETE：前缀不完整但仍可恢复（开引号、未闭合括号、字面量未完）-> 继续等
  - BROKEN    ：语法已确定不可恢复（非法 token、闭合后出现尾随内容）-> 提前止损
  - COMPLETE  ：顶层对象已完整且无多余内容（是否满足 Schema 仍须终态校验）

仅识别顶层为 JSON object 的输出（json_object / json_schema 两种模式都要求）。
支持 ```json 代码围栏包裹（终态校验同样会剥离围栏）。
"""
from __future__ import annotations

import enum
import re

_FENCE_OPEN = re.compile(r"^\s*```(?:json)?[ \t]*\n?", re.IGNORECASE)
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_WS = " \t\n\r"
# 栈帧：[kind, expectation]
#   kind=obj : expectation ∈ key(等引号或}) / colon / value / comma
#   kind=arr : expectation ∈ value(等值或]) / comma


class JsonPrefix(enum.Enum):
    INCOMPLETE = "incomplete"
    BROKEN = "broken"
    COMPLETE = "complete"


def classify_json_prefix(raw: str) -> JsonPrefix:
    m = _FENCE_OPEN.match(raw)
    s = raw[m.end():] if m else raw
    n = len(s)
    i = 0

    def ws(j: int) -> int:
        while j < n and s[j] in _WS:
            j += 1
        return j

    i = ws(i)
    if i >= n:
        return JsonPrefix.INCOMPLETE
    if s[i] != "{":
        return JsonPrefix.BROKEN

    stack: list[list[str]] = [["obj", "key"]]
    i += 1
    while stack:
        i = ws(i)
        if i >= n:
            return JsonPrefix.INCOMPLETE
        ch = s[i]
        kind, exp = stack[-1]

        # ---- 对象/数组的结构性分隔符
        if kind == "obj" and exp in ("key", "key_required"):
            if ch == "}":
                if exp == "key_required":
                    return JsonPrefix.BROKEN      # 悬空逗号：,} 非法
                stack.pop()
                i += 1
                continue
            if ch != '"':
                return JsonPrefix.BROKEN
            j, status = _scan_string(s, i, n)
            if status is not None:
                return status
            i = j
            stack[-1][1] = "colon"
            continue
        elif kind == "obj" and exp == "colon":
            if ch != ":":
                return JsonPrefix.BROKEN
            stack[-1][1] = "value"
            i += 1
            continue
        elif exp == "comma":
            close = "}" if kind == "obj" else "]"
            if ch == close:
                stack.pop()
                i += 1
                continue
            if ch != ",":
                return JsonPrefix.BROKEN
            stack[-1][1] = "key_required" if kind == "obj" else "value"
            i += 1
            continue

        # ---- 扫描一个 value（obj exp=value 或 arr exp=value）
        i = ws(i)
        if i >= n:
            return JsonPrefix.INCOMPLETE
        ch = s[i]

        if ch == '"':
            j, status = _scan_string(s, i, n)
            if status is not None:
                return status
            i = j
        elif ch == "{":
            stack[-1][1] = "comma"            # 容器本身也是一个 value
            stack.append(["obj", "key"])
            i += 1
            continue
        elif ch == "[":
            stack[-1][1] = "comma"
            stack.append(["arr", "value"])
            i += 1
            continue
        elif ch in "-0123456789":
            j = i
            while j < n and s[j] in "0123456789-+.eE":
                j += 1
            token = s[i:j]
            if j >= n:
                return JsonPrefix.INCOMPLETE
            if not _NUMBER.match(token):
                return JsonPrefix.BROKEN
            i = j
        elif ch in "tfn":
            j = i
            while j < n and s[j].isalpha():
                j += 1
            token = s[i:j]
            if j >= n:
                if not any(lit.startswith(token) for lit in ("true", "false", "null")):
                    return JsonPrefix.BROKEN
                return JsonPrefix.INCOMPLETE
            if token not in ("true", "false", "null"):
                return JsonPrefix.BROKEN
            i = j
        else:
            return JsonPrefix.BROKEN

        # value 结束：父帧进入等逗号状态
        stack[-1][1] = "comma"

    # 栈空：顶层对象闭合。剩余内容只允许空白 + 可选关闭围栏
    tail = s[i:].strip()
    if not tail or tail.startswith("```"):
        return JsonPrefix.COMPLETE
    return JsonPrefix.BROKEN


def _scan_string(s: str, i: int, n: int) -> tuple[int, JsonPrefix | None]:
    """s[i] 为开引号。返回 (闭引号后位置, None) 或 (任意位置, 判定)。"""
    j = i + 1
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1, None
        if ord(c) < 0x20:
            return i, JsonPrefix.BROKEN  # 字符串内未转义控制字符
        j += 1
    return i, JsonPrefix.INCOMPLETE
