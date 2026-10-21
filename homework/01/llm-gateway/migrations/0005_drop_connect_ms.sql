-- 改用官方 SDK 后无法获取"响应头到达"时刻，建连耗时指标不再可得；
-- 首包体验由 ttft_ms 覆盖，删除 connect_ms 避免假数据。
ALTER TABLE traces DROP COLUMN connect_ms;
ALTER TABLE trace_calls DROP COLUMN connect_ms;
