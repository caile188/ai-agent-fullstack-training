"""数据访问：traces / calls / costs / 路由决策 / Prompt 版本 / Eval。

所有方法只接收/返回基础类型或 core/observability 模型，不泄漏 SQL 到上层。
写失败由调用方决定是否降级，不允许让记账异常打断主响应（LEDGER_WRITE_FAILED）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.prompts.versions import PromptVersion
from app.core.router.decision import RouteDecision
from app.services.db.connection import Database
from app.services.observability.records import CallRecord, CostRecord, TraceRecord


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def conn(self):
        return self.db.conn

    # ============================================================ Prompt
    async def upsert_package(self, name: str, owner: str = "default") -> int:
        await self.conn.execute(
            "INSERT INTO prompt_packages(name, owner) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner",
            (name, owner),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM prompt_packages WHERE name=?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return row["id"]

    async def insert_prompt_version(self, pv: PromptVersion, package_id: int) -> None:
        d = pv.model_dump(mode="json")
        await self.conn.execute(
            """INSERT INTO prompt_versions
            (package_id, version, status, content_hash, system_template, variables_schema,
             few_shot_json, output_schema, tool_versions, default_logical_model,
             generation_params, context_budget, eval_set_json, eval_threshold, changelog)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                package_id,
                pv.version,
                d["status"],
                pv.content_hash,
                pv.system_template,
                json.dumps(d["variables_schema"], ensure_ascii=False),
                json.dumps(d["few_shot"], ensure_ascii=False),
                json.dumps(d["output_schema"], ensure_ascii=False) if d["output_schema"] else None,
                json.dumps(d["tool_versions"], ensure_ascii=False),
                pv.default_logical_model,
                json.dumps(d["generation_params"], ensure_ascii=False),
                pv.context_budget,
                json.dumps(d["eval_set"], ensure_ascii=False),
                pv.eval_threshold,
                pv.changelog,
            ),
        )
        await self.conn.commit()

    async def get_prompt_version(self, name: str, version: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            """SELECT v.* FROM prompt_versions v
               JOIN prompt_packages p ON p.id = v.package_id
               WHERE p.name=? AND v.version=?""",
            (name, version),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_published(self, name: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            """SELECT v.* FROM prompt_versions v
               JOIN prompt_packages p ON p.id = v.package_id
               WHERE p.name=? AND v.status='published'
               ORDER BY v.id DESC LIMIT 1""",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_versions(self, name: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """SELECT v.version, v.status, v.content_hash, v.created_at
               FROM prompt_versions v JOIN prompt_packages p ON p.id = v.package_id
               WHERE p.name=? ORDER BY v.id DESC""",
            (name,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ============================================================ 路由决策
    async def insert_route_decision(
        self, trace_id: str, decision: RouteDecision
    ) -> int:
        scores = [
            c.score.model_dump(mode="json") for c in decision.survivors if c.score is not None
        ]
        cur = await self.conn.execute(
            """INSERT INTO route_decisions
            (trace_id, logical_model, request_caps, chosen_endpoint, score_snapshot, decision_json)
            VALUES (?,?,?,?,?,?)""",
            (
                trace_id,
                decision.logical_model,
                json.dumps(decision.required_capabilities),
                decision.chosen_endpoint_id,
                json.dumps(scores, ensure_ascii=False),
                decision.snapshot_json(),
            ),
        )
        decision_id = cur.lastrowid
        for rej in decision.rejections:
            await self.conn.execute(
                "INSERT INTO route_rejections(decision_id, endpoint_id, stage, reason_code, detail)"
                " VALUES (?,?,?,?,?)",
                (decision_id, rej.endpoint_id, rej.stage, rej.reason_code, rej.detail),
            )
        await self.conn.commit()
        return decision_id

    # ============================================================ Trace
    async def insert_trace(self, t: TraceRecord) -> None:
        last = t.attempts[-1] if t.attempts else None
        await self.conn.execute(
            """INSERT OR REPLACE INTO traces
            (trace_id, run_id, tenant_id, requested_model, provider_id, endpoint_id, resolved_model,
             prompt_name, prompt_version, prompt_hash, schema_version, stream,
             queue_ms, render_ms, route_ms, connect_ms, ttft_ms, generation_ms, total_ms,
             attempt, retries, structured_repairs, fallback_from, timeout_budget_ms,
             finish_reason, terminal_status, output_validation,
             error_code, error_stage, http_status, upstream_request_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.trace_id, t.run_id, t.tenant_id, t.requested_model,
                t.provider_id, t.endpoint_id, t.resolved_model,
                t.prompt_name, t.prompt_version, t.prompt_hash, t.schema_version,
                int(t.stream),
                t.queue_ms, t.render_ms, t.route_ms, t.connect_ms,
                t.ttft_ms, t.generation_ms, t.total_ms,
                len(t.attempts), t.retries, t.structured_repairs,
                (last.fallback_from if last else None) or t.fallback_from,
                t.timeout_budget_ms,
                t.finish_reason, t.terminal_status, t.output_validation,
                t.error_code, t.error_stage, t.http_status, t.upstream_request_id,
            ),
        )
        await self.conn.commit()

    async def insert_call(self, c: CallRecord) -> None:
        await self.conn.execute(
            """INSERT INTO trace_calls
            (call_id, trace_id, run_id, step_id, attempt, endpoint_id, request_hash,
             input_tokens, output_tokens, cached_tokens, reasoning_tokens,
             connect_ms, ttft_ms, http_status, upstream_request_id, error_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                c.call_id, c.trace_id, c.run_id, c.step_id, c.attempt, c.endpoint_id,
                c.request_hash,
                c.usage.input_tokens, c.usage.output_tokens,
                c.usage.cached_tokens, c.usage.reasoning_tokens,
                c.connect_ms, c.ttft_ms, c.http_status, c.upstream_request_id, c.error_code,
            ),
        )
        await self.conn.commit()

    async def insert_cost(self, cr: CostRecord) -> None:
        await self.conn.execute(
            """INSERT INTO cost_ledger
            (trace_id, call_id, tenant_id, input_cost, output_cost, cached_cost,
             total_cost, currency, price_version)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                cr.trace_id, cr.call_id, cr.tenant_id,
                cr.cost.input_cost, cr.cost.output_cost, cr.cost.cached_cost,
                cr.cost.total, cr.currency, cr.price_version,
            ),
        )
        await self.conn.commit()

    async def spent_usd(self, tenant_id: str) -> float:
        async with self.conn.execute(
            "SELECT COALESCE(SUM(total_cost),0) AS s FROM cost_ledger WHERE tenant_id=?",
            (tenant_id,),
        ) as cur:
            row = await cur.fetchone()
        return float(row["s"])

    # ============================================================ 价格版本
    async def ensure_price_version(self, version: str, snapshot_json: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO price_versions(version, snapshot_json) VALUES (?,?)",
            (version, snapshot_json),
        )
        await self.conn.commit()

    # ============================================================ 幂等
    async def get_idempotency(self, tenant_id: str, key: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM idempotency_keys WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, key),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def insert_idempotency_pending(
        self, tenant_id: str, key: str, request_hash: str, trace_id: str
    ) -> bool:
        """占位。返回 True 表示本次成功占位（可执行）；False 表示 key 已存在。"""
        try:
            await self.conn.execute(
                "INSERT INTO idempotency_keys"
                "(tenant_id, idempotency_key, request_hash, trace_id, status) VALUES (?,?,?,?,'pending')",
                (tenant_id, key, request_hash, trace_id),
            )
            await self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    async def complete_idempotency(
        self, tenant_id: str, key: str, response_json: str
    ) -> None:
        await self.conn.execute(
            "UPDATE idempotency_keys SET status='completed', response_json=?,"
            " completed_at=datetime('now') WHERE tenant_id=? AND idempotency_key=?",
            (response_json, tenant_id, key),
        )
        await self.conn.commit()

    async def fail_idempotency(self, tenant_id: str, key: str) -> None:
        """执行失败时释放占位，允许客户端用同键重试。"""
        await self.conn.execute(
            "DELETE FROM idempotency_keys WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, key),
        )
        await self.conn.commit()

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM traces WHERE trace_id=?", (trace_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_trace_calls(self, trace_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM trace_calls WHERE trace_id=? ORDER BY attempt", (trace_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_decision(self, trace_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM route_decisions WHERE trace_id=?", (trace_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        result = dict(row)
        async with self.conn.execute(
            "SELECT endpoint_id, stage, reason_code, detail FROM route_rejections WHERE decision_id=?",
            (row["id"],),
        ) as cur:
            result["rejections"] = [dict(r) for r in await cur.fetchall()]
        return result

    # ============================================================ Eval
    async def insert_eval_run(
        self, name: str, version: str, total: int, passed: int,
        threshold: float, result: list[dict[str, Any]],
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO eval_runs
            (prompt_name, prompt_version, cases_total, cases_passed, threshold, passed, result_json)
            VALUES (?,?,?,?,?,?,?)""",
            (name, version, total, passed, threshold, int(passed / total >= threshold) if total else 0,
             json.dumps(result, ensure_ascii=False)),
        )
        await self.conn.commit()
        return cur.lastrowid

    # ============================================================ Admin
    async def recent_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM traces ORDER BY rowid DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
