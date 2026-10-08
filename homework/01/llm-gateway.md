# 《项目演进路线 阶段一：LLM 统一模型调用服务》核心内容整理

## 核心命题

从 1.6 的单文件 Gateway 原型演进为**可配置、可部署、可测试的独立服务**。核心变化是职责归属清晰化：配置、策略、审计、调用方边界都要有明确归属。

**本讲主链：**
```
调用方提交OpenAI-compatible请求 → API Layer鉴权/限流/校验 → 规范化为ModelRequest
→ 解析冻结Prompt/Schema/Budget/Trace上下文 → Router生成候选端点 → Adapter调用Provider
→ 结果校验/事件流转换 → 记录Token/Cost/TTFT/Latency/Retry/Fallback → ModelResponse回到Loop
```

---

## 一、术语层级：Gateway ≠ Router ≠ Adapter ≠ Harness

```
API Layer：决定请求能否进入，负责外部协议
Gateway Service：编排一次模型调用
Registry：描述有哪些模型端点及能力
Router：决定本次尝试哪些端点及顺序
Adapter：把内部请求翻译成某Provider协议
Validator/Stream Mapper：把结果收敛为内部协议
Ledger/Trace：保存执行证据
Agent Loop/Harness：使用模型结果推进任务，决定真实终态
```

**贯穿原则：Gateway 统一模型执行语义；Agent Loop 决定任务怎样推进；Harness 管理状态/预算/权限/取消/完成条件。Gateway 不能因为模型说"任务已完成"就修改 Agent 的真实终态。**

---

## 二、从 1.6 到 1.7 的演进对比

| 1.6 原型 | 1.7 项目演进 |
|---|---|
| 单文件编排 | api/core/services 分层 |
| 自定义 `/v1/llm` 协议 | OpenAI Compatible 标准接口 |
| 两个写死的主备模型 | YAML配置Provider/别名/优先级/加权路由 |
| 固定次数重试fallback | 可配置重试/退避抖动/熔断/候选切换 |
| 内存Prompt模板和记录 | SQLite持久化版本/用量/成本/流检查点 |
| 无调用方边界 | API Key鉴权/令牌桶限流/健康检查 |

**为什么每个 Agent 不能直接调模型**：会导致策略不一致（同一429不同重试策略）、成本口径不一致、模型下线要改多处、Prompt无法组成可回放版本、审计无法追溯、Eval与生产路径不一致。**统一Gateway的价值：把变化集中在Adapter，策略集中在Router，证据集中在Trace。**

**LLM调用的四层成功**（HTTP 200 只代表 Transport 层）：

| 层级 | 成功条件 | 失败示例 |
|---|---|---|
| Transport | HTTP请求完整返回 | DNS/超时/连接中断 |
| Provider | 供应商接受并执行 | 429/5xx/认证失败 |
| Protocol | 响应能映射到内部协议 | 缺少choice/未知事件 |
| Semantic | 输出满足Schema与业务规则 | JSON合法但工具参数越权 |

---

## 三、内部协议设计（不能让外部兼容变内部耦合）

**OpenAI Compatible 是外部接口形态，不是内部领域模型**——不同供应商"字段长得一样"不代表行为相同，差异必须收敛到 Adapter，外部协议应在 API Layer 终止。

**ModelRequest 核心设计**：`model` 是逻辑名（如 `agent-structured`），不直接绑定供应商模型，实际端点由 Router 决定。

**ModelResponse 必须同时保留 `requested_model` 与 `resolved_model`**——否则日志只知道"用户请求了默认模型"，不知道最终用了哪个版本。

**ModelAdapter Protocol**：Loop 只依赖协议（`complete`/`stream`/`healthcheck`），不读 `choices[0]`，不识别供应商 chunk 类型，切换供应商只改 Adapter。

**错误必须是协议**（`ErrorCode` StrEnum + `GatewayError`），让 Router/Loop/客户端根据稳定语义决策，而非字符串匹配或猜测。

---

## 四、统一入口：兼容不等于全盘接收

