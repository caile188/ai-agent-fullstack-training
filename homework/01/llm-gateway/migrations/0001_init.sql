-- LLM Gateway 初始 schema
-- 设计要点：
--  1) Prompt 版本不可变、内容寻址(hash)；
--  2) 路由决策与候选拒绝原因分离落库，支持离线 Eval 回放与解释；
--  3) traces / trace_calls / cost_ledger 三级记账：一次编排 -> 多次尝试 -> 多次 HTTP；
--  4) 不存请求/响应正文，只存长度与 hash，敏感数据不入库。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- Prompt 合约
CREATE TABLE prompt_packages (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    owner       TEXT NOT NULL DEFAULT 'default',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE prompt_versions (
    id                    INTEGER PRIMARY KEY,
    package_id            INTEGER NOT NULL REFERENCES prompt_packages(id),
    version               TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('draft','published','deprecated')),
    content_hash          TEXT NOT NULL,                 -- 模板+schema+few-shot 等全量 hash
    system_template       TEXT NOT NULL,
    variables_schema      TEXT NOT NULL DEFAULT '{}',    -- JSON Schema 约束输入变量
    few_shot_json         TEXT NOT NULL DEFAULT '[]',
    output_schema         TEXT,                          -- 输出 JSON Schema
    tool_versions         TEXT NOT NULL DEFAULT '[]',    -- 工具定义版本引用
    default_logical_model TEXT,
    generation_params     TEXT NOT NULL DEFAULT '{}',
    context_budget        INTEGER,                       -- token 预算
    eval_set_json         TEXT NOT NULL DEFAULT '[]',
    eval_threshold        REAL,                          -- 通过门槛 0~1
    changelog             TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (package_id, version),
    UNIQUE (package_id, content_hash)
);

-- ---------------------------------------------------------------- 路由决策
CREATE TABLE route_decisions (
    id             INTEGER PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    logical_model  TEXT NOT NULL,                        -- 用户请求的逻辑模型 requested_model
    request_caps   TEXT NOT NULL DEFAULT '[]',
    chosen_endpoint TEXT,
    score_snapshot TEXT NOT NULL DEFAULT '[]',          -- 各候选得分明细 JSON
    decision_json  TEXT NOT NULL,                        -- 完整决策快照(可回放)
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_route_decisions_trace ON route_decisions(trace_id);

CREATE TABLE route_rejections (
    id           INTEGER PRIMARY KEY,
    decision_id  INTEGER NOT NULL REFERENCES route_decisions(id),
    endpoint_id  TEXT NOT NULL,
    stage        TEXT NOT NULL,             -- capability | policy
    reason_code  TEXT NOT NULL,             -- 如 capability_missing:structured_output
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_route_rejections_decision ON route_rejections(decision_id);

-- ---------------------------------------------------------------- Trace 记账
CREATE TABLE traces (
    trace_id          TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    requested_model   TEXT NOT NULL,        -- 用户请求的逻辑模型
    provider_id       TEXT,
    endpoint_id       TEXT,
    resolved_model    TEXT,                 -- 供应商实际返回的模型版本
    prompt_name       TEXT,
    prompt_version    TEXT,
    prompt_hash       TEXT,
    schema_version    TEXT NOT NULL DEFAULT '1.0',
    stream            INTEGER NOT NULL DEFAULT 0,
    queue_ms          REAL,
    route_ms          REAL,
    ttft_ms           REAL,
    generation_ms     REAL,
    total_ms          REAL,
    attempt           INTEGER NOT NULL DEFAULT 1,
    retries           INTEGER NOT NULL DEFAULT 0,
    fallback_from     TEXT,                 -- 从哪个 endpoint 切换而来
    timeout_budget_ms INTEGER,
    finish_reason     TEXT,
    terminal_status   TEXT,                 -- success | failed | cancelled
    output_validation TEXT,                 -- pass | fail:<reason>
    error_code        TEXT,
    error_stage       TEXT,
    http_status       INTEGER,
    upstream_request_id TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_traces_tenant_created ON traces(tenant_id, created_at);
CREATE INDEX idx_traces_model ON traces(requested_model, resolved_model);

-- 每次真实 HTTP 调用（重试产生新 call_id）
CREATE TABLE trace_calls (
    call_id            TEXT PRIMARY KEY,
    trace_id           TEXT NOT NULL REFERENCES traces(trace_id),
    run_id             TEXT NOT NULL,
    step_id            TEXT NOT NULL,
    attempt            INTEGER NOT NULL,
    endpoint_id        TEXT NOT NULL,
    request_hash       TEXT,                -- 请求体 hash，不存正文
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cached_tokens      INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    ttft_ms            REAL,
    http_status        INTEGER,
    upstream_request_id TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_trace_calls_trace ON trace_calls(trace_id);

CREATE TABLE cost_ledger (
    id          INTEGER PRIMARY KEY,
    trace_id    TEXT NOT NULL REFERENCES traces(trace_id),
    call_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    input_cost  REAL NOT NULL DEFAULT 0,
    output_cost REAL NOT NULL DEFAULT 0,
    cached_cost REAL NOT NULL DEFAULT 0,
    total_cost  REAL NOT NULL DEFAULT 0,
    currency    TEXT NOT NULL DEFAULT 'USD',
    price_version TEXT NOT NULL DEFAULT 'config',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_cost_ledger_tenant_created ON cost_ledger(tenant_id, created_at);

-- ---------------------------------------------------------------- Eval
CREATE TABLE eval_runs (
    id             INTEGER PRIMARY KEY,
    prompt_name    TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    cases_total    INTEGER NOT NULL,
    cases_passed   INTEGER NOT NULL,
    threshold      REAL NOT NULL,
    passed         INTEGER NOT NULL,
    result_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
