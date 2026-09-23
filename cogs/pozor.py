import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils.settings import get_settings, resolve_role
from utils.time import fmt_msk

log = logging.getLogger("cogs.pozor")

# Забавные подписи при снятии/окончании позора. Добавляйте свои варианты сюда.
FUNNY_RELEASE_MESSAGES = [
    "{mention} был(а) отмыт(а) от позора, но всё равно остался(ась) дырявым(ой).",
    "Позор снят! Пятно на репутации {mention} ещё видно пару дней, но технически всё чисто.",
    "{mention} вышел(шла) на свободу. Готовьте новый повод, у нас тут не курорт.",
    "Наказание окончено. {mention}, можно снова всех бесить — но уже с чистой совестью!",
    "Официально: {mention} прощён(а) народом. Неофициально — все всё помнят.",
    "{mention} реабилитирован(а). Аплодисменты (сдержанные).",
    "{mention} выписан(а) из позора. Справку о невиновности не выдаём: чернила кончились.",
    "Срок отбыт. {mention} снова среди нас — делайте вид, что ничего не было. Мы не сможем.",
    "{mention} отмыт(а) до скрипа. Скрип, правда, подозрительный.",
    "Позор с {mention} снят по амнистии. Амнистия временная, характер — нет.",
    "{mention} свободен(на). Штамп в личном деле остаётся, просто его теперь не видно.",
    "Комиссия постановила: {mention} больше не позор сервера. Воздержавшихся было много.",
    "{mention} возвращён(а) в общество. Общество пока думает, радоваться ли.",
    "Чисто! {mention} можно пускать к приличным людям. На расстоянии и под присмотром.",
]

# Подписи в момент выдачи позора. Плейсхолдеры: {mention}, {hours}, {reason}.
FUNNY_GIVE_MESSAGES = [
    "{mention} официально опозорен(а) на {hours} ч. Причина: {reason}. Носите с достоинством, которого нет.",
    "Позор выдан. {mention} на {hours} ч. отправляется в петушиный угол. За что: {reason}.",
    "{mention} пойман(а): {reason}. Срок — {hours} ч., апелляция не принимается.",
    "Решение подписано. {mention} в позоре {hours} ч. Формулировка скромная: {reason}.",
    "{mention}, это не бан, это воспитательная работа на {hours} ч. Тема урока: {reason}.",
    "Печать поставлена. {mention} опозорен(а) на {hours} ч. В сопроводиловке: {reason}.",
    "{mention} получает фирменный позор на {hours} ч. Основание: {reason}. Справку дауна потом не прячьте.",
    "Готово. {mention} теперь ходячая оговорка на {hours} ч. Повод: {reason}.",
]

# Подписи супер позора. Плейсхолдеры: {mention}, {reason}.
FUNNY_SUPER_MESSAGES = [
    "{mention} получает супер позор. 10 минут без микрофона: слушать можно, из канала не выгоняем. В статистике это 100 обычных. Причина: {reason}.",
    "Супер позор. {mention} 10 минут сидит с закрытым ртом и открытыми ушами. В досье это сотня. Формулировка: {reason}.",
    "{mention}, микрофон на паузе 10 минут. Канал ваш, голос — нет. Счётчик +100. За что: {reason}.",
    "Печать супер позора. {mention} молчит в голосе 10 минут и всё слышит. Снять метку может только админ. Основание: {reason}.",
]

SUPER_MUTE_MINUTES = 10
SUPER_WEIGHT = 100


def _fmt_hours(hours: float) -> str:
    if abs(hours - round(hours)) < 0.05:
        return str(int(round(hours)))
    return f"{hours:.1f}"


def _fmt_dt(value: str | None) -> str:
    return fmt_msk(value)


def _times_label(count: int) -> str:
    n = abs(count) % 100
    n1 = n % 10
    if 11 <= n <= 14 or n1 == 1 or not 2 <= n1 <= 4:
        return f"{count} раз"
    return f"{count} раза"


def _fill(template: str, **kwargs) -> str:
    text = template
    for key, value in kwargs.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _is_super(row) -> bool:
    try:
        return bool(row["is_super"])
    except (KeyError, IndexError, TypeError):
        return False


def _shame_weight(row) -> int:
    return SUPER_WEIGHT if _is_super(row) else 1


def _mute_left_minutes(row) -> float | None:
    """Сколько минут молчания ещё осталось от выдачи супер позора."""
    try:
        start = datetime.fromisoformat(row["given_at"])
    except (TypeError, ValueError):
        return None
    left = (start + timedelta(minutes=SUPER_MUTE_MINUTES) - datetime.now(timezone.utc)).total_seconds() / 60
    if left <= 0:
        return None
    return left


def _fmt_minutes(minutes: float) -> str:
    whole = int(minutes + 0.999)
    return str(max(whole, 1))


def _record_hours(row) -> float:
    """Часы позора: полный текущий срок, а если сняли раньше — время до снятия."""
    try:
        start = datetime.fromisoformat(row["given_at"])
    except (TypeError, ValueError):
        return 0.0
    if _is_super(row):
        end = datetime.now(timezone.utc)
        if row["removed_at"]:
            try:
                end = datetime.fromisoformat(row["removed_at"])
            except ValueError:
                pass
        return max((end - start).total_seconds() / 3600, 0)
    try:
        planned = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else start
    except (TypeError, ValueError):
        planned = start
    end = planned
    removed_at = row["removed_at"]
    if removed_at:
        try:
            removed = datetime.fromisoformat(removed_at)
        except ValueError:
            removed = planned
        if removed < planned:
            end = removed
    return max((end - start).total_seconds() / 3600, 0)

