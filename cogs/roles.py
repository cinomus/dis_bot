import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.settings import get_settings, resolve_role

log = logging.getLogger("cogs.roles")


class RolesCog(commands.GroupCog, name="role", description="Управление ролями сервера"):
    """Быстрое создание ролей, база ролей с описанием и топы по ролям.

    Права на редактирование/удаление роли из базы:
    - роль с ролью "админ" (или указанная в /role setup) — может управлять любой ролью всегда;
    - роль с ролью "бро <3" (или указанная в /role setup) — может редактировать/удалять роль,
      только если она создана через бота и ещё не истекло "окно редактирования"
      (по умолчанию 24ч, настраивается в /role setup);
    - роли, зарегистрированные в базе до появления окна редактирования (editable_until = NULL),
      или роль, у которой окно уже истекло, — редактировать/удалять может только "админ".
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    # ---------- helpers ----------
    async def _get_role_record(self, guild_id: int, role_id: int):
        return await self.bot.db.fetchone(
            "SELECT * FROM managed_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )

    async def _can_manage_role(self, interaction: discord.Interaction, record) -> bool:
        """Проверяет, может ли автор команды редактировать/удалять эту роль."""
        if interaction.user.guild_permissions.administrator:
            return True

        settings = await get_settings(self.bot.db, interaction.guild.id)

        admin_role = resolve_role(interaction.guild, settings["admin_role_id"], config.ROLE_ADMIN_NAME)
        if admin_role and admin_role in interaction.user.roles:
            return True

        editable_until = record["editable_until"] if record else None
        if editable_until:
            try:
                until = datetime.fromisoformat(editable_until)
                if datetime.now(timezone.utc) < until:
                    trusted_role = resolve_role(interaction.guild, settings["trusted_role_id"], config.ROLE_TRUSTED_NAME)
                    if trusted_role and trusted_role in interaction.user.roles:
                        return True
            except ValueError:
                pass

        return False

    # ---------- /role create ----------
    @app_commands.command(name="create", description="Создать новую роль и занести её в базу")
    @app_commands.describe(
        name="Название роли",
        description="Описание роли",
        criteria="За что выдаётся роль",
        color="Цвет в HEX, например #ff0000 (необязательно)",
        stackable="Можно ли выдавать роль одному человеку повторно (для очков в топе)",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str,
        criteria: str,
        color: str = None,
        stackable: bool = False,
    ):
        await interaction.response.defer(thinking=True)

        colour = discord.Colour.default()
        warning = ""
        if color:
            try:
                colour = discord.Colour(int(color.lstrip("#"), 16))
            except ValueError:
                warning = "\n⚠️ Некорректный HEX-цвет, использован цвет по умолчанию."

        discord_role = await interaction.guild.create_role(
            name=name, colour=colour, reason=f"Создано через бота пользователем {interaction.user}"
        )

        settings = await get_settings(self.bot.db, interaction.guild.id)
        window_hours = settings["role_edit_window_hours"] or config.DEFAULT_ROLE_EDIT_WINDOW_HOURS
        editable_until = datetime.now(timezone.utc) + timedelta(hours=window_hours)

        await self.bot.db.execute(
            """INSERT INTO managed_roles
               (role_id, guild_id, name, description, criteria, stackable, created_by, created_at, editable_until)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                discord_role.id,
                interaction.guild.id,
                name,
                description,
                criteria,
                int(stackable),
                interaction.user.id,
                datetime.now(timezone.utc).isoformat(),
                editable_until.isoformat(),
            ),
        )

        embed = discord.Embed(title=f"Роль «{name}» создана", colour=colour)
        embed.add_field(name="Описание", value=description, inline=False)
        embed.add_field(name="За что выдаётся", value=criteria, inline=False)
        embed.add_field(name="Повторная выдача", value="Да" if stackable else "Нет")
        embed.set_footer(
            text=f"Редактировать/удалять могут «{config.ROLE_TRUSTED_NAME}» ещё {window_hours:.0f} ч., затем только «{config.ROLE_ADMIN_NAME}»."
        )
        await interaction.followup.send(content=warning or None, embed=embed)

    # ---------- /role list ----------
    @app_commands.command(name="list", description="Показать все роли, зарегистрированные в базе")
    async def list_roles(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM managed_roles WHERE guild_id = ? ORDER BY created_at", (interaction.guild.id,)
        )
        if not rows:
            await interaction.response.send_message("В базе пока нет ни одной роли. Создайте её через /role create.")
            return

        embed = discord.Embed(title="Роли сервера", colour=discord.Colour.blurple())
        for row in rows[:25]:
            discord_role = interaction.guild.get_role(row["role_id"])
            role_mention = discord_role.mention if discord_role else f"(удалена) {row['name']}"
            embed.add_field(
                name=f"{row['name']} — {role_mention}",
                value=f"{row['description']}\n*За что:* {row['criteria']}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    # ---------- /role info ----------
    @app_commands.command(name="info", description="Подробная информация о роли")
    async def info(self, interaction: discord.Interaction, role: discord.Role):
        record = await self._get_role_record(interaction.guild.id, role.id)
        if not record:
            await interaction.response.send_message("Эта роль не зарегистрирована в базе бота.", ephemeral=True)
            return

        holders = [m for m in interaction.guild.members if role in m.roles]
        embed = discord.Embed(title=f"Роль: {record['name']}", colour=role.colour)
        embed.add_field(name="Описание", value=record["description"] or "—", inline=False)
        embed.add_field(name="За что выдаётся", value=record["criteria"] or "—", inline=False)
        embed.add_field(name="Носителей сейчас", value=str(len(holders)))
        embed.add_field(name="Повторная выдача", value="Да" if record["stackable"] else "Нет")

        if record["editable_until"]:
            until = datetime.fromisoformat(record["editable_until"])
            if datetime.now(timezone.utc) < until:
                left = (until - datetime.now(timezone.utc)).total_seconds() / 3600
                embed.add_field(
                    name="Кто может редактировать",
                    value=f"«{config.ROLE_TRUSTED_NAME}» ещё {left:.1f} ч., затем только «{config.ROLE_ADMIN_NAME}»",
                    inline=False,
                )
            else:
                embed.add_field(name="Кто может редактировать", value=f"Только «{config.ROLE_ADMIN_NAME}» (окно истекло)", inline=False)
        else:
            embed.add_field(name="Кто может редактировать", value=f"Только «{config.ROLE_ADMIN_NAME}»", inline=False)

        await interaction.response.send_message(embed=embed)

    # ---------- /role give ----------
    @app_commands.command(name="give", description="Выдать роль участнику")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        reason: str = "",
    ):
        record = await self._get_role_record(interaction.guild.id, role.id)
        if not record:
            await interaction.response.send_message(
                "Эта роль не зарегистрирована в базе. Сначала создайте её через /role create "
                "(или зарегистрируйте существующую вручную в БД).",
                ephemeral=True,
            )
            return

        if not record["stackable"] and role in member.roles:
            await interaction.response.send_message(
                f"{member.mention} уже имеет роль {role.mention}, повторная выдача для неё отключена.",
                ephemeral=True,
            )
            return

        await member.add_roles(role, reason=f"Выдано {interaction.user} через бота: {reason}")
        await self.bot.db.execute(
            """INSERT INTO role_grants (role_id, user_id, guild_id, granted_by, granted_at, reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (role.id, member.id, interaction.guild.id, interaction.user.id, datetime.now(timezone.utc).isoformat(), reason),
        )

        embed = discord.Embed(description=f"{member.mention} получает роль {role.mention}!", colour=role.colour)
        if reason:
            embed.add_field(name="За что", value=reason)
        await interaction.response.send_message(embed=embed)

    # ---------- /role top ----------
    @app_commands.command(name="top", description="Топ участников по роли")
    async def top(self, interaction: discord.Interaction, role: discord.Role, limit: int = 10):
        record = await self._get_role_record(interaction.guild.id, role.id)
        if not record:
            await interaction.response.send_message("Эта роль не зарегистрирована в базе.", ephemeral=True)
            return

        limit = max(1, min(limit, 25))

        if record["stackable"]:
            rows = await self.bot.db.fetchall(
                """SELECT user_id, COUNT(*) as cnt FROM role_grants
                   WHERE role_id = ? AND guild_id = ?
                   GROUP BY user_id ORDER BY cnt DESC LIMIT ?""",
                (role.id, interaction.guild.id, limit),
            )
            lines = [
                f"**{i}.** <@{row['user_id']}> — {row['cnt']} раз(а)"
                for i, row in enumerate(rows, start=1)
            ]
        else:
            rows = await self.bot.db.fetchall(
                """SELECT user_id, MIN(granted_at) as first_grant FROM role_grants
                   WHERE role_id = ? AND guild_id = ?
                   GROUP BY user_id ORDER BY first_grant ASC LIMIT ?""",
                (role.id, interaction.guild.id, limit),
            )
            lines = [
                f"**{i}.** <@{row['user_id']}> — с {row['first_grant'][:10]}"
                for i, row in enumerate(rows, start=1)
            ]

        if not lines:
            lines = ["Пока никто не получал эту роль."]

        embed = discord.Embed(title=f"Топ по роли «{record['name']}»", description="\n".join(lines), colour=role.colour)
        await interaction.response.send_message(embed=embed)

    # ---------- /role edit ----------
    @app_commands.command(name="edit", description="Изменить название/описание/критерии/цвет роли")
    @app_commands.describe(
        name="Новое название роли (необязательно)",
        description="Новое описание (необязательно)",
        criteria="Новое «за что выдаётся» (необязательно)",
        color="Новый цвет в HEX, например #00ff00 (необязательно)",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        name: str = None,
        description: str = None,
        criteria: str = None,
        color: str = None,
    ):
        record = await self._get_role_record(interaction.guild.id, role.id)
        if not record:
            await interaction.response.send_message("Эта роль не зарегистрирована в базе бота.", ephemeral=True)
            return

        if not await self._can_manage_role(interaction, record):
            await interaction.response.send_message(
                f"⛔ Редактировать эту роль сейчас может только «{config.ROLE_ADMIN_NAME}» "
                f"(окно для «{config.ROLE_TRUSTED_NAME}» истекло или не действует для этой роли).",
                ephemeral=True,
            )
            return

        if not any([name, description, criteria, color]):
            await interaction.response.send_message("Укажите хотя бы одно поле для изменения.", ephemeral=True)
            return

        updates = {"name": name, "description": description, "criteria": criteria}
        set_clauses = []
        params = []
        for column, value in updates.items():
            if value is not None:
                set_clauses.append(f"{column} = ?")
                params.append(value)

        colour = None
        if color:
            try:
                colour = discord.Colour(int(color.lstrip("#"), 16))
            except ValueError:
                await interaction.response.send_message("Некорректный HEX-цвет.", ephemeral=True)
                return

        discord_edit_kwargs = {}
        if name:
            discord_edit_kwargs["name"] = name
        if colour is not None:
            discord_edit_kwargs["colour"] = colour
        if discord_edit_kwargs:
            await role.edit(reason=f"Изменено через бота пользователем {interaction.user}", **discord_edit_kwargs)

        if set_clauses:
            params.extend([interaction.guild.id, role.id])
            await self.bot.db.execute(
                f"UPDATE managed_roles SET {', '.join(set_clauses)} WHERE guild_id = ? AND role_id = ?",
                tuple(params),
            )

        await interaction.response.send_message(f"Роль {role.mention} обновлена.")

    # ---------- /role delete ----------
    @app_commands.command(name="delete", description="Удалить роль из базы (и опционально из Discord)")
    @app_commands.describe(delete_from_discord="Удалить роль полностью с сервера, а не только из базы бота")
    async def delete(self, interaction: discord.Interaction, role: discord.Role, delete_from_discord: bool = False):
        record = await self._get_role_record(interaction.guild.id, role.id)
        if not record:
            await interaction.response.send_message("Эта роль не зарегистрирована в базе бота.", ephemeral=True)
            return

        if not await self._can_manage_role(interaction, record):
            await interaction.response.send_message(
                f"⛔ Удалять эту роль сейчас может только «{config.ROLE_ADMIN_NAME}» "
                f"(окно для «{config.ROLE_TRUSTED_NAME}» истекло или не действует для этой роли).",
                ephemeral=True,
            )
            return

        await self.bot.db.execute(
            "DELETE FROM managed_roles WHERE guild_id = ? AND role_id = ?", (interaction.guild.id, role.id)
        )
        if delete_from_discord:
            await role.delete(reason=f"Удалено через бота пользователем {interaction.user}")
        await interaction.response.send_message(
            f"Роль «{role.name}» удалена из базы" + (" и с сервера." if delete_from_discord else ".")
        )

    # ---------- /role setup ----------
    @app_commands.command(name="setup", description="Настроить права на редактирование ролей")
    @app_commands.describe(
        admin_role="Роль, которая всегда может редактировать/удалять любые роли из базы",
        trusted_role='Роль (например "бро <3"), которая может редактировать/удалять роль, пока не истекло окно',
        edit_window_hours="Сколько часов после создания роли доверенная роль может её редактировать/удалять",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_permissions(
        self,
        interaction: discord.Interaction,
        admin_role: discord.Role = None,
        trusted_role: discord.Role = None,
        edit_window_hours: float = None,
    ):
        settings = await get_settings(self.bot.db, interaction.guild.id)
        new_admin_id = admin_role.id if admin_role else settings["admin_role_id"]
        new_trusted_id = trusted_role.id if trusted_role else settings["trusted_role_id"]
        new_window = edit_window_hours if edit_window_hours is not None else settings["role_edit_window_hours"]

        await self.bot.db.execute(
            "UPDATE guild_settings SET admin_role_id = ?, trusted_role_id = ?, role_edit_window_hours = ? WHERE guild_id = ?",
            (new_admin_id, new_trusted_id, new_window, interaction.guild.id),
        )

        admin_text = f"<@&{new_admin_id}>" if new_admin_id else f"по названию «{config.ROLE_ADMIN_NAME}»"
        trusted_text = f"<@&{new_trusted_id}>" if new_trusted_id else f"по названию «{config.ROLE_TRUSTED_NAME}»"
        await interaction.response.send_message(
            f"Готово.\nАдмин-роль: {admin_text}\nДоверенная роль: {trusted_text}\nОкно редактирования: {new_window} ч."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))
