import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.settings import get_settings

log = logging.getLogger("cogs.shame")

# Забавные подписи при снятии/окончании позора. Добавляйте свои варианты сюда.
FUNNY_RELEASE_MESSAGES = [
    "{mention} был(а) отмыт(а) от позора, но всё равно остался(ась) дырявым(ой).",
    "Позор снят! Пятно на репутации {mention} ещё видно пару дней, но технически всё чисто.",
    "{mention} вышел(шла) на свободу. Готовьте новый повод, у нас тут не курорт.",
    "Наказание окончено. {mention}, можно снова всех бесить — но уже с чистой совестью!",
    "Официально: {mention} прощён(а) народом. Неофициально — все всё помнят.",
    "{mention} реабилитирован(а). Аплодисменты (сдержанные).",
]

VOTE_EMOJI_YES = "✅"
VOTE_EMOJI_NO = "❌"


class ShameCog(commands.GroupCog, name="shame", description="Система позора"):
    """Выдача/снятие позора, голосования и авто-уведомления по истечении срока."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        self.check_expired_shames.start()
        self.check_expired_votes.start()

    def cog_unload(self):
        self.check_expired_shames.cancel()
        self.check_expired_votes.cancel()

    # ---------- helpers ----------
    async def _get_settings(self, guild_id: int):
        return await get_settings(self.bot.db, guild_id)

    async def _get_shame_role(self, guild: discord.Guild):
        settings = await self._get_settings(guild.id)
        if settings["shame_role_id"]:
            role = guild.get_role(settings["shame_role_id"])
            if role:
                return role
        role = await guild.create_role(
            name="Позор", colour=discord.Colour.dark_grey(), reason="Автоматически создано ботом"
        )
        await self.bot.db.execute("UPDATE guild_settings SET shame_role_id = ? WHERE guild_id = ?", (role.id, guild.id))
        return role

    async def _apply_shame(self, guild, member: discord.Member, reason: str, duration_hours: float, given_by: int):
        role = await self._get_shame_role(guild)
        await member.add_roles(role, reason=f"Позор: {reason}")
        expires_at = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        await self.bot.db.execute(
            """INSERT INTO shame_records (guild_id, user_id, reason, given_by, given_at, expires_at, active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (guild.id, member.id, reason, given_by, datetime.now(timezone.utc).isoformat(), expires_at.isoformat()),
        )

    async def _release_shame(self, guild, member: discord.Member, removed_reason: str, announce: bool):
        row = await self.bot.db.fetchone(
            "SELECT * FROM shame_records WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY given_at DESC LIMIT 1",
            (guild.id, member.id),
        )
        settings = await self._get_settings(guild.id)
        role = guild.get_role(settings["shame_role_id"]) if settings["shame_role_id"] else None
        if role and role in member.roles:
            await member.remove_roles(role, reason=f"Снятие позора: {removed_reason}")
        if row:
            await self.bot.db.execute(
                "UPDATE shame_records SET active = 0, removed_reason = ? WHERE id = ?", (removed_reason, row["id"])
            )
        if announce and settings["log_channel_id"]:
            channel = guild.get_channel(settings["log_channel_id"])
            if channel:
                text = random.choice(FUNNY_RELEASE_MESSAGES).format(mention=member.mention)
                await channel.send(text)
        return row

    # ---------- /shame give ----------
    @app_commands.command(name="give", description="Выдать позор участнику напрямую")
    @app_commands.describe(duration_hours="На сколько часов выдать позор", reason="Причина")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def give(self, interaction: discord.Interaction, member: discord.Member, duration_hours: float, reason: str):
        await self._apply_shame(interaction.guild, member, reason, duration_hours, interaction.user.id)
        await interaction.response.send_message(
            f"🫡 {member.mention} получает позор на {duration_hours} ч. Причина: {reason}"
        )

    # ---------- /shame remove ----------
    @app_commands.command(name="remove", description="Снять позор с участника напрямую")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, member: discord.Member, reason: str = "решение модератора"):
        await self._release_shame(interaction.guild, member, reason, announce=False)
        text = random.choice(FUNNY_RELEASE_MESSAGES).format(mention=member.mention)
        await interaction.response.send_message(text)

    # ---------- /shame status ----------
    @app_commands.command(name="status", description="Проверить, сколько осталось позора у участника")
    async def status(self, interaction: discord.Interaction, member: discord.Member):
        row = await self.bot.db.fetchone(
            "SELECT * FROM shame_records WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY given_at DESC LIMIT 1",
            (interaction.guild.id, member.id),
        )
        if not row:
            await interaction.response.send_message(f"{member.mention} сейчас не опозорен(а).")
            return
        expires = datetime.fromisoformat(row["expires_at"])
        remaining = expires - datetime.now(timezone.utc)
        hours_left = max(remaining.total_seconds() / 3600, 0)
        await interaction.response.send_message(
            f"{member.mention} опозорен(а) ещё {hours_left:.1f} ч. Причина: {row['reason']}"
        )

    # ---------- /shame list ----------
    @app_commands.command(name="list", description="Список тех, кто сейчас опозорен")
    async def list_shamed(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shame_records WHERE guild_id = ? AND active = 1", (interaction.guild.id,)
        )
        if not rows:
            await interaction.response.send_message("Сейчас никто не в позоре. Скучно тут у вас.")
            return
        lines = []
        for row in rows:
            expires = datetime.fromisoformat(row["expires_at"])
            hours_left = max((expires - datetime.now(timezone.utc)).total_seconds() / 3600, 0)
            lines.append(f"<@{row['user_id']}> — {hours_left:.1f} ч. осталось ({row['reason']})")
        await interaction.response.send_message("\n".join(lines))

    # ---------- /shame vote_give ----------
    @app_commands.command(name="vote_give", description="Запустить голосование за выдачу позора")
    @app_commands.describe(shame_duration_hours="На сколько часов будет позор, если голосование пройдёт")
    async def vote_give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
        shame_duration_hours: float = 24.0,
    ):
        await self._start_vote(interaction, member, reason, "give", shame_duration_hours)

    # ---------- /shame vote_remove ----------
    @app_commands.command(name="vote_remove", description="Запустить голосование за снятие позора (за заслуги)")
    async def vote_remove(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        await self._start_vote(interaction, member, reason, "remove", 0)

    async def _start_vote(self, interaction, member, reason, vote_type, shame_duration_hours):
        settings = await self._get_settings(interaction.guild.id)
        window_hours = settings["vote_duration_hours"] or 1
        threshold = settings["vote_threshold"] or 3

        action_text = "выдать 🫡 позор" if vote_type == "give" else "снять 🧼 позор (за заслуги)"
        embed = discord.Embed(
            title=f"Голосование: {action_text}",
            description=(
                f"Участник: {member.mention}\nПричина: {reason}\n\n"
                f"Голосуйте {VOTE_EMOJI_YES} за / {VOTE_EMOJI_NO} против.\n"
                f"Нужно минимум {threshold} голосов «за», чтобы решение прошло."
            ),
            colour=discord.Colour.orange(),
        )
        embed.set_footer(text=f"Голосование завершится через {window_hours} ч.")
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        await message.add_reaction(VOTE_EMOJI_YES)
        await message.add_reaction(VOTE_EMOJI_NO)

        ends_at = datetime.now(timezone.utc) + timedelta(hours=window_hours)
        await self.bot.db.execute(
            """INSERT INTO shame_votes (guild_id, channel_id, message_id, target_user_id, vote_type, reason,
               duration_hours, initiator_id, created_at, ends_at, status, threshold)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                interaction.guild.id,
                interaction.channel.id,
                message.id,
                member.id,
                vote_type,
                reason,
                shame_duration_hours,
                interaction.user.id,
                datetime.now(timezone.utc).isoformat(),
                ends_at.isoformat(),
                threshold,
            ),
        )

    # ---------- /shame setup ----------
    @app_commands.command(name="setup", description="Настроить систему позора для сервера")
    @app_commands.describe(
        role="Роль позора (если не указана — будет создана автоматически)",
        vote_threshold="Сколько голосов «за» нужно для принятия решения",
        vote_window_hours="Сколько часов длится голосование",
        log_channel="Канал для объявлений об окончании позора",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_shame(
        self,
        interaction: discord.Interaction,
        role: discord.Role = None,
        vote_threshold: int = 3,
        vote_window_hours: float = 24.0,
        log_channel: discord.TextChannel = None,
    ):
        await self._get_settings(interaction.guild.id)
        role_id = role.id if role else (await self._get_shame_role(interaction.guild)).id
        await self.bot.db.execute(
            """UPDATE guild_settings SET shame_role_id = ?, vote_threshold = ?, vote_duration_hours = ?, log_channel_id = ?
               WHERE guild_id = ?""",
            (
                role_id,
                vote_threshold,
                vote_window_hours,
                log_channel.id if log_channel else interaction.channel.id,
                interaction.guild.id,
            ),
        )
        await interaction.response.send_message(
            f"Настроено: роль <@&{role_id}>, порог голосов: {vote_threshold}, окно голосования: {vote_window_hours} ч."
        )

    # ---------- фоновые задачи ----------
    @tasks.loop(minutes=1)
    async def check_expired_shames(self):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shame_records WHERE active = 1 AND expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            member = guild.get_member(row["user_id"])
            if member:
                await self._release_shame(guild, member, "срок истёк", announce=True)
            else:
                await self.bot.db.execute("UPDATE shame_records SET active = 0 WHERE id = ?", (row["id"],))

    @tasks.loop(minutes=1)
    async def check_expired_votes(self):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shame_votes WHERE status = 'active' AND ends_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            channel = guild.get_channel(row["channel_id"])
            passed = False
            yes_count = 0
            no_count = 0
            if channel:
                try:
                    message = await channel.fetch_message(row["message_id"])
                    for reaction in message.reactions:
                        if str(reaction.emoji) == VOTE_EMOJI_YES:
                            yes_count = max(reaction.count - 1, 0)  # минус реакция самого бота
                        elif str(reaction.emoji) == VOTE_EMOJI_NO:
                            no_count = max(reaction.count - 1, 0)
                    passed = yes_count >= row["threshold"] and yes_count > no_count
                except discord.NotFound:
                    pass

            member = guild.get_member(row["target_user_id"])
            status = "failed"
            if passed and member:
                status = "passed"
                if row["vote_type"] == "give":
                    await self._apply_shame(guild, member, row["reason"], row["duration_hours"], row["initiator_id"])
                    if channel:
                        await channel.send(
                            f"✅ Голосование прошло! {member.mention} получает позор на {row['duration_hours']} ч."
                        )
                else:
                    await self._release_shame(guild, member, f"снято голосованием: {row['reason']}", announce=False)
                    if channel:
                        text = random.choice(FUNNY_RELEASE_MESSAGES).format(mention=member.mention)
                        await channel.send(f"✅ Голосование прошло! {text}")
            else:
                if channel:
                    await channel.send(f"❌ Голосование не набрало нужного числа голосов ({yes_count}/{row['threshold']}).")

            await self.bot.db.execute("UPDATE shame_votes SET status = ? WHERE id = ?", (status, row["id"]))

    @check_expired_shames.before_loop
    async def before_check_expired_shames(self):
        await self.bot.wait_until_ready()

    @check_expired_votes.before_loop
    async def before_check_expired_votes(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ShameCog(bot))
