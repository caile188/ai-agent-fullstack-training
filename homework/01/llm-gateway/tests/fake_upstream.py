"""内存假上游：用 httpx.MockTransport 模拟两种协议，可注入失败/非法 JSON。"""
from __future__ import annotations

import json

import httpx


class FakeUpstream:
    def __init__(self) -> None:
        # endpoint 关键字(pro/flash) -> 故障配置
        self.fail: dict[str, dict] = {}
        # endpoint 关键字 -> 结构化请求前 N 次返回非法 JSON（模拟供应商违约）
        self.bad_json: dict[str, int] = {}
        self.calls: list[httpx.Request] = []

    def set_fail(self, key: str, status: int = 500, *, times: int | None = None,
                 retry_after: int | None = None) -> None:
        self.fail[key] = {"status": status, "times": times, "retry_after": retry_after}

    def reset_fail(self, key: str) -> None:
        self.fail.pop(key, None)

    def set_bad_json(self, key: str, times: int) -> None:
        self.bad_json[key] = times

    # ---------------------------------------------------------- responses
    def _responses(self, body: dict, stream: bool) -> httpx.Response:
        if self._should_fail("pro"):
            return self._failure("pro", {"error": {"type": "server", "message": "boom"}})
        text = self._text_for("pro", body)
        if stream:
            d1, d2 = _split_delta(text if (body.get("text") or body.get("system")) else "Hello")
            sse = (
                f'data: {{"type":"response.output_text.delta","delta":{json.dumps(d1)}}}\n\n'
                f'data: {{"type":"response.output_text.delta","delta":{json.dumps(d2)}}}\n\n'
                'data: {"type":"response.completed","response":{'
                '"id":"resp_1","status":"completed","model":"deepseek-v4-pro-250801",'
                f'"output":[{{"type":"message","content":[{{"type":"output_text","text":{json.dumps(text)}}}]}}],'
                '"usage":{"input_tokens":10,"output_tokens":5,'
                '"input_tokens_details":{"cached_tokens":2},'
                '"output_tokens_details":{"reasoning_tokens":1}}}}\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "status": "completed",
            "model": "deepseek-v4-pro-250801",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 2},
                      "output_tokens_details": {"reasoning_tokens": 1}},
        })

    # ---------------------------------------------------------- anthropic
    def _anthropic(self, body: dict, stream: bool) -> httpx.Response:
        if self._should_fail("flash"):
            return self._failure("flash", {"error": {"type": "overloaded", "message": "busy"}})
        text = self._text_for("flash", body)
        if stream:
            d1, d2 = _split_delta(text if (body.get("text") or body.get("system")) else "Hello")
            sse = (
                'event: message_start\n'
                'data: {"type":"message_start","message":{"id":"m_1","model":"deepseek-v4-flash-20250801",'
                '"usage":{"input_tokens":8,"output_tokens":0,"cache_read_input_tokens":1}}}\n\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":{json.dumps(d1)}}}}}\n\n'
                f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":{json.dumps(d2)}}}}}\n\n'
                'data: {"type":"content_block_stop","index":0}\n\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n'
                'data: {"type":"message_stop"}\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
        return httpx.Response(200, json={
            "id": "m_1", "type": "message", "role": "assistant",
            "model": "deepseek-v4-flash-20250801", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 8, "output_tokens": 4, "cache_read_input_tokens": 1},
        })

    def _text_for(self, key: str, body: dict) -> str:
        # 结构化输出场景：按脚本先返回非法 JSON（供应商违约），随后回吐合法 JSON
        if body.get("text") or body.get("system"):
            if self.bad_json.get(key, 0) > 0:
                self.bad_json[key] -= 1
                return "```json\n{\"answer\": " + key + ", broken"
            return json.dumps({"answer": key, "ok": True})
        return f"{key}-ok"

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

    def _failure(self, key: str, payload: dict) -> httpx.Response:
        cfg = self.fail[key]
        headers = {}
        if cfg.get("retry_after") is not None:
            headers["retry-after"] = str(cfg["retry_after"])
        return httpx.Response(cfg["status"], headers=headers, json=payload)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        body = json.loads(request.content or b"{}")
        stream = bool(body.get("stream"))
        if request.url.path.endswith("/responses"):
            return self._responses(body, stream)
        return self._anthropic(body, stream)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _split_delta(text: str) -> tuple[str, str]:
    """把流式正文切成两个 UTF-8 安全片段，模拟分片到达。"""
    mid = max(1, len(text) // 2)
    return text[:mid], text[mid:]
