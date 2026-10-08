-- 非流式结构化输出 repair 闭环
--  traces.structured_repairs：本次编排内为修正输出而额外发起的调用次数
--  trace_calls.error_code：单次调用在输出校验阶段被拒绝时的稳定错误码
ALTER TABLE traces ADD COLUMN structured_repairs INTEGER NOT NULL DEFAULT 0;
ALTER TABLE trace_calls ADD COLUMN error_code TEXT;
