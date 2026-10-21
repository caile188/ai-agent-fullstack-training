-- ID 语义对齐：trace_id 重命名为 request_id（一次网关模型调用），
-- 并删除与 trace 1:1 冗余的 run_id。未来 Agent Run 的 trace_id 是 request 的上层。
-- 说明：
--  - RENAME COLUMN 会由 SQLite 自动改写引用该列的索引与其它表外键定义；
--  - 先重命名父表 traces 的主键，再重命名子表/外键表中的同名列。
ALTER TABLE traces RENAME COLUMN trace_id TO request_id;
ALTER TABLE trace_calls RENAME COLUMN trace_id TO request_id;
ALTER TABLE route_decisions RENAME COLUMN trace_id TO request_id;
ALTER TABLE cost_ledger RENAME COLUMN trace_id TO request_id;
ALTER TABLE idempotency_keys RENAME COLUMN trace_id TO request_id;

ALTER TABLE traces DROP COLUMN run_id;
ALTER TABLE trace_calls DROP COLUMN run_id;
