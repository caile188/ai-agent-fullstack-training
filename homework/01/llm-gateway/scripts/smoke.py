#!/usr/bin/env python3
"""端到端冒烟脚本：对一个**运行中**的真实网关发 HTTP 请求，串起六大能力。

与 tests/ 下全 MockTransport 的单测不同，本脚本验证真实装配：
鉴权 → 限流 → 双协议路由 → Prompt 合约 → 结构化/流式 → Trace 落库。

前置：
  1) 网关已启动（make run / docker run），且配置了有效 DEEPSEEK_API_KEY；
  2) 需要触达上游的步骤（chat/stream/structured/eval）才会真正调用 DeepSeek。

环境变量：
  BASE_URL          默认 http://127.0.0.1:8080
  GATEWAY_KEY       网关接入 key（auth.enabled=false 时可留空）
  TENANT_ID         默认 default
  MODEL_CHAT        默认 standard-chat
"""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("GATEWAY_KEY", "")
TENANT = os.environ.get("TENANT_ID", "default")
MODEL = os.environ.get("MODEL_CHAT", "standard-chat")

_passed = 0
_failed = 0


def _headers() -> dict[str, str]:
    h = {"X-Tenant-Id": TENANT}
    if KEY:
        h["Authorization"] = f"Bearer {KEY}"
    return h


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def step(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    client = httpx.Client(base_url=BASE, headers=_headers(), timeout=30.0)

    # 0) 存活与管理健康（不触上游）
    step("0. liveness / admin health")
    try:
        r = client.get("/")
    except httpx.HTTPError as exc:
        print(f"  无法连接网关 {BASE}：{exc}\n  请先启动网关（make run）。")
        return 2
    check("root 200", r.status_code == 200, r.text[:200])
    try:
        r = client.get("/v1/admin/health")
        body = r.json() if r.status_code == 200 else {}
        check("health 200 + circuits", r.status_code == 200 and "circuits" in body, r.text[:200])
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        check("health 200 + circuits", False, str(exc))

    # 1) 非流式对话（真实上游：standard-chat → Anthropic 协议 flash）
    step("1. non-stream chat")
    chat_ok = False
    try:
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "用一句话自我介绍"}],
        })
        chat_ok = r.status_code == 200 and bool(r.json().get("choices"))
        check("chat 200", chat_ok, r.text)
    except httpx.HTTPError as exc:
        check("chat 200", False, str(exc))

    # 2) 流式对话（SSE，验证分片到达且有唯一终态）
    step("2. stream chat (SSE)")
    try:
        saw_delta = saw_done = 0
        with client.stream("POST", "/v1/chat/completions", json={
            "model": MODEL, "stream": True,
            "messages": [{"role": "user", "content": "数到三"}],
        }) as resp:
            check("stream 200", resp.status_code == 200, f"HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    saw_done += 1
                elif payload:
                    evt = json.loads(payload)
                    choices = evt.get("choices") or []
                    if choices and choices[0].get("delta", {}).get("content"):
                        saw_delta += 1
        check("stream deltas > 0", saw_delta > 0, f"deltas={saw_delta}")
        check("single terminal DONE", saw_done == 1, f"done={saw_done}")
    except httpx.HTTPError as exc:
        check("stream", False, str(exc))

    # 3) 结构化输出（json_object；非流式会做出口校验）
    step("3. structured output (json_object)")
    try:
        r = client.post("/v1/chat/completions", json={
            "model": "standard-json",
            "messages": [{"role": "user", "content": "返回一个 JSON，含 answer 字段"}],
            "response_format": {"type": "json_object"},
        })
        ok = False
        if r.status_code == 200:
            text = r.json()["choices"][0]["message"]["content"]
            json.loads(text)
            ok = True
        check("structured valid JSON", ok, r.text)
    except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
        check("structured valid JSON", False, str(exc))

    # 4) Prompt 合约生命周期（不触上游：创建 / 发布 / 列表 / 沙箱渲染）
    step("4. prompt contract lifecycle")
    pname = "smoke-greeter"
    contract = {
        "name": pname, "version": "1.0.0",
        "system_template": "你是{{ role }}，用{{ lang }}回答。",
        "variables_schema": {
            "type": "object",
            "properties": {"role": {"type": "string"}, "lang": {"type": "string"}},
            "required": ["role", "lang"],
        },
        "default_logical_model": MODEL,
    }
    try:
        r = client.post("/v1/prompts", json=contract)
        check("create version", r.status_code == 200, r.text)
        r = client.post(f"/v1/prompts/{pname}/versions/1.0.0/publish")
        check("publish", r.status_code == 200, r.text)
        r = client.get(f"/v1/prompts/{pname}/versions")
        check("list versions", r.status_code == 200 and len(r.json().get("versions", [])) >= 1, r.text)
        r = client.post(f"/v1/prompts/{pname}/render",
                        json={"version": "1.0.0",
                              "variables": {"role": "向导", "lang": "中文"}})
        rendered = r.json().get("rendered_system", "") if r.status_code == 200 else ""
        check("sandbox render", r.status_code == 200 and "向导" in rendered and "中文" in rendered, r.text)
        # 缺变量应被 StrictUndefined 拒绝
        r2 = client.post(f"/v1/prompts/{pname}/render", json={"variables": {}})
        check("missing variable rejected", r2.status_code >= 400, f"got {r2.status_code}")
    except httpx.HTTPError as exc:
        check("prompt lifecycle", False, str(exc))

    # 5) 用已发布 Prompt 发起调用（渲染入 trace，禁止再带 system 消息）
    step("5. invoke via published prompt")
    try:
        r = client.post("/v1/chat/completions", json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "打个招呼"}],
            "prompt": {"name": pname, "version": "1.0.0",
                       "variables": {"role": "向导", "lang": "中文"}},
        })
        check("prompt-based chat 200", r.status_code == 200, r.text)
    except httpx.HTTPError as exc:
        check("prompt-based chat 200", False, str(exc))

    # 6) Eval 回放（触上游；对已发布版本跑 eval_set，这里合约未带 eval_set，验证端点可用）
    step("6. observability: traces & cost")
    try:
        r = client.get("/v1/admin/traces", params={"limit": 5})
        traces = r.json().get("traces", []) if r.status_code == 200 else []
        check("list traces", r.status_code == 200 and len(traces) >= 1, r.text)
        if traces:
            tid = traces[0]["trace_id"]
            r = client.get(f"/v1/admin/traces/{tid}")
            detail = r.json()
            has_segments = all(
                k in detail for k in ("render_ms", "route_ms", "total_ms")
            )
            check("trace has latency segments", has_segments, str(sorted(detail.keys())))
            check("trace records calls", len(detail.get("calls", [])) >= 1, "")
    except (httpx.HTTPError, KeyError) as exc:
        check("observability", False, str(exc))

    print(f"\n结果：{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