**"兼容"≠复制供应商全部字段**。盲目接收会导致：上层绕过平台策略、参数语义不一致、无法提供长期协议保证。正确做法：**对外保持熟悉形态，只支持治理子集，未知字段立即拒绝而非静默忽略。**

**请求规范化**：身份信息必须来自认证中间件，**不接受客户端在 body 中自行声明 tenant_id**。

**不要做透明代理**：会丢失参数治理、能力校验、统一错误语义、Prompt/Schema版本、可解释路由、精确成本与Trace。

---

## 五、发现与路由

**Adapter 只适配协议，不制定策略**：不负责路由选择、重试策略、Prompt选择或业务状态修改。SDK 层 `max_retries=0`，因为重试统一由 Gateway 管理（否则 SDK 重试2次+Gateway重试2次，Trace 看不到真实次数）。

**能力注册表**：Router 不能只维护 `dict[model, client]`，需知道端点能力（streaming/json_schema/tool_calling/context长度等）作为路由基础。

**路由四步**：候选端点 → 按请求能力过滤 → 按租户预算/区域/数据策略/健康状态过滤 → 明确评分排序并记录原因。**路由结果必须进 Trace**，否则无法回答"为什么选中了这个端点"。

**Fallback 的明确边界**：

适用：主端点连接失败、供应商暂时过载、429且等待超预算、端点熔断、发出前发现能力不匹配
**不适用**：用户请求本身非法、认证/权限失败、内容安全拒答后尝试绕过更宽松模型、**已发送大量文本后偷偷切模型续写**、结构化结果业务校验失败却无明确修复策略

**路由变化需要 Eval 验证**：相同逻辑模型切换实际模型后行为可能变化，上线新候选前需在固定 Eval 集比较成功率/合法率/工具选择正确率/延迟/成本/拒答行为。

---

## 六、执行与收敛：三种模式共享同一条主链

普通输出、Streaming、Structured Output 共享请求规范化、Prompt解析、路由、超时取消、错误归一化、Usage/Trace、Policy/预算——**差异只出现在结果消费与验证阶段**，为三种模式各写一套逻辑会导致重试/日志/安全行为不一致。

**Trace 写入不能覆盖原始异常**：`finally` 中记录 Trace，可观测性失败应进独立告警，不能让一次成功调用变成500。

**Streaming 不能把供应商 chunk 原样转发**，先转成稳定内部事件（`GatewayStreamEvent`）再编码为 OpenAI Compatible SSE。

**流式响应只能有一个终态**（completed/failed/cancelled），需通过原子状态迁移（compare-and-set）保证唯一性。**客户端断开只是取消信号，不等于模型已停止**，Harness 需把取消传播到后台Task/HTTP请求/可取消工具。

**Structured Output 两层保证**：模型供应商的生成约束（第一层）+ Gateway 本地校验（第二层）。**即使端点声称支持 JSON Schema，也不能省略本地 Pydantic 校验。**`retryable=True` 不代表任何时候都自动重试，还要看预算、是否已发内容、修复是否会引入新信息。

**Structured Streaming 边界**：JSON 在流式中途通常不完整，**绝不能在半个 JSON 到达时执行工具**——要么前端展示状态等完整对象再解析提交，要么用供应商原生类型化事件在字段完成时转换。

**输出修复要有边界**：修复调用计入同一 Run 和预算、用新 call_id 关联原调用、设最大修复次数、失败后返回明确错误而非退回未校验的自然语言。

---

## 七、Prompt 装配：冻结行为契约

**Prompt 不是一段字符串，而是可发布、可复现、可评估的行为契约**——必须绑定模板、输入/输出Schema、Few-shot、工具版本、默认模型、Context预算、Eval集，并记录内容hash。只给文件名加个v2不足以复现行为。

**严格模板渲染**：用 `StrictUndefined`，缺失变量在调用模型前失败，而非静默渲染成空字符串。

**Prompt Registry**：调用记录必须保存 name、version、content_hash——即使文件名没变，hash 也能暴露不可追踪的修改。

**禁止同时传完整 messages 和 Prompt 模板变量**，避免两套 System Prompt 冲突。

**灰度分流必须稳定**：基于租户/用户/Run 做哈希分桶，**同一个 Run 内不能一会儿v1一会儿v2**，选定版本后固定到 RunState。

