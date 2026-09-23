import asyncio
import os

import aiosqlite

# Схема базы данных. Новые таблицы для новых функций добавляйте прямо сюда —
# executescript идемпотентен благодаря IF NOT EXISTS.
SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_roles (
    role_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    criteria TEXT DEFAULT '',
    stackable INTEGER DEFAULT 0,
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS role_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    granted_by INTEGER,
    granted_at TEXT DEFAULT (datetime('now')),
    reason TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shame_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    reason TEXT DEFAULT '',
    given_by INTEGER,
    given_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT,
    active INTEGER DEFAULT 1,
    removed_reason TEXT DEFAULT '',
    is_super INTEGER DEFAULT 0,
    bot_muted INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shame_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL UNIQUE,
    target_user_id INTEGER NOT NULL,
    vote_type TEXT NOT NULL,
    reason TEXT DEFAULT '',
    duration_hours REAL DEFAULT 24,
    initiator_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    ends_at TEXT,
    status TEXT DEFAULT 'active',
    threshold INTEGER DEFAULT 3
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    shame_role_id INTEGER,
    vote_threshold INTEGER DEFAULT 3,
    vote_duration_hours REAL DEFAULT 24,
    log_channel_id INTEGER
);
"""


class Database:
    """Простая асинхронная обёртка над SQLite для всего бота."""

    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        await self._run_migrations()

    async def _ensure_column(self, table: str, column: str, coltype: str):
        """Добавляет колонку в таблицу, если её ещё нет — безопасно для уже существующих БД."""
        cur = await self.conn.execute(f"PRAGMA table_info({table})")
        existing = [row[1] for row in await cur.fetchall()]
        await cur.close()
        if column not in existing:
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            await self.conn.commit()

    async def _run_migrations(self):
        """Точечные миграции для полей, добавленных после первого релиза.

        Новые колонки добавляйте сюда через _ensure_column — это не сломает
        уже существующую базу данных на сервере пользователя.
        """
        # Окно времени, когда созданную через бота роль ещё может редактировать
        # не только админ, но и доверенная роль (например, "бро <3").
        await self._ensure_column("managed_roles", "editable_until", "TEXT")
        # Настройки прав на редактирование ролей — хранятся в guild_settings,
        # чтобы не плодить отдельную таблицу под одну функцию.
        await self._ensure_column("guild_settings", "admin_role_id", "INTEGER")
        await self._ensure_column("guild_settings", "trusted_role_id", "INTEGER")
        await self._ensure_column("guild_settings", "role_edit_window_hours", "REAL DEFAULT 24")
        # Когда позор сняли — нужно для истории, топа и кулдауна нового голосования.
        await self._ensure_column("shame_records", "removed_at", "TEXT")
        await self._ensure_column("guild_settings", "pozor_cooldown_hours", "REAL")
        # Супер позор не истекает сам и в статистике весит как 100 обычных.
        await self._ensure_column("shame_records", "is_super", "INTEGER DEFAULT 0")
        # 1 — микрофон выключил бот и после супер позора его нужно вернуть.
        await self._ensure_column("shame_records", "bot_muted", "INTEGER DEFAULT 0")

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def execute(self, query: str, params: tuple = ()):
        async with self.lock:
            cur = await self.conn.execute(query, params)
            await self.conn.commit()
            return cur

    async def fetchone(self, query: str, params: tuple = ()):
        async with self.lock:
            cur = await self.conn.execute(query, params)
            row = await cur.fetchone()
            await cur.close()
            return row

    async def fetchall(self, query: str, params: tuple = ()):
        async with self.lock:
            cur = await self.conn.execute(query, params)
            rows = await cur.fetchall()
            await cur.close()
            return rows
