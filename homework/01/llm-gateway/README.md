# Governed LLM Gateway

一个**可治理的多供应商 LLM 网关**：对外是 OpenAI 兼容的 `/v1/chat/completions`，
对内把两家差异最大的协议收敛为统一事件模型，并在路由、韧性、Prompt 合约、
结构化输出、可观测、成本、限流鉴权等环节提供强治理。

- **Python 3.11+** · FastAPI · httpx · Pydantic v2 · aiosqlite · Jinja2 沙箱
- 分层（依赖单向 `api → services → core`）：
  - `app/api` HTTP 边界（协议在边界终止，未知字段立即拒绝）
  - `app/services` 编排（网关流程、执行韧性、适配器、可观测、DB）
  - `app/core` 纯领域逻辑（能力/路由/限流/Prompt/校验/错误）

## 双协议固定映射

| 逻辑模型首选 | 实际模型 | 上游协议 | 路径 | 鉴权头 |
| --- | --- | --- | --- | --- |
| `premium-chat` | DeepSeek v4 **pro** | OpenAI **Responses** | `/responses` | `Authorization: Bearer` |
| `standard-chat` / `standard-json` | DeepSeek v4 **flash** | Anthropic **Messages** | `/anthropic/v1/messages` | `x-api-key` |

适配器把两家 SSE/非流式响应翻译成统一内部模型；出口为 OpenAI 兼容信封
（`model` 为实际模型），网关治理扩展统一收在独立 `gateway` 命名空间
（`requested_model` 逻辑模型、`resolved_model` 实际模型、`endpoint_id`、
`provider_id`、`output_valid`、`upstream_request_id`），不与兼容字段混淆。
SSE 每帧的元数据同样放在帧内 `gateway` 对象中。

## 核心能力

1. **能力路由 + 兜底链**：候选 → 能力过滤 → 策略过滤（区域/数据驻留）→ 加权评分；
   fallback 链按 priority/score 锁定。首选策略可选 `score` 或
   `weighted_round_robin`（同优先级内 nginx 式平滑加权轮询）。
2. **韧性**：总 deadline 预算 + 重试三重约束、指数抖动退避、429 `Retry-After`
   （delta-seconds / HTTP-date）、熔断（开/半开/闭）、每端点并发闸、流式"首包门"。
3. **Prompt 即合约**：不可变 + 内容寻址版本，发布后只能出新版本；Jinja2
   `SandboxedEnvironment` + `StrictUndefined` 渲染；变量 JSON Schema 与上下文预算。
   使用 Prompt 时禁止再传 `system` 消息。
4. **结构化输出**：
   - 非流式：出口 JSON/Schema 校验，失败在**同一端点**做有限次 repair 重生成
     （不重试、不 fallback、不拼接文本），额外调用独立计费。
   - 流式：中途不校验半片 JSON，仅做增量语法可行性分类（确定不可恢复即提前
     止损），终态对完整文本做**一次**出口校验；脏输出是唯一错误终态（取代
     finish），且已产出 token 仍计费（不 repair）。
   - 流式唯一终态：finish / error / **cancelled** 三者居其一。客户端断连
     （ASGI 取消或 `is_disconnected` 探活）落 `CLIENT_CANCELLED`(499) 终态，
     已产出 token 同样计费，随后保持取消语义。
5. **可观测 + 成本**：trace/run/step/call 四级 ID；分段延迟
   （queue/render/route/connect/ttft/generation/total）；成本台账；**价格版本化**
   （配置价格快照内容寻址，cost 行绑定 `price_version`）。
6. **接入治理**：多 Key 常量时间比较鉴权、按 **(租户, 逻辑模型)** 独立的令牌桶限流
   （模型级可覆盖租户默认，超限返回 429）、租户预算/区域/并发策略、
   **幂等键去重**（同键异指纹 409，pending 409，completed 重放）。

## 快速开始

```bash
cp .env.example .env            # 填入 DEEPSEEK_API_KEY；需要时改 GATEWAY_KEYS
make dev                        # 建 venv 并安装运行 + 测试依赖
make run                        # http://0.0.0.0:8080 （自动迁移 SQLite）
```

不使用 make：

```bash
python -m venv .venv && . .venv/bin/activate
pip install ".[dev]"
export DEEPSEEK_API_KEY=sk-...
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

配置文件默认 `gateway.yaml`，可用环境变量 `GATEWAY_CONFIG` 覆盖；密钥通过
`${ENV}` / `${ENV:-default}` 插值，不入库不入日志（`SecretStr`）。

### 测试与冒烟

```bash
make test       # 全部单测走 httpx MockTransport，不触真实网络
make smoke      # 对运行中的真实网关端到端联调（需先 make run）
```

`scripts/smoke.py` 串起存活、双协议非流式/流式/结构化、Prompt 生命周期、
基于 Prompt 的调用、Trace/成本分段，共 16 项断言。可用 `BASE_URL` /
`GATEWAY_KEY` / `TENANT_ID` / `MODEL_CHAT` 覆盖目标。

## Docker

```bash
docker build -t governed-llm-gateway:latest .
docker run --rm -p 8080:8080 --env-file .env \
  -v "$PWD/data:/app/data" governed-llm-gateway:latest
