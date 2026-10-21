-- Trace 字段口径修正与补全：
--  schema_version 改名 record_schema_version（记录格式版本，与 prompt/schema 无关）；
--  traces 增退避汇总与四类 token 用量汇总（取成功 attempt usage）；
--  trace_calls 增 attempt 类型（attempt/repair）、供应商与每次调用的等待/耗时/退避。
ALTER TABLE traces RENAME COLUMN schema_version TO record_schema_version;
ALTER TABLE traces ADD COLUMN backoff_ms REAL;
ALTER TABLE traces ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE traces ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE traces ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE traces ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0;

ALTER TABLE trace_calls ADD COLUMN kind TEXT NOT NULL DEFAULT 'attempt';
ALTER TABLE trace_calls ADD COLUMN provider_id TEXT;
ALTER TABLE trace_calls ADD COLUMN wait_ms REAL;
ALTER TABLE trace_calls ADD COLUMN latency_ms REAL;
ALTER TABLE trace_calls ADD COLUMN backoff_ms REAL;
