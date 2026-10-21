"""内存假上游：模拟 anthropic / openai 两套官方 SDK 的调用表面。

适配器只依赖 SDK 客户端的属性/异步流协议，这里用 SimpleNamespace 构造等价对象，
不发真实网络。可注入 4xx/5xx 失败（抛真实 SDK 异常）与结构化违约文本。
calls 记录每次调用：{"protocol": ..., "kwargs": ...}。
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import anthropic
import openai

FLASH_MODEL = "deepseek-v4-flash-20250801"
PRO_MODEL = "deepseek-v4-pro-250801"


class FakeHeaders(dict):
    """大小写不敏感的响应头（等价 httpx.Headers.get）。"""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _StreamContext:
    """SDK 的 async with + async for 流上下文。"""

    def __init__(self, on_enter, events):
        self._on_enter = on_enter
        self._events = events
        self._i = 0

    async def __aenter__(self):
        if self._on_enter is not None:
            raise self._on_enter
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        evt = self._events[self._i]
        self._i += 1
        return evt


def _status_error(mod, status: int, body: dict, retry_after: int | None):
    headers = FakeHeaders()
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    response = NS(status_code=status, headers=headers, request=NS())
    return mod.APIStatusError("upstream error", response=response, body=body)


def _split_delta(text: str) -> tuple[str, str]:
    """把流式正文切成两个片段，模拟分片到达。"""
    mid = max(1, len(text) // 2)
    return text[:mid], text[mid:]


class FakeUpstream:
    def __init__(self) -> None:
        # endpoint 关键字(flash/pro) -> 故障配置
        self.fail: dict[str, dict] = {}
        # endpoint 关键字 -> 结构化请求前 N 次返回非法 JSON（模拟供应商违约）
        self.bad_json: dict[str, int] = {}
        # endpoint 关键字集合：流式时掐掉终止事件（message_stop / response.completed），
        # 模拟上游无报错却提前关闭连接
        self.truncate_stream: set[str] = set()
        self.calls: list[dict] = []
        self.anthropic = _FakeAnthropicClient(self)
        self.openai = _FakeOpenAIClient(self)

    def attach(self, registry) -> None:
        """把两个假 SDK 客户端注入注册表，替换真实客户端。"""
        registry.replace_client("deepseek-anthropic", self.anthropic)
        registry.replace_client("deepseek-responses", self.openai)

    def set_fail(self, key: str, status: int = 500, *, times: int | None = None,
                 retry_after: int | None = None) -> None:
        self.fail[key] = {"status": status, "times": times, "retry_after": retry_after}

    def reset_fail(self, key: str) -> None:
        self.fail.pop(key, None)

    def set_bad_json(self, key: str, times: int) -> None:
        self.bad_json[key] = times

    def set_truncated_stream(self, key: str, truncated: bool = True) -> None:
        if truncated:
            self.truncate_stream.add(key)
        else:
            self.truncate_stream.discard(key)

    def _should_fail(self, key: str) -> bool:
        cfg = self.fail.get(key)
        if not cfg:
            return False
        if cfg["times"] is None:
            return True
        if cfg["times"] > 0:
            cfg["times"] -= 1
            return True
        return False

    def _failure(self, key: str, mod, body: dict) -> Exception:
        cfg = self.fail[key]
        return _status_error(mod, cfg["status"], body, cfg.get("retry_after"))

    def _text_for(self, key: str, kwargs: dict) -> str:
        # 结构化输出场景：按脚本先返回非法 JSON（供应商违约），随后回吐合法 JSON
        if kwargs.get("text") or kwargs.get("system"):
            if self.bad_json.get(key, 0) > 0:
                self.bad_json[key] -= 1
                return "```json\n{\"answer\": " + key + ", broken"
            return json.dumps({"answer": key, "ok": True})
        return f"{key}-ok"

    # ---------------------------------------------------------- anthropic
    def anthropic_message(self, kwargs: dict):
        self.calls.append({"protocol": "anthropic", "kwargs": kwargs})
        if self._should_fail("flash"):
            raise self._failure("flash", anthropic, {"error": {"type": "overloaded", "message": "busy"}})
        text = self._text_for("flash", kwargs)
        return NS(
            id="m_1", model=FLASH_MODEL, stop_reason="end_turn",
            content=[NS(type="text", text=text)],
            usage=NS(input_tokens=8, output_tokens=4, cache_read_input_tokens=1),
        )

    def anthropic_stream(self, kwargs: dict):
        self.calls.append({"protocol": "anthropic", "kwargs": kwargs})
        if self._should_fail("flash"):
            err = self._failure("flash", anthropic, {"error": {"type": "overloaded", "message": "busy"}})
            return _StreamContext(err, [])
        d1, d2 = _split_delta("Hello")
        events = [
            NS(type="message_start", message=NS(
                id="m_1", model=FLASH_MODEL,
                usage=NS(input_tokens=8, output_tokens=0, cache_read_input_tokens=1))),
            NS(type="content_block_start", index=0,
               content_block=NS(type="text", id=None, name=None)),
            NS(type="content_block_delta", index=0,
               delta=NS(type="text_delta", text=d1, partial_json=None)),
            NS(type="content_block_delta", index=0,
               delta=NS(type="text_delta", text=d2, partial_json=None)),
            NS(type="content_block_stop", index=0),
            NS(type="message_delta",
               delta=NS(stop_reason="end_turn"),
               usage=NS(output_tokens=4)),
            NS(type="message_stop"),
        ]
        if "flash" in self.truncate_stream:
            # 收到正文（含 message_start 用量）后连接结束，但无 message_delta/message_stop
            events = events[:5]
        return _StreamContext(None, events)

    # ---------------------------------------------------------- responses
    def openai_response(self, kwargs: dict):
        self.calls.append({"protocol": "openai_responses", "kwargs": kwargs})
        if self._should_fail("pro"):
            raise self._failure("pro", openai, {"error": {"type": "server", "message": "boom"}})
        text = self._text_for("pro", kwargs)
        return self._responses_object(text)

    def openai_stream(self, kwargs: dict):
        self.calls.append({"protocol": "openai_responses", "kwargs": kwargs})
        if self._should_fail("pro"):
            err = self._failure("pro", openai, {"error": {"type": "server", "message": "boom"}})
            return _StreamContext(err, [])
        d1, d2 = _split_delta("Hello")
        events = [
            NS(type="response.output_text.delta", delta=d1),
            NS(type="response.output_text.delta", delta=d2),
            NS(type="response.completed", response=self._responses_object("Hello")),
        ]
        if "pro" in self.truncate_stream:
            # 只有正文 delta，连接结束但无 response.completed
            events = events[:2]
        return _StreamContext(None, events)

    @staticmethod
    def _responses_object(text: str):
        usage = NS(
            input_tokens=10, output_tokens=5,
            input_tokens_details=NS(cached_tokens=2),
            output_tokens_details=NS(reasoning_tokens=1),
        )
        return NS(
            id="resp_1", status="completed", model=PRO_MODEL, incomplete_details=None,
            output=[NS(type="message",
                       content=[NS(type="output_text", text=text)])],
            usage=usage,
        )


class _FakeAnthropicMessages:
    def __init__(self, fake: FakeUpstream) -> None:
        self._fake = fake

    async def create(self, **kwargs):
        return self._fake.anthropic_message(kwargs)

    def stream(self, **kwargs):
        return self._fake.anthropic_stream(kwargs)


class _FakeAnthropicClient:
    def __init__(self, fake: FakeUpstream) -> None:
        self.messages = _FakeAnthropicMessages(fake)

    async def close(self) -> None:
        pass


class _FakeResponses:
    def __init__(self, fake: FakeUpstream) -> None:
        self._fake = fake

    async def create(self, **kwargs):
        return self._fake.openai_response(kwargs)

    def stream(self, **kwargs):
        return self._fake.openai_stream(kwargs)


class _FakeOpenAIClient:
    def __init__(self, fake: FakeUpstream) -> None:
        self.responses = _FakeResponses(fake)

    async def close(self) -> None:
        pass