# 或 make docker-build && make docker-run
```

多阶段构建，运行镜像以非 root（uid 10001）运行，SQLite 落 `/app/data` 卷。

## API 速览

鉴权关闭（默认 `auth.enabled=false`，本地开发）时以下命令可直接用；
生产在 `gateway.yaml` 置 `enabled: true` 并带 `Authorization: Bearer $GATEWAY_KEY`。
多租户用 `X-Tenant-Id` 头（缺省 `default`）。

### 1. 非流式对话（standard-chat → Anthropic 协议 flash）

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"standard-chat",
       "messages":[{"role":"user","content":"用一句话自我介绍"}]}'
```

### 2. 流式对话（SSE）

```bash
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"premium-chat","stream":true,
       "messages":[{"role":"user","content":"数到三"}]}'
# OpenAI Chat Completions 兼容 chunk 帧：
# 首帧 delta.role=assistant（带实际 model 与 gateway 元数据）
#   -> 若干 delta.content 帧 -> finish_reason 终帧 -> usage 帧(choices=[]) -> data: [DONE]
# 出错时只有一帧 {"error": {...}} 作为唯一错误终态，随后同样 [DONE]
# requested_model / resolved_model / endpoint_id 等元数据在每帧的 gateway 对象中
# （首帧为配置名义模型，finish 帧为上游回传的精确版本串）
```

### 3. 结构化输出（json_object / json_schema）

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"standard-json",
       "messages":[{"role":"user","content":"返回含 answer 字段的 JSON"}],
       "response_format":{"type":"json_object"}}'
```

非流式响应为 OpenAI 兼容信封，扩展字段一律在 `gateway` 命名空间：

```json
{
  "id": "chatcmpl_...",
  "object": "chat.completion",
  "model": "deepseek-v4-flash-20250801",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."},
               "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12,
            "cached_tokens": 1, "reasoning_tokens": 0},
  "gateway": {"requested_model": "standard-chat",
              "resolved_model": "deepseek-v4-flash-20250801",
              "endpoint_id": "ds-v4-flash",
              "provider_id": "deepseek-anthropic",
              "output_valid": true}
}
```

### 4. Prompt 合约：创建版本 → 发布 → 沙箱渲染

```bash
curl -s http://127.0.0.1:8080/v1/prompts -H 'Content-Type: application/json' -d '{
  "name":"greeter","version":"1.0.0",
  "system_template":"你是{{ role }}，用{{ lang }}回答。",
  "variables_schema":{"type":"object",
    "properties":{"role":{"type":"string"},"lang":{"type":"string"}},
    "required":["role","lang"]},
  "default_logical_model":"standard-chat"}'

curl -s -X POST http://127.0.0.1:8080/v1/prompts/greeter/versions/1.0.0/publish

curl -s http://127.0.0.1:8080/v1/prompts/greeter/render \
  -H 'Content-Type: application/json' \
  -d '{"version":"1.0.0","variables":{"role":"向导","lang":"中文"}}'
# 缺变量 -> PROMPT_VARIABLE_MISSING；模板内危险属性访问 -> 安全错误
```

### 5. 用已发布 Prompt 发起调用（幂等）

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"standard-chat",
       "idempotency_key":"demo-001",
       "messages":[{"role":"user","content":"打个招呼"}],
       "prompt":{"name":"greeter","version":"1.0.0",
                 "variables":{"role":"向导","lang":"中文"}}}'
# 注意：使用 prompt 时 messages 中不得再含 system 消息
```

### 6. Eval 回放与管理/可观测

```bash
curl -s -X POST 'http://127.0.0.1:8080/v1/eval/greeter?version=1.0.0'

curl -s http://127.0.0.1:8080/v1/admin/health                 # 熔断状态
curl -s 'http://127.0.0.1:8080/v1/admin/traces?limit=10'
curl -s http://127.0.0.1:8080/v1/admin/traces/<trace_id>     # calls + 决策 + 分段延迟
```

## 错误治理

所有受控错误统一为 `{"error":{"code","message",...}}`，常见码：
`AUTH_MISSING_KEY/AUTH_INVALID_KEY`(401)、`RATE_LIMITED`(429)、
`VALIDATION_BAD_REQUEST`(400)、`IDEMPOTENCY_CONFLICT/IDEMPOTENCY_IN_PROGRESS`(409)、
`UPSTREAM_*`、`OUTPUT_INVALID_JSON/OUTPUT_SCHEMA_VIOLATION`、
`PROMPT_VARIABLE_MISSING`。仅配置内的可重试状态码与可重试错误会触发重试/兜底。
