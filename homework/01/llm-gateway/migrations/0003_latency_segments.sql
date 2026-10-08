-- 延迟分段补全：Prompt 渲染段、建连(响应头到达)段
--  traces.render_ms：Prompt 解析/变量渲染耗时（渲染期失败也有值）
--  traces.connect_ms：本次编排最后一次成功上游建连耗时
--  trace_calls.connect_ms：每次真实调用到响应头到达的耗时
ALTER TABLE traces ADD COLUMN render_ms REAL;
ALTER TABLE traces ADD COLUMN connect_ms REAL;
ALTER TABLE trace_calls ADD COLUMN connect_ms REAL;
