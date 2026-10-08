"""SQLite 连接管理：WAL + busy_timeout + 迁移执行。"""
from __future__ import annotations

from pathlib import Path

import aiosqlite


class Database:
    def __init__(self, path: str, migrations_dir: str = "migrations") -> None:
        self.path = path
        self.migrations_dir = Path(migrations_dir)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.commit()
        await self.run_migrations()

    async def run_migrations(self) -> None:
        assert self._db is not None
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
        for sql_file in sorted(self.migrations_dir.glob("*.sql")):
            name = sql_file.name
            async with self._db.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?", (name,)
            ) as cur:
                if await cur.fetchone():
                    continue
            # 迁移脚本内含多语句（含 PRAGMA），用 executescript
            await self._db.executescript(sql_file.read_text(encoding="utf-8"))
            await self._db.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))
            await self._db.commit()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "database not connected"
        return self._db

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