VOTE_EMOJI_YES = "✅"
VOTE_EMOJI_NO = "❌"


class PozorCog(commands.GroupCog, name="pozor", description="Система позора"):
    """Выдача и продление позора, история, топ, голосования и кулдаун после снятия.

    Таблицы в БД исторически называются shame_* — это внутренние имена,
    пользовательские команды живут в группе /pozor.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        self._resolving_votes: set[int] = set()
        # До какого момента человеку нельзя говорить. Канал при этом не покидаем.
        self._muted_until: dict[tuple[int, int], datetime] = {}
        # Микрофон вернуть, когда человек окажется в голосовом (сейчас его там нет).
        self._unmute_later: set[tuple[int, int]] = set()
        self.check_expired_pozor.start()
        self.check_votes.start()

    def cog_unload(self):
        self.check_expired_pozor.cancel()
        self.check_votes.cancel()

    # ---------- helpers ----------
    async def _get_settings(self, guild_id: int):
        return await get_settings(self.bot.db, guild_id)

    async def _get_pozor_role(self, guild: discord.Guild):
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

    async def _active_pozor(self, guild_id: int, user_id: int):
        """Обычный активный позор. Супер позор сюда не попадает: его нельзя продлить или снять как обычный."""
        return await self.bot.db.fetchone(
            """SELECT * FROM shame_records
               WHERE guild_id = ? AND user_id = ? AND active = 1 AND COALESCE(is_super, 0) = 0
               ORDER BY given_at DESC LIMIT 1""",
            (guild_id, user_id),
        )

    async def _active_super(self, guild_id: int, user_id: int):
        return await self.bot.db.fetchone(
            """SELECT * FROM shame_records
               WHERE guild_id = ? AND user_id = ? AND active = 1 AND is_super = 1
               ORDER BY given_at DESC LIMIT 1""",
            (guild_id, user_id),
        )

    async def _any_active(self, guild_id: int, user_id: int):
        return await self.bot.db.fetchone(
            "SELECT id FROM shame_records WHERE guild_id = ? AND user_id = ? AND active = 1 LIMIT 1",
            (guild_id, user_id),
        )

    async def _is_admin(self, user, guild: discord.Guild) -> bool:
        member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
        if member is None:
            return False
        if member.guild_permissions.administrator:
            return True
        settings = await self._get_settings(guild.id)
        admin_role = resolve_role(guild, settings["admin_role_id"], config.ROLE_ADMIN_NAME)
        return admin_role is not None and admin_role in member.roles

    async def _active_vote(self, guild_id: int, user_id: int):
        return await self.bot.db.fetchone(
            """SELECT * FROM shame_votes
               WHERE guild_id = ? AND target_user_id = ? AND status = 'active'
               ORDER BY created_at DESC LIMIT 1""",
            (guild_id, user_id),
        )

    def _cooldown_hours(self, settings) -> float:
        value = settings["pozor_cooldown_hours"]
        if value is None:
            value = config.DEFAULT_POZOR_COOLDOWN_HOURS
        return max(float(value), 0)

    async def _cooldown_left(self, guild_id: int, user_id: int, settings) -> float | None:
        hours = self._cooldown_hours(settings)
        if hours <= 0:
            return None
        row = await self.bot.db.fetchone(
            """SELECT removed_at FROM shame_records
               WHERE guild_id = ? AND user_id = ? AND active = 0 AND removed_at IS NOT NULL
               ORDER BY removed_at DESC LIMIT 1""",
            (guild_id, user_id),
        )
        if not row or not row["removed_at"]:
            return None
        try:
            removed = datetime.fromisoformat(row["removed_at"])
        except ValueError:
            return None
        until = removed + timedelta(hours=hours)
        left = (until - datetime.now(timezone.utc)).total_seconds() / 3600
        if left <= 0:
            return None
        return left

    async def _apply_pozor(self, guild, member: discord.Member, reason: str, duration_hours: float, given_by: int):
        """Выдаёт позор или продлевает уже идущий срок. Возвращает (продлён, момент окончания)."""
        role = await self._get_pozor_role(guild)
        await member.add_roles(role, reason=f"Позор: {reason}")
        existing = await self._active_pozor(guild.id, member.id)
        now = datetime.now(timezone.utc)
        if existing:
            try:
                base = datetime.fromisoformat(existing["expires_at"])
            except (TypeError, ValueError):
                base = now
            if base < now:
                base = now
            expires_at = base + timedelta(hours=duration_hours)
            extra = f"+{_fmt_hours(duration_hours)} ч.: {reason}"
            combined = f"{existing['reason']} | {extra}" if existing["reason"] else extra
            if len(combined) > 500:
                combined = combined[-500:]
            await self.bot.db.execute(
                "UPDATE shame_records SET expires_at = ?, reason = ? WHERE id = ?",
                (expires_at.isoformat(), combined, existing["id"]),
            )
            return True, expires_at

        expires_at = now + timedelta(hours=duration_hours)
        await self.bot.db.execute(
            """INSERT INTO shame_records (guild_id, user_id, reason, given_by, given_at, expires_at, active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (guild.id, member.id, reason, given_by, now.isoformat(), expires_at.isoformat()),
        )
        return False, expires_at

    def _in_voice(self, member: discord.Member) -> bool:
        voice = member.voice
        return voice is not None and voice.channel is not None

    async def _clear_our_timeout(self, member: discord.Member, issued: datetime):
        """Старый супер позор ставил таймаут, а он выкидывает из голосового. Снимаем только короткий."""
        if not member.is_timed_out() or member.timed_out_until is None:
            return
        if member.timed_out_until > issued + timedelta(minutes=SUPER_MUTE_MINUTES, seconds=30):
            return
        try:
            await member.timeout(None, reason="Супер позор: вместо таймаута серверный мут")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Не удалось снять старый таймаут супер позора %s", member.id)

    async def _server_mute(self, member: discord.Member, muted: bool, reason: str) -> bool:
        """Серверный мут: нельзя говорить, слышно всё, из канала не выкидывает."""
        if not self._in_voice(member):
            return False
        if bool(member.voice.mute) == muted:
            return True
        try:
            await member.edit(mute=muted, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Не удалось %s микрофон %s", "выключить" if muted else "включить", member.id)
            return False
        return True

    async def _apply_super(self, guild, member: discord.Member, reason: str, given_by: int) -> str | None:
        """Выдаёт супер позор и выключает микрофон на 10 минут. Канал не трогает."""
        role = await self._get_pozor_role(guild)
        await member.add_roles(role, reason=f"Супер позор: {reason}")
        now = datetime.now(timezone.utc)
        await self._clear_our_timeout(member, now)
        in_voice = self._in_voice(member)
        already_muted = in_voice and member.voice.mute
        bot_muted = 0 if already_muted else 1
        note = None
        if in_voice and not already_muted:
            if not await self._server_mute(member, True, f"Супер позор: {reason}"):
                bot_muted = 0
                note = "Метку поставил, но микрофон не выключился: нужно право мутить участников и роль бота выше."
        await self.bot.db.execute(
            """INSERT INTO shame_records
               (guild_id, user_id, reason, given_by, given_at, expires_at, active, is_super, bot_muted)
               VALUES (?, ?, ?, ?, ?, NULL, 1, 1, ?)""",
            (guild.id, member.id, reason, given_by, now.isoformat(), bot_muted),
        )
        self._muted_until[(guild.id, member.id)] = now + timedelta(minutes=SUPER_MUTE_MINUTES)
        return note

    async def _release_super(self, guild, member: discord.Member, removed_reason: str):
        """Снимает супер позор и возвращает микрофон, если его выключали мы."""
        row = await self._active_super(guild.id, member.id)
        owed = await self.bot.db.fetchone(
            """SELECT id FROM shame_records
               WHERE guild_id = ? AND user_id = ? AND active = 1 AND is_super = 1 AND bot_muted = 1
               LIMIT 1""",
            (guild.id, member.id),
        )
        now = datetime.now(timezone.utc)
        await self.bot.db.execute(
            """UPDATE shame_records
               SET active = 0, removed_reason = ?, removed_at = ?
               WHERE guild_id = ? AND user_id = ? AND active = 1 AND is_super = 1""",
            (removed_reason, now.isoformat(), guild.id, member.id),
        )
        key = (guild.id, member.id)
        self._muted_until.pop(key, None)
        if row and row["given_at"]:
            try:
                await self._clear_our_timeout(member, datetime.fromisoformat(row["given_at"]))
            except ValueError:
                pass
        if owed:
            if await self._server_mute(member, False, f"Снятие супер позора: {removed_reason}"):
                await self._clear_bot_muted(guild.id, member.id)
            elif not self._in_voice(member):
                self._unmute_later.add(key)
        settings = await self._get_settings(guild.id)
        still_active = await self._any_active(guild.id, member.id)
        role = guild.get_role(settings["shame_role_id"]) if settings["shame_role_id"] else None
        if role and role in member.roles and not still_active:
            await member.remove_roles(role, reason=f"Снятие супер позора: {removed_reason}")

    async def _clear_bot_muted(self, guild_id: int, user_id: int):
        self._unmute_later.discard((guild_id, user_id))
        await self.bot.db.execute(
            """UPDATE shame_records SET bot_muted = 0
               WHERE guild_id = ? AND user_id = ? AND is_super = 1 AND bot_muted = 1""",
            (guild_id, user_id),
        )

    async def _lift_expired_voice_mutes(self):
        """Через 10 минут снова даёт говорить. Метку супер позора это не снимает."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shame_records WHERE is_super = 1 AND (active = 1 OR bot_muted = 1)"
        )
        latest_active: dict[tuple[int, int], object] = {}
        owed: set[tuple[int, int]] = set()
        for row in rows:
            key = (row["guild_id"], row["user_id"])
            if row["bot_muted"]:
                owed.add(key)
            if row["active"]:
                current = latest_active.get(key)
                if current is None or row["given_at"] > current["given_at"]:
                    latest_active[key] = row
        now = datetime.now(timezone.utc)
        for key in owed | set(latest_active):
            guild_id, user_id = key
            row = latest_active.get(key)
            mute_end = None
            if row is not None:
                try:
                    mute_end = datetime.fromisoformat(row["given_at"]) + timedelta(minutes=SUPER_MUTE_MINUTES)
                except (TypeError, ValueError):
                    mute_end = now
            if mute_end is not None and mute_end > now:
                self._muted_until[key] = mute_end
                guild = self.bot.get_guild(guild_id)
                member = await self._get_member(guild, user_id) if guild else None
                if member and self._in_voice(member) and not member.voice.mute:
                    await self._server_mute(member, True, "Супер позор")
                continue
            self._muted_until.pop(key, None)
            if key not in owed:
                continue
            guild = self.bot.get_guild(guild_id)
            member = await self._get_member(guild, user_id) if guild else None
            if member is None:
                await self._clear_bot_muted(guild_id, user_id)
                continue
            if not self._in_voice(member):
                self._unmute_later.add(key)
                continue
            if await self._server_mute(member, False, "Супер позор: 10 минут без микрофона прошли"):
                await self._clear_bot_muted(guild_id, user_id)

    def _give_text(self, member: discord.Member, reason: str, duration_hours: float, extended: bool, expires_at: datetime) -> str:
        hours = _fmt_hours(duration_hours)
        if extended:
            left = max((expires_at - datetime.now(timezone.utc)).total_seconds() / 3600, 0)
            return (
                f"У {member.mention} позор ещё не кончился, так что сверху накинули {hours} ч. "
                f"Осталось {_fmt_hours(left)} ч. Причина добавки: {reason}"
            )
        shown_reason = reason if len(reason) <= 300 else reason[:297] + "..."
        return _fill(
            random.choice(FUNNY_GIVE_MESSAGES),
            mention=member.mention,
            hours=hours,
            reason=shown_reason,
        )

    async def _release_pozor(self, guild, member: discord.Member, removed_reason: str, announce: bool):
        row = await self._active_pozor(guild.id, member.id)
        settings = await self._get_settings(guild.id)
        if row:
            await self.bot.db.execute(
                "UPDATE shame_records SET active = 0, removed_reason = ?, removed_at = ? WHERE id = ?",
                (removed_reason, datetime.now(timezone.utc).isoformat(), row["id"]),
            )
        still_active = await self._any_active(guild.id, member.id)
        role = guild.get_role(settings["shame_role_id"]) if settings["shame_role_id"] else None
        if role and role in member.roles and not still_active:
            await member.remove_roles(role, reason=f"Снятие позора: {removed_reason}")
        if announce and settings["log_channel_id"]:
            channel = guild.get_channel(settings["log_channel_id"])
            if channel:
                text = random.choice(FUNNY_RELEASE_MESSAGES).format(mention=member.mention)
                await channel.send(text)
        return row

    async def _get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    def _vote_channel(self, guild: discord.Guild, channel_id: int):
        return guild.get_channel_or_thread(channel_id)

    async def _human_reactions(self, reaction: discord.Reaction) -> int:
        """Считает голоса людей. Реакция бота, которой помечено сообщение, не голос."""
        count = 0
        async for user in reaction.users():
            if self.bot.user is None or user.id != self.bot.user.id:
                count += 1
        return count

    async def _count_votes(self, channel, message_id: int) -> tuple[int, int] | None:
        try:
            message = await channel.fetch_message(message_id)
            yes_count = 0
            no_count = 0
            for reaction in message.reactions:
                if str(reaction.emoji) == VOTE_EMOJI_YES:
                    yes_count = await self._human_reactions(reaction)
                elif str(reaction.emoji) == VOTE_EMOJI_NO:
                    no_count = await self._human_reactions(reaction)
            return yes_count, no_count
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _vote_passed(self, row, yes_count: int, no_count: int) -> bool:
        return yes_count >= row["threshold"] and yes_count > no_count

    async def _close_vote_message(self, channel, message_id: int, passed: bool, footer: str | None = None):
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        if not message.embeds:
            return
        embed = message.embeds[0]
        if footer is None:
            footer = "Решение принято." if passed else "Голосование завершено: голосов не хватило."
        embed.set_footer(text=footer)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            log.exception("Не удалось обновить сообщение голосования %s", message_id)

    async def _resolve_vote(self, row, *, only_if_passed: bool):
        """Закрывает голосование, если набран порог или истекло время.

        only_if_passed=True — реакция: закрываем только успешное голосование, не дожидаясь таймера.
        """
        vote_id = row["id"]
        if vote_id in self._resolving_votes:
            return

        guild = self.bot.get_guild(row["guild_id"])
        channel = self._vote_channel(guild, row["channel_id"]) if guild else None
        counts = await self._count_votes(channel, row["message_id"]) if channel else None
        yes_count, no_count = counts if counts else (0, 0)
        passed = counts is not None and self._vote_passed(row, yes_count, no_count)
        timed_out = datetime.fromisoformat(row["ends_at"]) <= datetime.now(timezone.utc)

        if counts is None and not timed_out:
            return
        if only_if_passed and not passed:
            return
        if not passed and not timed_out:
            return

        if vote_id in self._resolving_votes:
            return
        self._resolving_votes.add(vote_id)
        try:
            fresh = await self.bot.db.fetchone("SELECT status FROM shame_votes WHERE id = ?", (vote_id,))
            if not fresh or fresh["status"] != "active":
                return

            # Повторный подсчёт уже под блокировкой: между первым подсчётом и захватом
            # голоса могли измениться.
            counts = await self._count_votes(channel, row["message_id"]) if channel else None
            yes_count, no_count = counts if counts else (0, 0)
            passed = counts is not None and self._vote_passed(row, yes_count, no_count)
            timed_out = datetime.fromisoformat(row["ends_at"]) <= datetime.now(timezone.utc)
            if counts is None and not timed_out:
                return
            if not passed and (only_if_passed or not timed_out):
                return

            member = await self._get_member(guild, row["target_user_id"]) if guild else None
            will_apply = passed and member is not None
            new_status = "passed" if will_apply else "failed"
            cur = await self.bot.db.execute(
                "UPDATE shame_votes SET status = ? WHERE id = ? AND status = 'active'",
                (new_status, vote_id),
            )
            if cur.rowcount != 1:
                return

            applied = will_apply
            if will_apply:
                try:
                    if row["vote_type"] == "give":
                        extended, expires_at = await self._apply_pozor(
                            guild, member, row["reason"], row["duration_hours"], row["initiator_id"]
                        )
                        text = "✅ Голосование прошло! " + self._give_text(
                            member, row["reason"], row["duration_hours"], extended, expires_at
                        )
                    else:
                        if await self._active_super(guild.id, member.id):
                            regular = await self._active_pozor(guild.id, member.id)
                            if regular:
                                await self._release_pozor(
                                    guild, member, f"снято голосованием: {row['reason']}", announce=False
                                )
                                text = (
                                    "✅ Обычный позор снят голосованием. "
                                    "Супер позор остаётся: его снимает только админ."
                                )
                            else:
                                applied = False
                                text = "❌ Это супер позор. Снять его может только админ."
                        else:
                            await self._release_pozor(
                                guild, member, f"снято голосованием: {row['reason']}", announce=False
                            )
                            text = f"✅ Голосование прошло! {random.choice(FUNNY_RELEASE_MESSAGES).format(mention=member.mention)}"
                except Exception:
                    log.exception("Не удалось применить результат голосования %s", vote_id)
                    await self.bot.db.execute(
                        "UPDATE shame_votes SET status = 'active' WHERE id = ?", (vote_id,)
                    )
                    return
            elif passed and member is None:
                text = "❌ Голосование набрало голоса, но участник не найден на сервере."
            else:
                text = f"❌ Голосование не набрало нужного числа голосов ({yes_count}/{row['threshold']})."

            if channel:
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    log.exception("Не удалось отправить итог голосования %s", vote_id)
                await self._close_vote_message(channel, row["message_id"], applied)
        finally:
            self._resolving_votes.discard(vote_id)

    # ---------- /pozor give ----------
    @app_commands.command(name="give", description="Выдать позор участнику напрямую")
    @app_commands.rename(member="участник", duration_hours="часы", reason="причина")
    @app_commands.describe(
        member="Кого опозорить",
        duration_hours="На сколько часов выдать позор",
        reason="Причина",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def give(self, interaction: discord.Interaction, member: discord.Member, duration_hours: float, reason: str):
        extended, expires_at = await self._apply_pozor(
            interaction.guild, member, reason, duration_hours, interaction.user.id
        )
        await interaction.response.send_message(self._give_text(member, reason, duration_hours, extended, expires_at))

    # ---------- /pozor super ----------
    @app_commands.command(name="super", description="Выдать супер позор: 10 минут без микрофона и ×100 в статистике")
    @app_commands.rename(member="участник", reason="причина")
    @app_commands.describe(member="Кого опозорить по-крупному", reason="Причина")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def super_pozor(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if member.bot:
            await interaction.response.send_message("Ботам супер позор не выдаём.", ephemeral=True)
            return
        already = await self._active_super(interaction.guild.id, member.id)
        shown_reason = reason if len(reason) <= 300 else reason[:297] + "..."
        mute_note = await self._apply_super(interaction.guild, member, reason, interaction.user.id)
        text = _fill(random.choice(FUNNY_SUPER_MESSAGES), mention=member.mention, reason=shown_reason)
        if already and mute_note is None:
            text += " Сверху ещё одна сотня в статистику, молчание заново на 10 минут."
        elif already:
            text += " Сверху ещё одна сотня в статистику."
        if mute_note:
            text += f" {mute_note}"
        await interaction.response.send_message(text)

    # ---------- /pozor remove ----------
    @app_commands.command(name="remove", description="Снять позор с участника напрямую")
    @app_commands.rename(member="участник", reason="причина")
    @app_commands.describe(member="С кого снять позор", reason="Почему снимаете")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, member: discord.Member, reason: str = "решение модератора"):
        super_row = await self._active_super(interaction.guild.id, member.id)
        if super_row and not await self._is_admin(interaction.user, interaction.guild):
            regular = await self._active_pozor(interaction.guild.id, member.id)
            if not regular:
                await interaction.response.send_message("Супер позор снимает только админ.", ephemeral=True)
                return
            await self._release_pozor(interaction.guild, member, reason, announce=False)
            text = random.choice(FUNNY_RELEASE_MESSAGES).format(mention=member.mention)
            await interaction.response.send_message(f"{text}\nСупер позор остаётся: его снимает только админ.")
            return
        if super_row:
            await self._release_super(interaction.guild, member, reason)
        await self._release_pozor(interaction.guild, member, reason, announce=False)
        text = random.choice(FUNNY_RELEASE_MESSAGES).format(mention=member.mention)
        if super_row:
            text = "Супер позор снят. " + text
        await interaction.response.send_message(text)

    # ---------- /pozor status ----------
    @app_commands.command(name="status", description="Проверить, сколько осталось позора у участника")
    @app_commands.rename(member="участник")
    @app_commands.describe(member="Чей позор проверить")
    async def status(self, interaction: discord.Interaction, member: discord.Member):
        regular = await self._active_pozor(interaction.guild.id, member.id)
        super_rows = await self.bot.db.fetchall(
            """SELECT * FROM shame_records
               WHERE guild_id = ? AND user_id = ? AND active = 1 AND is_super = 1
               ORDER BY given_at DESC""",
            (interaction.guild.id, member.id),
        )
        if not regular and not super_rows:
            await interaction.response.send_message(f"{member.mention} сейчас не опозорен(а).")
            return
        lines = [f"{member.mention}:"]
        if super_rows:
            super_row = super_rows[0]
            mute = _mute_left_minutes(super_row)
            reason = super_row["reason"] or "—"
            if mute is not None:
                silence = f"без микрофона ещё {_fmt_minutes(mute)} мин, слушать можно"
            else:
                silence = "микрофон уже можно включать"
            weight = SUPER_WEIGHT * len(super_rows)
            lines.append(f"Супер позор (×{weight}): {silence}. Снять может только админ. Причина: {reason}")
        if regular:
            expires = datetime.fromisoformat(regular["expires_at"])
            hours_left = max((expires - datetime.now(timezone.utc)).total_seconds() / 3600, 0)
            lines.append(f"Обычный позор ещё {hours_left:.1f} ч. Причина: {regular['reason']}")
        await interaction.response.send_message("\n".join(lines))

    # ---------- /pozor list ----------
    @app_commands.command(name="list", description="Список тех, кто сейчас опозорен")
    async def list_pozor(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shame_records WHERE guild_id = ? AND active = 1", (interaction.guild.id,)
        )
        if not rows:
            await interaction.response.send_message("Сейчас никто не в позоре. Скучно тут у вас.")
            return
        lines = []
        for row in rows:
            reason = row["reason"] or "—"
            if len(reason) > 80:
                reason = reason[:77] + "..."
            if _is_super(row):
                mute = _mute_left_minutes(row)
                silence = f", без микрофона ещё {_fmt_minutes(mute)} мин" if mute is not None else ""
                lines.append(f"<@{row['user_id']}> — супер позор (×{SUPER_WEIGHT}){silence} ({reason})")
                continue
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                hours_left = max((expires - datetime.now(timezone.utc)).total_seconds() / 3600, 0)
                left = f"{hours_left:.1f} ч. осталось"
            except (TypeError, ValueError):
                left = "срок не указан"
            lines.append(f"<@{row['user_id']}> — {left} ({reason})")
        await interaction.response.send_message("\n".join(lines))

    # ---------- /pozor history ----------
    @app_commands.command(name="history", description="История позора участника: сроки, причины и кто выдавал")
    @app_commands.rename(member="участник")
    @app_commands.describe(member="Чью историю показать")
    async def history(self, interaction: discord.Interaction, member: discord.Member):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shame_records WHERE guild_id = ? AND user_id = ? ORDER BY given_at DESC",
            (interaction.guild.id, member.id),
        )
        if not rows:
            await interaction.response.send_message(f"{member.mention} в позоре ещё не бывал(а). Либо очень хитрый(ая).")
            return

        shown = rows[:10]
        lines = []
        for row in shown:
            giver = f"<@{row['given_by']}>" if row["given_by"] else "неизвестно"
            reason = row["reason"] or "—"
            if len(reason) > 180:
                reason = reason[:177] + "..."
            if row["active"] and _is_super(row):
                mute = _mute_left_minutes(row)
                if mute is not None:
                    state = f"сейчас, без микрофона ещё {_fmt_minutes(mute)} мин. Снять может только админ."
                else:
                    state = "сейчас. Микрофон уже можно включать, снять метку может только админ."
            elif row["active"]:
                try:
                    left = max(
                        (datetime.fromisoformat(row["expires_at"]) - datetime.now(timezone.utc)).total_seconds() / 3600,
                        0,
                    )
                    state = f"сейчас, ещё {_fmt_hours(left)} ч."
                except (TypeError, ValueError):
                    state = "сейчас"
            elif row["removed_at"]:
                state = f"снят {_fmt_dt(row['removed_at'])} ({row['removed_reason'] or 'без причины'})"
            else:
                state = f"до {_fmt_dt(row['expires_at'])}"
            kind = f"супер позор (×{SUPER_WEIGHT})" if _is_super(row) else "позор"
            lines.append(
                f"**{_fmt_dt(row['given_at'])}** — {kind}, {state}, {_fmt_hours(_record_hours(row))} ч.\n"
                f"Причина: {reason}\nВыдал: {giver}"
            )

        embed = discord.Embed(
            title=f"Досье позора: {member.display_name}",
            description="\n\n".join(lines),
            colour=discord.Colour.dark_grey(),
        )
        embed.set_footer(text=f"Записей: {len(rows)}. Показаны последние {len(shown)}.")
        await interaction.response.send_message(embed=embed)

    # ---------- /pozor top ----------
    @app_commands.command(name="top", description="Кто чаще и дольше всех был в позоре")
    @app_commands.rename(limit="лимит")
    @app_commands.describe(limit="Сколько строк показать")
    async def top(self, interaction: discord.Interaction, limit: int = 10):
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shame_records WHERE guild_id = ?", (interaction.guild.id,)
        )
        if not rows:
            await interaction.response.send_message("Позора на сервере ещё не случалось. Подозрительно тихо.")
            return

        limit = max(1, min(limit, 15))
        stats: dict[int, list] = {}
        for row in rows:
            bucket = stats.setdefault(row["user_id"], [0, 0.0, False])
            bucket[0] += _shame_weight(row)
            bucket[1] += _record_hours(row)
            bucket[2] = bucket[2] or bool(row["active"])

        ranked = sorted(stats.items(), key=lambda item: (item[1][0], item[1][1]), reverse=True)[:limit]
        lines = []
        for index, (user_id, (count, hours, active)) in enumerate(ranked, start=1):
            mark = " · сейчас в позоре" if active else ""
            lines.append(f"**{index}.** <@{user_id}> — {_times_label(count)}, {_fmt_hours(hours)} ч.{mark}")

        embed = discord.Embed(
            title="Топ позора",
            description="\n".join(lines),
            colour=discord.Colour.dark_grey(),
        )
        embed.set_footer(text="Супер позор считается как 100 обычных. Часы — срок до конца или до снятия.")
        await interaction.response.send_message(embed=embed)

    # ---------- /pozor vote_give ----------
    @app_commands.command(name="vote_give", description="Запустить голосование за выдачу позора")
    @app_commands.rename(member="участник", reason="причина", duration_hours="часы")
    @app_commands.describe(
        member="За кого голосуем",
        reason="За что",
        duration_hours="На сколько часов будет позор, если голосование пройдёт",
    )
    async def vote_give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
        duration_hours: float = 24.0,
    ):
        await self._start_vote(interaction, member, reason, "give", duration_hours)

    # ---------- /pozor vote_remove ----------
    @app_commands.command(name="vote_remove", description="Запустить голосование за снятие позора (за заслуги)")
    @app_commands.rename(member="участник", reason="причина")
    @app_commands.describe(member="С кого предлагаете снять позор", reason="За какие заслуги")
    async def vote_remove(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        await self._start_vote(interaction, member, reason, "remove", 0)

    async def _start_vote(self, interaction, member, reason, vote_type, duration_hours):
        existing_vote = await self._active_vote(interaction.guild.id, member.id)
        if existing_vote:
            jump = (
                f"https://discord.com/channels/{existing_vote['guild_id']}/"
                f"{existing_vote['channel_id']}/{existing_vote['message_id']}"
            )
            await interaction.response.send_message(
                f"По {member.mention} уже идёт голосование: {jump}\n"
                "Отменить его может автор или модератор командой /pozor vote_cancel.",
                ephemeral=True,
            )
            return

        settings = await self._get_settings(interaction.guild.id)
        if vote_type == "remove" and await self._active_super(interaction.guild.id, member.id):
            await interaction.response.send_message(
                f"У {member.mention} супер позор. Снять его может только админ, голосование тут не поможет.",
                ephemeral=True,
            )
            return
        active = await self._active_pozor(interaction.guild.id, member.id) if vote_type == "give" else None
        if vote_type == "give" and not active:
            left = await self._cooldown_left(interaction.guild.id, member.id, settings)
            if left is not None:
                await interaction.response.send_message(
                    f"{member.mention} недавно вышел(шла) из позора. "
                    f"Новое голосование можно через {_fmt_hours(left)} ч. "
                    "Прямую выдачу модератор по-прежнему делает через /pozor give.",
                    ephemeral=True,
                )
                return

        window_hours = settings["vote_duration_hours"] or 1
        threshold = settings["vote_threshold"] or 3

        action_text = "выдать 🫡 позор" if vote_type == "give" else "снять 🧼 позор (за заслуги)"
        extend_note = ""
        if vote_type == "give" and active:
            extend_note = "\nУ участника уже есть позор: если голосование пройдёт, срок продлится, а не начнётся заново."
        embed = discord.Embed(
            title=f"Голосование: {action_text}",
            description=(
                f"Участник: {member.mention}\nПричина: {reason}\n\n"
                f"Голосуйте {VOTE_EMOJI_YES} за / {VOTE_EMOJI_NO} против.\n"
                f"Решение принимается сразу, когда голосов «за» не меньше {threshold} "
                f"и их больше, чем «против».\n"
                f"Если порог не набран, голосование закроется через {window_hours} ч."
                f"{extend_note}"
            ),
            colour=discord.Colour.orange(),
        )
        embed.set_footer(text=f"Порог: {threshold} «за». Крайний срок: {window_hours} ч.")
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

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
                duration_hours,
                interaction.user.id,
                datetime.now(timezone.utc).isoformat(),
                ends_at.isoformat(),
                threshold,
            ),
        )
        await message.add_reaction(VOTE_EMOJI_YES)
        await message.add_reaction(VOTE_EMOJI_NO)

    # ---------- /pozor vote_cancel ----------
    @app_commands.command(name="vote_cancel", description="Отменить активное голосование по участнику")
    @app_commands.rename(member="участник")
    @app_commands.describe(member="По кому отменить голосование")
    async def vote_cancel(self, interaction: discord.Interaction, member: discord.Member):
        row = await self._active_vote(interaction.guild.id, member.id)
        if not row:
            await interaction.response.send_message(f"По {member.mention} нет активного голосования.", ephemeral=True)
            return
        is_mod = interaction.user.guild_permissions.manage_roles
        if interaction.user.id != row["initiator_id"] and not is_mod:
            await interaction.response.send_message(
                "Отменить голосование может его автор или тот, у кого есть право управлять ролями.",
                ephemeral=True,
            )
            return

        cur = await self.bot.db.execute(
            "UPDATE shame_votes SET status = 'cancelled' WHERE id = ? AND status = 'active'",
            (row["id"],),
        )
        if cur.rowcount != 1:
            await interaction.response.send_message("Это голосование уже завершилось.", ephemeral=True)
            return

        channel = self._vote_channel(interaction.guild, row["channel_id"])
        if channel:
            await self._close_vote_message(channel, row["message_id"], False, footer="Голосование отменено.")
            if interaction.channel_id != channel.id:
                try:
                    await channel.send(f"Голосование по {member.mention} отменено.")
                except discord.HTTPException:
                    log.exception("Не удалось написать в канал голосования %s", channel.id)
        await interaction.response.send_message(f"Голосование по {member.mention} отменено.")

    # ---------- /pozor setup ----------
    @app_commands.command(name="setup", description="Настроить систему позора для сервера")
    @app_commands.rename(
        role="роль",
        vote_threshold="порог",
        vote_window_hours="окно",
        log_channel="канал",
        cooldown_hours="кулдаун",
    )
    @app_commands.describe(
        role="Роль позора (если не указана — будет создана автоматически)",
        vote_threshold="Сколько голосов «за» нужно для принятия решения",
        vote_window_hours="Сколько часов длится голосование, если порог не набран раньше",
        log_channel="Канал для объявлений об окончании позора",
        cooldown_hours="Сколько часов после снятия нельзя снова запускать голосование за позор",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_pozor(
        self,
        interaction: discord.Interaction,
        role: discord.Role = None,
        vote_threshold: int = 3,
        vote_window_hours: float = 24.0,
        log_channel: discord.TextChannel = None,
        cooldown_hours: float = None,
    ):
        settings = await self._get_settings(interaction.guild.id)
        role_id = role.id if role else (await self._get_pozor_role(interaction.guild)).id
        new_cooldown = self._cooldown_hours(settings) if cooldown_hours is None else max(cooldown_hours, 0)
        await self.bot.db.execute(
            """UPDATE guild_settings
               SET shame_role_id = ?, vote_threshold = ?, vote_duration_hours = ?, log_channel_id = ?, pozor_cooldown_hours = ?
               WHERE guild_id = ?""",
            (
                role_id,
                vote_threshold,
                vote_window_hours,
                log_channel.id if log_channel else interaction.channel.id,
                new_cooldown,
                interaction.guild.id,
            ),
        )
        cooldown_text = "выключен" if new_cooldown <= 0 else f"{_fmt_hours(new_cooldown)} ч. после снятия"
        await interaction.response.send_message(
            f"Настроено: роль <@&{role_id}>, порог голосов: {vote_threshold}, окно голосования: {vote_window_hours} ч. "
            f"При наборе порога решение выполняется сразу. Кулдаун нового голосования: {cooldown_text}."
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Пока идёт супер позор, человек остаётся в канале, слышит, но не говорит."""
        if member.bot or after.channel is None:
            return
        key = (member.guild.id, member.id)
        until = self._muted_until.get(key)
        if until is not None and datetime.now(timezone.utc) < until:
            if not after.mute:
                await self._server_mute(member, True, "Супер позор")
            return
        if key not in self._unmute_later or not after.mute:
            return
        if await self._server_mute(member, False, "Супер позор: микрофон можно снова включать"):
            await self._clear_bot_muted(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Текстовый чат тоже молчит 10 минут. Голосовой канал при этом не трогаем."""
        if message.guild is None or message.author.bot:
            return
        until = self._muted_until.get((message.guild.id, message.author.id))
        if until is None or datetime.now(timezone.utc) >= until:
            return
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) not in (VOTE_EMOJI_YES, VOTE_EMOJI_NO):
            return
        if payload.guild_id is None:
            return
        row = await self.bot.db.fetchone(
            "SELECT * FROM shame_votes WHERE message_id = ? AND status = 'active'",
            (payload.message_id,),
        )
        if not row:
            return
        await self._resolve_vote(row, only_if_passed=True)

    # ---------- фоновые задачи ----------
    @tasks.loop(minutes=1)
    async def check_expired_pozor(self):
        rows = await self.bot.db.fetchall(
            """SELECT * FROM shame_records
               WHERE active = 1 AND COALESCE(is_super, 0) = 0 AND expires_at IS NOT NULL AND expires_at <= ?""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            member = await self._get_member(guild, row["user_id"])
            if member:
                await self._release_pozor(guild, member, "срок истёк", announce=True)
            else:
                await self.bot.db.execute(
                    "UPDATE shame_records SET active = 0, removed_reason = ?, removed_at = ? WHERE id = ?",
                    ("срок истёк, участник не на сервере", datetime.now(timezone.utc).isoformat(), row["id"]),
                )
        await self._lift_expired_voice_mutes()

    @tasks.loop(minutes=1)
    async def check_votes(self):
        """Страховка: порог, набранный пока бот был offline, и голосования с истёкшим сроком."""
        rows = await self.bot.db.fetchall("SELECT * FROM shame_votes WHERE status = 'active'")
        for row in rows:
            await self._resolve_vote(row, only_if_passed=False)

    @check_expired_pozor.before_loop
    async def before_check_expired_pozor(self):
        await self.bot.wait_until_ready()
        await self._lift_expired_voice_mutes()

    @check_votes.before_loop
    async def before_check_votes(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(PozorCog(bot))
