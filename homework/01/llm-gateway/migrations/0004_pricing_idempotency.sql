-- 价格版本化：版本号为端点单价快照的内容寻址 hash
CREATE TABLE IF NOT EXISTS price_versions (
    version       TEXT PRIMARY KEY,           -- 如 cfg_1a2b3c4d5e6f
    snapshot_json TEXT NOT NULL,              -- {endpoint: {input/output/cached per 1m}}
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 幂等键：同一租户下同一 key 只允许执行一次；请求指纹必须一致
CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash   TEXT NOT NULL,             -- 规范化请求体指纹（不含 key 本身）
    trace_id       TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('pending','completed')),
    response_json  TEXT,                      -- 仅非流式：缓存完整响应用于重放
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at   TEXT,
    PRIMARY KEY (tenant_id, idempotency_key)
);
