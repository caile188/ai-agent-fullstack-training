"""Prompt 版本存取与发布。发布后不可变；任何修改都必须产生新版本。"""
from __future__ import annotations

import json
from typing import Any

from app.core.errors import GatewayError
from app.core.prompts.hashing import compute_hash, with_hash
from app.core.prompts.versions import PromptStatus, PromptVersion
from app.services.db.repositories import Repository


class PromptStore:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def create_version(self, pv: PromptVersion) -> PromptVersion:
        pv = with_hash(pv)
        package_id = await self.repo.upsert_package(pv.name)
        existing = await self.repo.get_prompt_version(pv.name, pv.version)
        if existing is not None:
            raise GatewayError(
                "VALIDATION_BAD_REQUEST",
                f"prompt version already exists: {pv.name}@{pv.version}",
            )
        await self.repo.insert_prompt_version(pv, package_id)
        return pv

    async def publish(self, name: str, version: str) -> None:
        row = await self.repo.get_prompt_version(name, version)
        if row is None:
            raise GatewayError("PROMPT_NOT_FOUND", f"{name}@{version}")
        await self.repo.conn.execute(
            "UPDATE prompt_versions SET status='published' WHERE id=? "
            "AND (SELECT name FROM prompt_packages WHERE id=prompt_versions.package_id)=?",
            (row["id"], name),
        )
        await self.repo.conn.commit()

    async def resolve(self, name: str, version: str | None = None) -> PromptVersion:
        row = (
            await self.repo.get_prompt_version(name, version)
            if version
            else await self.repo.get_published(name)
        )
        if row is None:
            code = "PROMPT_NOT_FOUND" if version else "PROMPT_VERSION_NOT_PUBLISHED"
            raise GatewayError(code, f"{name}@{version or 'published'}")
        return self._from_row(row, name)

    @staticmethod
    def _from_row(row: dict[str, Any], name: str) -> PromptVersion:
        output_schema = row["output_schema"]
        return PromptVersion(
            name=name,
            version=row["version"],
            status=PromptStatus(row["status"]),
            system_template=row["system_template"],
            variables_schema=json.loads(row["variables_schema"] or "{}"),
            few_shot=json.loads(row["few_shot_json"] or "[]"),
            output_schema=json.loads(output_schema) if output_schema else None,
            tool_versions=json.loads(row["tool_versions"] or "[]"),
            default_logical_model=row["default_logical_model"],
            generation_params=json.loads(row["generation_params"] or "{}"),
            context_budget=row["context_budget"],
            eval_set=json.loads(row["eval_set_json"] or "[]"),
            eval_threshold=row["eval_threshold"],
            changelog=row["changelog"] or "",
            content_hash=row["content_hash"],
        )
