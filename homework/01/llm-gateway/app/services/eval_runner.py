"""Eval 执行：对一个已发布 Prompt 版本跑其 eval 集，给出门槛结论并落库。"""
from __future__ import annotations

from app.api.schemas import ChatCompletionRequest, IncomingMessage, PromptRef
from app.core.prompts.eval import evaluate_output
from app.services.db.repositories import Repository
from app.services.gateway import GatewayService
from app.services.prompts_store import PromptStore


class EvalRunner:
    def __init__(self, gateway: GatewayService, prompts: PromptStore, repo: Repository) -> None:
        self.gateway = gateway
        self.prompts = prompts
        self.repo = repo

    async def run(self, name: str, version: str | None = None, tenant_id: str = "default") -> dict:
        pv = await self.prompts.resolve(name, version)
        if not pv.eval_set:
            return {"prompt": name, "version": pv.version, "cases_total": 0, "passed": False,
                    "reason": "empty eval set"}

        results = []
        passed_count = 0
        for case in pv.eval_set:
            payload = ChatCompletionRequest(
                model=pv.default_logical_model or "standard-chat",
                messages=[IncomingMessage(role="user", content="")],
                prompt=PromptRef(name=name, version=pv.version, variables=case.variables),
            )
            text, error = "", None
            try:
                resp = await self.gateway.chat(payload, tenant_id=tenant_id)
                text = resp.text
            except Exception as e:  # 单条失败记为该用例失败，不中断整套评估
                error = str(e)
            case_result = evaluate_output(text, case) if error is None else None
            ok = bool(case_result and case_result.passed)
            if ok:
                passed_count += 1
            results.append({
                "variables": case.variables,
                "passed": ok,
                "reasons": case_result.reasons if case_result and not case_result.passed else (
                    [error] if error else []
                ),
            })

        total = len(pv.eval_set)
        threshold = pv.eval_threshold if pv.eval_threshold is not None else 1.0
        run_id = await self.repo.insert_eval_run(
            name, pv.version, total, passed_count, threshold, results
        )
        return {
            "eval_run_id": run_id,
            "prompt": name,
            "version": pv.version,
            "cases_total": total,
            "cases_passed": passed_count,
            "pass_rate": round(passed_count / total, 4),
            "threshold": threshold,
            "passed": passed_count / total >= threshold,
            "cases": results,
        }
