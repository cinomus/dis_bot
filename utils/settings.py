import discord

import config

DEFAULT_VOTE_THRESHOLD = config.DEFAULT_VOTE_THRESHOLD
DEFAULT_VOTE_DURATION_HOURS = config.DEFAULT_VOTE_DURATION_HOURS
DEFAULT_ROLE_EDIT_WINDOW_HOURS = config.DEFAULT_ROLE_EDIT_WINDOW_HOURS


async def get_settings(db, guild_id: int):
    """Возвращает строку guild_settings для сервера, создавая её при первом обращении.

    Таблица guild_settings общая для всех когов (позор, роли и т.д.),
    поэтому новые настройки добавляйте туда же через миграцию в database/db.py.
    """
    row = await db.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
    if not row:
        await db.execute(
            """INSERT INTO guild_settings (guild_id, vote_threshold, vote_duration_hours, role_edit_window_hours)
               VALUES (?, ?, ?, ?)""",
            (guild_id, DEFAULT_VOTE_THRESHOLD, DEFAULT_VOTE_DURATION_HOURS, DEFAULT_ROLE_EDIT_WINDOW_HOURS),
        )
        row = await db.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
    return row


def resolve_role(guild: discord.Guild, role_id: int | None, fallback_name: str | None) -> discord.Role | None:
    """Находит роль по сохранённому ID, а если его нет — по названию (регистр не важен)."""
    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
    if fallback_name:
        for role in guild.roles:
            if role.name.lower() == fallback_name.lower():
                return role
    return None