---

## 八、证据回写：Trace 与 Cost Ledger

一次调用至少记录：**关联**（trace_id/run_id/step_id/call_id）、**路由**（逻辑模型/供应商/实际模型/候选/拒绝原因）、**Prompt**（名称/版本/hash）、**用量**（各类token）、**延迟**（queue/route/TTFT/generation拆分）、**弹性**（attempt/retry/fallback）、**结果**（finish reason/终态/校验结果）、**错误**（错误码/HTTP状态/供应商请求ID）、**成本**。

**不要默认把完整 Prompt 和输出写进普通日志**——可能含源代码/PII/密钥，采样/脱敏/保存期限需独立配置。

**延迟要拆解，不能只记一个数字**：queue/prompt_render/route/connect/TTFT/generation 各自记录，否则无法指导优化。

**TTFT 必须以第一个有业务意义的 delta 为准**，不能把连接建立事件当首Token。

**成本估算要用版本化价格表**，不能用今天的价格回算几个月前的调用。

**不要把高基数字段（user_id/run_id/prompt_text）做成 Metrics Label**，否则监控系统基数爆炸——高基数细节归 Trace/日志，Metrics 只放低基数聚合维度。

---

## 九、失败治理

**错误按调用生命周期分类**：认证/请求校验/Prompt渲染 → 不重试；排队/连接/首Token前 → 有限重试或Fallback；**已流式输出后 → 不盲目重新生成**；输出校验失败 → 有限修复；内容拒答 → 不换模型绕过。

**有边界的指数退避**：必须由**次数+总截止时间(deadline)+错误类型**共同约束——只设 `max_retries=3` 不够，因为三次超时可能远超上层 Run 的剩余预算。退避超过 deadline 时应立即终止而非继续等待。

**并发限制比 QPS 更重要**：模型请求持续时间长，只限QPS仍可能堆积大量并发连接，生产系统需要全局并发/每供应商并发/每租户QPS与并发/每模型Token每分钟等多层限制。

**Backpressure**：事件队列必须设上限，但**不能丢弃终态/错误/usage/工具调用事件**，慢消费者应触发明确的中断策略而非无限堆积内存。

**幂等性不能避免模型重复计费**：Idempotency-Key 只能防止 Gateway 因客户端重复提交创建多个调用记录，除非供应商保证，否则模型端点仍可能执行两次。**同一幂等键对应不同请求指纹必须拒绝，不能返回旧结果。**

---

## 十、接回 Agent Harness

Loop 只依赖稳定的 **ModelPort** 协议，不知道供应商/SDK/重试次数/价格表，只处理成功的 `ActionEnvelope` 或稳定的 `GatewayError`。

**Gateway 不负责**：Agent任务的业务终态、工具副作用、权限扩大、长期记忆、伪造成功。

**完整追踪链**：`trace_id → run_id → step_id → call_id/tool_call_id`。**Gateway 负责 call_id 内部细节；Harness 负责把它放进完整 Run 的因果链。**

**预算必须从 Run 传播到 Call**：否则每次单调用都"没超预算"，完整 Agent Run 却可能无限消耗（`CallBudget` 从 `RunBudget` 派生剩余额度）。

---

## 十一、测试策略：证明不只是"能返回 200"

六类测试：
1. **Adapter Contract Test**：归一化正确性、错误码映射、429/401/5xx/超时/取消
2. **Router 测试**：不止断言选中谁，还要断言**为什么拒绝其他候选**
3. **重试测试不能真的等待**：注入时钟/随机数/sleep，测试重试边界而非真实等待几秒
4. **Streaming 测试**：首delta、UTF-8边界、客户端断开、唯一终态、**首块后不重试**、Backpressure、usage只计一次
5. **Structured Output 测试**：至少九类固定样本（合法/Markdown包裹/语法错误/类型错误/缺字段/多字段/业务规则不合法/修复成功/达到修复上限）
6. **可观测性测试**：成功/重试成功/Fallback成功/最终失败都要产生完整Trace，**并断言敏感字段（API Key/完整Prompt/堆栈）未进普通日志**

---