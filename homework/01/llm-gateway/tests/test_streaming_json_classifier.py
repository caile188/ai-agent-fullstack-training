"""流式 JSON 增量分类器：不完整继续等、确定损坏即止损、完整再交终态 Schema 校验。"""
from __future__ import annotations

import pytest

from app.core.validation.streaming_json import JsonPrefix, classify_json_prefix

INCOMPLETE = JsonPrefix.INCOMPLETE
BROKEN = JsonPrefix.BROKEN
COMPLETE = JsonPrefix.COMPLETE


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  ",
        "```json\n",
        "{",
        '{ "answer": "x',                       # 字符串未闭合
        '{"a": 12.',                            # 数字未完
        '{"a": tru',                            # 字面量未完（true 的前缀）
        '{"a": [1, 2,',                         # 数组未完
        '{"a": {"b":',                          # 嵌套对象未完
        '```json\n{"answer": "你好',            # 围栏内未闭合
    ],
)
def test_incomplete_prefixes_keep_waiting(text):
    assert classify_json_prefix(text) is INCOMPLETE


@pytest.mark.parametrize(
    "text",
    [
        '{"answer": flash, broken}',            # 未加引号的标识符做值
        '{"a": 12x}',                           # 非法数字
        '{"a": truX}',                          # 非法字面量
        '{"a"}',                                # 缺冒号
        '{"a": 1,}',                           # 悬空逗号
        '{"a": 1} trailing prose',             # 闭合后尾随散文
        '{"a": 1}{',                            # 闭合后出现第二个对象
        '{"a": "x\x01"}',                       # 字符串内未转义控制字符
        '[1, 2]',                               # 顶层不是 object
        'hello',                                # 根本不是 JSON
    ],
)
def test_broken_prefixes_detected_early(text):
    # 注意：{"a": 1,} 实际是合法容忍形态，在实现中被判为 BROKEN（悬空逗号不补值）
    assert classify_json_prefix(text) is BROKEN


def test_trailing_comma_then_close_is_broken():
    # 悬空逗号后必须还有元素：,} 不合法
    assert classify_json_prefix('{"a": 1,}') is BROKEN


@pytest.mark.parametrize(
    "text",
    [
        '{"answer": "ok"}',
        ' {"a": 1, "b": [true, false, null]} ',
        '```json\n{"answer": "ok"}\n```',
        '{"obj": {"k": -1.2e3}}',
    ],
)
def test_complete_prefixes(text):
    assert classify_json_prefix(text) is COMPLETE
