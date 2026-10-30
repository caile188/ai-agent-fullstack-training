# Week02 作业：工具治理框架 —— 转账工具（transfer）

在一个完整的「工具治理 Runtime」框架上，新增一个高风险写工具 **transfer（账户转账）**，
让模型发起的每次调用都必须经过：**参数校验 → 权限状态机 → 业务预检 → 一次性审批 → 受控执行 → 结果脱敏 → 审计**。

## 文件说明

| 文件 | 作用 |
|---|---|
| `tool_governance_demo.py` | 治理框架 + 4 个工具（get_order / create_refund / run_shell / transfer）+ 演示入口 |
| `test_tool_governance.py` | 转账工具的 5 个链路验收用例（全部经 `runtime.invoke()` 调用） |

## 转账工具的 6 个实现任务

1. **ACCOUNTS 模拟数据**：模块级可变 dict，Key 为 `(tenant_id, account_id)`，Value 为余额（float）。
   预设 tenant_a 三个账户（100000 / 5000 / 20000）、tenant_b 一个账户（50000）。
2. **TransferArgs（Pydantic）**：`from_account` / `to_account` 正则 `^ACC-[A-Z]-[0-9]{6}$`，
   `amount` 为 float 且 `0 < amount <= 100000`；继承 `StrictArgs`（`extra="forbid"` + strict）。
3. **transfer_precheck**：教学限额区间 `50000 < amount <= 80000` → `EXCEED_LIMIT`；
   转出账户不存在或余额不足 → `INSUFFICIENT_BALANCE`。只判断、不改余额。
4. **transfer_handler**：`amount > 80000` 先 `sleep(3.0)`（制造写超时，且在扣款之前）；
   转入账户不存在 → `ACCOUNT_NOT_FOUND`；否则同租户内扣款 + 入账；
   返回 `txn_id`（tool_call_id 后 6 位）/ from / to / amount / `status="accepted"`。
5. **build_tools 注册**：WRITE / HIGH 风险 / 权限 `transfer:execute` / 需审批 /
   `timeout_seconds=1`（小于 3 秒）/ `max_retries=0` / 非幂等，绑定 precheck 与 handler。
6. **_redact 账号脱敏**：`ACC-A-123456` → `ACC-A-****3456`（保留前缀与末 4 位）。

## 治理决策链（PermissionEngine.decide，固定优先级）

deny 规则 → plan 只读 → 执行白名单 → RBAC 权限 → 业务 precheck → 高风险一次性审批
→ bypass（只跳普通确认）→ allow 规则 → Shell 危险命令兜底 → 默认放行。

结论为三态：**ALLOW / DENY / CONFIRM**。

## 三条红线

1. 不修改 `PermissionEngine.decide` 的任何一行（优先级是固定框架）。
2. 测试不绕过 `ToolRuntime.invoke`，不得直接调用 `transfer_handler`。
3. 不删除 `TransferArgs` 的 `extra="forbid"`（防模型注入额外参数的最后屏障）。

## 环境准备

```bash
cd homework/02

# 创建并启用虚拟环境
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 依赖（pydantic 运行框架必需；pytest 仅跑测试用；用例自带 async 适配，无需 pytest-asyncio）
pip install pydantic pytest
```

## 运行方式

本地离线演示（默认入口，无需 API Key）：

```bash
python tool_governance_demo.py
```

可选：接 DeepSeek 跑真实 Agent Loop（需先设置 `DEEPSEEK_API_KEY`）：

```bash
python tool_governance_demo.py --agent --input "请查询订单 ord_1001 的状态"
```

运行转账工具的 5 个验收用例：

```bash
python -m pytest test_tool_governance.py -v -k transfer
```

5 个用例分别覆盖：参数注入/非法值拦截、业务预检（限额/余额）、硬授权（RBAC/白名单/plan）、
审批绑定 + 成功转账 + 账号脱敏 + 审计 + 一次性凭证、写超时 `TIMEOUT_UNKNOWN` 且余额不变。
