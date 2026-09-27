import asyncio
import json
import logging
import sqlite3
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils.dota import (
    OFFER_WINDOW_SECONDS,
    DotaUserError,
    MatchNotReady,
    OpenDota,
    OpenDotaError,
    PlayerNotInMatch,
    build_brief,
    find_match_id,
    offer_footer,
    parse_account_argument,
    parse_match_argument,
    parse_offer_footer,
    parse_player_ref,
    rank_name,
    teaser,
)
from utils.settings import get_settings

log = logging.getLogger("cogs.dota")

MODEL_TITLES = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
}
MODEL_CHOICES = [
    app_commands.Choice(name="ChatGPT", value="chatgpt"),
    app_commands.Choice(name="Claude", value="claude"),
    app_commands.Choice(name="Gemini", value="gemini"),
]
WATCH_CHOICES = [
    app_commands.Choice(name="следить за катками", value=1),
    app_commands.Choice(name="заткнись", value=0),
]
ROAST_COOLDOWN_SECONDS = 20

COACH_PROMPT = """Ты — токсичный тренер по Dota 2. Тебя зовут Тренер. Говоришь по-русски, на «ты», зло и конкретно, как игрок 7к, которому скинули реплей паба.

Метод разбора важнее шуток:
- Не пересказывай таблицу. Цифра нужна, только чтобы ткнуть в смысл.
- Ищи паттерн, а не одиночный ивент: повторные соло-смерти, один и тот же убийца, просадка золота, поздний предмет.
- Суди по роли. Строка «Как судить роль» обязательна. Низкое участие керри в драках до тайминга — не ошибка. Низкий нетворс саппорта — не ошибка. Смерти оффлейнера могут быть нормальными, если вражеский керри при этом не нафармился.
- Найди момент, где игра сломалась: перевес золота, серия смертей, драка без байбека, предмет позже тайминга, объектив после проигранной драки.
- Проигранная линия — смотри, перестроился ли игрок, а не только то, что линия красная.
- Смерти дели на неизбежные, свою вину, допустимый трейд и слив. В ответ вынеси только то, что меняет вывод.
- Каждая претензия кончается тем, что делать в следующей катке.

Запрещено:
- Выдумывать цифры, предметы, руны, варды и драки, которых нет во входных данных. Нет данных — так и скажи.
- Оскорблять национальность, пол, ориентацию, внешность, болезни, возраст и семью. Токсичность только про игру.
- Растягивать текст. Максимум 1500 символов.
- Извиняться, хвалить «в целом неплохо» и смягчать вердикт в конце.

Если первая строка «РЕЖИМ: вся катка», разбирай игру целиком и называй героев.

Формат:
**Вердикт.** Одна злая фраза.
**Где сломалось.** 2–4 предложения с цифрами из данных.
**Косяки.** Три пункта, в каждом цифра.
**Следующая катка.** Три коротких приказа.
"""

EXPOSE_HINT = (
    "Если каток не видно, в Dota 2 включи Settings → Social → Expose Public Match Data "
    "и подожди, пока OpenDota подтянет историю."
)


def _model_id(key: str) -> str:
    return {
        "chatgpt": config.CHATGPT_MODEL,
        "claude": config.CLAUDE_MODEL,
        "gemini": config.GEMINI_MODEL,
    }[key]


def _public_error(exc: Exception) -> str:
    if isinstance(exc, DotaUserError):
        return str(exc)
    if isinstance(exc, OpenDotaError):
        if exc.status == 404:
            return "OpenDota не нашёл игрока или матч."
        if exc.status == 429:
            return "OpenDota просит подождать. Попробуй через минуту."
        return "OpenDota сейчас не отвечает."
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "Что-то сломалось. Подробности в логе бота."


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text", "output_text"):
                parts.append(str(part.get("text") or ""))
        return "".join(parts).strip()
    return ""


def _split(text: str, limit: int = 1800) -> list[str]:
    rest = (text or "").strip()
    parts: list[str] = []
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return parts


def _pages(title: str, text: str, footer: str) -> list[discord.Embed]:
    embeds = []
    for index, chunk in enumerate(_split(text)[:4]):
        embed = discord.Embed(
            title=title if index == 0 else f"{title} ({index + 1})",
            description=chunk,
            colour=discord.Colour.dark_red(),
        )
        if index == 0:
            embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


class RoastView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Разобрать катку", style=discord.ButtonStyle.danger, custom_id="dota:roast")
    async def roast(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("DotaCog")
        if not isinstance(cog, DotaCog):
            await interaction.response.send_message("Тренер спит.", ephemeral=True)
            return
        await cog.deliver_roast(interaction)

    @discord.ui.button(label="Не надо", style=discord.ButtonStyle.secondary, custom_id="dota:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ладно. С этим позором живи сам.", view=None)


class DotaCog(commands.GroupCog, name="dota", description="Токсичный тренер и слежка за катками"):
    """Разбор в духе mcp-replay-dota2, голос — злой тренер, модель — уже подключённая."""

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.api: OpenDota | None = None
        self._last_roast: dict[int, float] = {}
        self._sync_lock = asyncio.Lock()

    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
        self.api = OpenDota(self.session, config.OPENDOTA_API_KEY)
        self.bot.add_view(RoastView())
        try:
            await self.api.load_constants()
        except Exception:
            log.exception("Справочник героев OpenDota не загрузился, разбор всё равно запустится")
        self.watch_matches.start()

    async def cog_unload(self):
        self.watch_matches.cancel()
        if self.session:
            await self.session.close()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Тренер работает только на сервере.", ephemeral=True)
            return False
        return True

    def _api(self) -> OpenDota:
        if self.api is None:
            raise DotaUserError("Тренер ещё поднимается. Повтори через пару секунд.")
        return self.api

    async def _ensure_names(self):
        api = self._api()
        if not api.names.heroes_by_id:
            await api.load_constants()

    def _model_key(self, settings, choice: app_commands.Choice[str] | None) -> str:
        if choice is not None:
            return choice.value
        stored = str(settings["dota_model"] or "").strip().lower()
        if stored in MODEL_TITLES:
            return stored
        return config.DOTA_COACH_MODEL

    async def _fail(self, interaction: discord.Interaction, exc: Exception):
        text = f"⚠️ {_public_error(exc)}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            log.exception("Не удалось отправить ошибку тренера")

    async def _player_row(self, guild_id: int, user_id: int):
        return await self.bot.db.fetchone(
            "SELECT * FROM dota_players WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def _account_from_ref(self, ref) -> int:
        if ref.account_id:
            return int(ref.account_id)
        if ref.vanity:
            return await self._api().resolve_vanity(ref.vanity)
        raise DotaUserError("Не понял, какой Steam.")

    async def _save_player(self, guild_id: int, user_id: int, account_id: int) -> tuple[str, bool]:
        """Возвращает (ник, пустая ли история)."""
        other = await self.bot.db.fetchone(
            "SELECT user_id FROM dota_players WHERE guild_id = ? AND account_id = ? AND user_id != ?",
            (guild_id, account_id, user_id),
        )
        if other:
            raise DotaUserError(f"Этот Steam уже привязан к <@{other['user_id']}>.")
        profile = await self._api().player(account_id)
        persona = str(((profile.get("profile") or {}).get("personaname") or "")).strip()
        try:
            recent = await self._api().recent_matches(account_id)
        except OpenDotaError:
            recent = []
        known = bool(persona or recent or (profile.get("profile") or {}).get("account_id"))
        if not known:
            raise DotaUserError("OpenDota не видит этот аккаунт. Проверь ссылку. " + EXPOSE_HINT)
        try:
            await self.bot.db.execute(
                """INSERT INTO dota_players (guild_id, user_id, account_id, persona, watch)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET
                     account_id = excluded.account_id,
                     persona = excluded.persona,
                     watch = 1,
                     linked_at = datetime('now')""",
                (guild_id, user_id, account_id, persona),
            )
        except sqlite3.IntegrityError as exc:
            raise DotaUserError("Этот Steam уже привязан к кому-то на сервере.") from exc
        await self.bot.db.execute(
            "DELETE FROM dota_seen_matches WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return persona or str(account_id), not recent

    async def _mark_seen(self, guild_id: int, user_id: int, match_id: int):
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO dota_seen_matches (guild_id, user_id, match_id) VALUES (?, ?, ?)",
            (guild_id, user_id, match_id),
        )

    async def _watch_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        settings = await get_settings(self.bot.db, guild.id)
        channel_id = settings["dota_channel_id"]
        if not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except discord.HTTPException:
                return None
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def _cooling_down(self, user_id: int) -> bool:
        now = time.monotonic()
        if now - self._last_roast.get(user_id, 0.0) < ROAST_COOLDOWN_SECONDS:
            return True
        self._last_roast[user_id] = now
        return False

    async def _sync_recent(self, guild: discord.Guild, user_id: int, account_id: int) -> bool:
        """Помечает старые катки просмотренными и предлагает одну свежую. True, если сообщение ушло."""
        async with self._sync_lock:
            return await self._sync_recent_unlocked(guild, user_id, account_id)

    async def _sync_recent_unlocked(self, guild: discord.Guild, user_id: int, account_id: int) -> bool:
        channel = await self._watch_channel(guild)
        if channel is None:
            return False
        recent = await self._api().recent_matches(account_id)
        seen_rows = await self.bot.db.fetchall(
            "SELECT match_id FROM dota_seen_matches WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        seen = {row["match_id"] for row in seen_rows}
        unseen = [match for match in recent if match.get("match_id") and match["match_id"] not in seen]
        offer = None
        if channel is not None and unseen:
            newest = unseen[0]
            start = int(newest.get("start_time") or 0)
            if start and time.time() - start <= OFFER_WINDOW_SECONDS:
                offer = newest
        for match in recent:
            match_id = match.get("match_id")
            if match_id and (offer is None or match_id != offer.get("match_id")):
                await self._mark_seen(guild.id, user_id, int(match_id))
        if offer is None or channel is None:
            return False
        try:
            await self._send_offer(channel, user_id, account_id, offer)
        except discord.HTTPException:
            log.exception("Не удалось предложить разбор матча %s", offer.get("match_id"))
            return False
        await self._mark_seen(guild.id, user_id, int(offer["match_id"]))
        return True

    async def _send_offer(self, channel: discord.TextChannel, user_id: int, account_id: int, match: dict):
        await self._ensure_names()
        hero = self._api().names.hero(match.get("hero_id"))
        won = None
        if match.get("radiant_win") is not None:
            won = (int(match.get("player_slot") or 0) < 128) == bool(match.get("radiant_win"))
        embed = discord.Embed(
            title="Новая катка уже пахнет",
            description=teaser(
                hero,
                won,
                match.get("duration"),
                match.get("kills"),
                match.get("deaths"),
                match.get("assists"),
                match.get("lobby_type"),
                match.get("game_mode"),
                match.get("leaver_status"),
            ),
            colour=discord.Colour.orange(),
        )
        embed.set_footer(text=offer_footer(int(match["match_id"]), account_id, user_id))
        await channel.send(content=f"<@{user_id}>", embed=embed, view=RoastView())

    async def _complete(self, model_key: str, brief: str) -> str:
        if not config.NORDROUTER_API_KEY:
            raise DotaUserError("NORDROUTER_API_KEY не настроен, тренеру нечем думать.")
        if self.session is None:
            raise DotaUserError("Тренер ещё поднимается. Повтори через пару секунд.")
        payload = {
            "model": _model_id(model_key),
            "messages": [
                {"role": "system", "content": COACH_PROMPT},
                {"role": "user", "content": "Разбери катку по этим данным.\n\n" + brief[:12000]},
            ],
            "max_tokens": 1400,
        }
        headers = {
            "Authorization": f"Bearer {config.NORDROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        url = f"{config.NORDROUTER_BASE_URL}/chat/completions"
        async with self.session.post(url, headers=headers, json=payload) as resp:
            raw = await resp.read()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                snippet = raw.decode("utf-8", errors="replace").strip()[:300]
                raise RuntimeError(snippet or f"NordRouter ответил {resp.status}.")
            if resp.status != 200:
                error = data.get("error") if isinstance(data, dict) else None
                if isinstance(error, dict) and error.get("message"):
                    raise RuntimeError(str(error["message"]))
                if isinstance(error, str) and error:
                    raise RuntimeError(error)
                raise RuntimeError(f"NordRouter ответил {resp.status}.")
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        answer = _message_text(message)
        if not answer:
            raise DotaUserError("Модель промолчала. Даже ей стыдно за эту катку.")
        return answer

    async def _analyze(self, match_id: int, account_id: int, model_key: str) -> str:
        await self._ensure_names()
        try:
            match = await self._api().match(match_id)
        except OpenDotaError as exc:
            raise DotaUserError(_public_error(exc)) from exc
        names = self._api().names
        try:
            brief = build_brief(match, account_id, names)
        except MatchNotReady as exc:
            await self._api().request_parse(match_id)
            raise DotaUserError(str(exc)) from exc
        except PlayerNotInMatch:
            brief = (
                "Заказанного аккаунта в матче нет: профиль скрыт или это чужая катка.\n"
                + build_brief(match, 0, names)
            )
        return await self._complete(model_key, brief)

    async def deliver_roast(self, interaction: discord.Interaction):
        footer = None
        if interaction.message and interaction.message.embeds:
            footer = interaction.message.embeds[0].footer.text
        parsed = parse_offer_footer(footer)
        if parsed is None or interaction.guild is None:
            await interaction.response.send_message("Не вижу, какую катку разбирать.", ephemeral=True)
            return
        match_id, account_id, user_id = parsed
        if self._cooling_down(interaction.user.id):
            await interaction.response.send_message("Подожди немного. Тренер ещё орёт прошлую катку.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            settings = await get_settings(self.bot.db, interaction.guild.id)
            model_key = self._model_key(settings, None)
            text = await self._analyze(match_id, account_id, model_key)
        except Exception as exc:
            log.exception("Разбор матча %s не удался", match_id)
            await self._fail(interaction, exc)
            return
        embeds = _pages("Тренер посмотрел реплей", text, f"{MODEL_TITLES.get(model_key, model_key)} · OpenDota")
        await interaction.followup.send(
            content=f"<@{user_id}>\nhttps://www.opendota.com/matches/{match_id}",
            embeds=embeds,
        )

    @tasks.loop(minutes=config.DOTA_POLL_MINUTES)
    async def watch_matches(self):
        try:
            rows = await self.bot.db.fetchall(
                "SELECT guild_id, user_id, account_id FROM dota_players WHERE watch = 1"
            )
        except Exception:
            log.exception("Не удалось прочитать список игроков Доты")
            return
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            try:
                await self._sync_recent(guild, row["user_id"], row["account_id"])
            except Exception:
                log.exception("Слежка за игроком %s не удалась", row["user_id"])
            await asyncio.sleep(1)

    @watch_matches.before_loop
    async def _before_watch(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not message.content:
            return
        ref = parse_player_ref(message.content)
        match_id = find_match_id(message.content)
        if ref is None and match_id is None:
            return
        channel = await self._watch_channel(message.guild)
        if channel is None or message.channel.id != channel.id:
            return
        if ref is not None:
            try:
                existing = await self._player_row(message.guild.id, message.author.id)
                account_id = await self._account_from_ref(ref)
                if existing and int(existing["account_id"]) == account_id:
                    await message.reply("Этот Steam уже привязан, я и так слежу.", mention_author=False)
                else:
                    persona, empty = await self._save_player(message.guild.id, message.author.id, account_id)
                    offered = await self._sync_recent(message.guild, message.author.id, account_id)
                    text = f"Принял **{persona}**. Дальше сам увижу новые катки."
                    if empty:
                        text += " " + EXPOSE_HINT
                    elif not offered:
                        text += " Свежей катки прямо сейчас нет."
                    await message.reply(text, mention_author=False)
            except Exception as exc:
                log.exception("Не удалось привязать Steam из канала")
                try:
                    await message.reply(f"⚠️ {_public_error(exc)}", mention_author=False)
                except discord.HTTPException:
                    log.exception("Не удалось ответить на сообщение в канале слежки")
        if match_id is not None:
            row = await self._player_row(message.guild.id, message.author.id)
            account_id = int(row["account_id"]) if row else 0
            embed = discord.Embed(
                title="Ссылку увидел. Разбор тоже можно.",
                description="Нажми кнопку — тренер посмотрит эту катку и не будет жалеть.",
                colour=discord.Colour.orange(),
            )
            embed.set_footer(text=offer_footer(match_id, account_id, message.author.id))
            try:
                await message.reply(embed=embed, view=RoastView(), mention_author=False)
            except discord.HTTPException:
                log.exception("Не удалось предложить разбор ссылки %s", match_id)

    async def _harvest(self, channel: discord.TextChannel) -> int:
        found = 0
        vanity_left = 15
        seen_users: set[int] = set()
        async for message in channel.history(limit=200):
            if message.author.bot or message.author.id in seen_users:
                continue
            ref = parse_player_ref(message.content or "")
            if ref is None:
                continue
            if ref.vanity:
                if vanity_left <= 0:
                    continue
                vanity_left -= 1
            try:
                account_id = await self._account_from_ref(ref)
                await self._save_player(channel.guild.id, message.author.id, account_id)
                recent = await self._api().recent_matches(account_id)
                for match in recent:
                    if match.get("match_id"):
                        await self._mark_seen(channel.guild.id, message.author.id, int(match["match_id"]))
                seen_users.add(message.author.id)
                found += 1
            except Exception:
                log.exception("Не удалось забрать Steam из истории канала")
            await asyncio.sleep(0.5)
        return found

    @app_commands.command(name="link", description="Привязать Steam и начать слежку за катками")
    @app_commands.rename(steam="стим")
    @app_commands.describe(steam="Ссылка на Steam, Dotabuff, OpenDota, Stratz или числовой ID")
    async def link(self, interaction: discord.Interaction, steam: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ref = parse_account_argument(steam)
            account_id = await self._account_from_ref(ref)
            existing = await self._player_row(interaction.guild.id, interaction.user.id)
            if existing and int(existing["account_id"]) == account_id:
                await interaction.followup.send("Этот Steam уже привязан.", ephemeral=True)
                return
            persona, empty = await self._save_player(interaction.guild.id, interaction.user.id, account_id)
            offered = await self._sync_recent(interaction.guild, interaction.user.id, account_id)
            channel = await self._watch_channel(interaction.guild)
            text = f"Привязал **{persona}**. Тренер токсичный специально, это не баг."
            if channel is None:
                text += " Канал слежки ещё не выбран: админ ставит его через /dota setup. Пока можно /dota last."
            elif offered:
                text += f" Свежую катку уже предложил в {channel.mention}."
            else:
                text += f" Новые катки буду предлагать в {channel.mention}."
            if empty:
                text += " " + EXPOSE_HINT
            text += " Выключить слежку: /dota watch."
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:
            log.exception("Не удалось привязать Steam")
            await self._fail(interaction, exc)

    @app_commands.command(name="unlink", description="Отвязать Steam и перестать следить за катками")
    async def unlink(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "DELETE FROM dota_players WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, interaction.user.id),
        )
        await self.bot.db.execute(
            "DELETE FROM dota_seen_matches WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, interaction.user.id),
        )
        await interaction.response.send_message("Отвязал. Тренер сделает вид, что тебя не знает.", ephemeral=True)

    @app_commands.command(name="me", description="Показать привязанный Steam, ранг и слежку")
    async def me(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self._player_row(interaction.guild.id, interaction.user.id)
            if row is None:
                await interaction.followup.send("Steam не привязан. Команда: /dota link.", ephemeral=True)
                return
            profile = await self._api().player(int(row["account_id"]))
            rank = rank_name(profile.get("rank_tier"))
            persona = ((profile.get("profile") or {}).get("personaname")) or row["persona"] or row["account_id"]
            watch = "слежу" if row["watch"] else "молчу"
            channel = await self._watch_channel(interaction.guild)
            where = channel.mention if channel else "канал не выбран"
            await interaction.followup.send(
                f"**{persona}** · {rank}\n"
                f"https://www.opendota.com/players/{row['account_id']}\n"
                f"Слежка: {watch}. Пишу в {where}.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Не удалось показать профиль")
            await self._fail(interaction, exc)

    @app_commands.command(name="last", description="Токсичный разбор последней катки")
    @app_commands.rename(user="игрок", model="модель")
    @app_commands.describe(user="Кого разбирать. По умолчанию — ты", model="Какой моделью орать")
    @app_commands.choices(model=MODEL_CHOICES)
    async def last(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        model: app_commands.Choice[str] | None = None,
    ):
        target = user or interaction.user
        row = await self._player_row(interaction.guild.id, target.id)
        if row is None:
            who = "У него" if user else "У тебя"
            await interaction.response.send_message(f"⚠️ {who} не привязан Steam. Сначала /dota link.", ephemeral=True)
            return
        if self._cooling_down(interaction.user.id):
            await interaction.response.send_message("Подожди немного. Тренер ещё орёт прошлую катку.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            recent = await self._api().recent_matches(int(row["account_id"]))
            if not recent:
                raise DotaUserError("Свежих каток не видно. " + EXPOSE_HINT)
            settings = await get_settings(self.bot.db, interaction.guild.id)
            model_key = self._model_key(settings, model)
            match_id = int(recent[0]["match_id"])
            text = await self._analyze(match_id, int(row["account_id"]), model_key)
            embeds = _pages(
                f"Тренер разбирает {target.display_name}",
                text,
                f"{MODEL_TITLES.get(model_key, model_key)} · OpenDota",
            )
            await interaction.followup.send(
                content=f"{target.mention}\nhttps://www.opendota.com/matches/{match_id}",
                embeds=embeds,
            )
        except Exception as exc:
            log.exception("Разбор последней катки не удался")
            await self._fail(interaction, exc)

    @app_commands.command(name="match", description="Токсичный разбор конкретного матча")
    @app_commands.rename(match="матч", user="игрок", model="модель")
    @app_commands.describe(
        match="ID матча или ссылка OpenDota, Dotabuff, Stratz",
        user="Чью игру внутри матча разбирать",
        model="Какой моделью орать",
    )
    @app_commands.choices(model=MODEL_CHOICES)
    async def match_cmd(
        self,
        interaction: discord.Interaction,
        match: str,
        user: discord.Member | None = None,
        model: app_commands.Choice[str] | None = None,
    ):
        try:
            match_id = parse_match_argument(match)
        except DotaUserError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        target = user or interaction.user
        row = await self._player_row(interaction.guild.id, target.id)
        if user is not None and row is None:
            await interaction.response.send_message("⚠️ У него Steam не привязан.", ephemeral=True)
            return
        if self._cooling_down(interaction.user.id):
            await interaction.response.send_message("Подожди немного. Тренер ещё орёт прошлую катку.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            account_id = int(row["account_id"]) if row else 0
            settings = await get_settings(self.bot.db, interaction.guild.id)
            model_key = self._model_key(settings, model)
            text = await self._analyze(match_id, account_id, model_key)
            embeds = _pages(
                "Тренер посмотрел реплей",
                text,
                f"{MODEL_TITLES.get(model_key, model_key)} · OpenDota",
            )
            await interaction.followup.send(
                content=f"{target.mention}\nhttps://www.opendota.com/matches/{match_id}",
                embeds=embeds,
            )
        except Exception as exc:
            log.exception("Разбор матча не удался")
            await self._fail(interaction, exc)

    @app_commands.command(name="watch", description="Включить или выключить сообщения о новых катках")
    @app_commands.rename(mode="режим")
    @app_commands.describe(mode="Следить за катками или заткнуться")
    @app_commands.choices(mode=WATCH_CHOICES)
    async def watch(self, interaction: discord.Interaction, mode: app_commands.Choice[int]):
        row = await self._player_row(interaction.guild.id, interaction.user.id)
        if row is None:
            await interaction.response.send_message("Сначала привяжи Steam: /dota link.", ephemeral=True)
            return
        await self.bot.db.execute(
            "UPDATE dota_players SET watch = ? WHERE guild_id = ? AND user_id = ?",
            (mode.value, interaction.guild.id, interaction.user.id),
        )
        text = "Слежу. Новая катка — и я приду." if mode.value else "Замолчал. Сам позовёшь через /dota last."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="players", description="Кто привязал Steam и за кем тренер следит")
    async def players(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT user_id, account_id, persona, watch FROM dota_players WHERE guild_id = ? ORDER BY persona",
            (interaction.guild.id,),
        )
        if not rows:
            await interaction.response.send_message("Пока никто не привязал Steam.", ephemeral=True)
            return
        lines = []
        for row in rows[:30]:
            state = "слежу" if row["watch"] else "молчу"
            name = row["persona"] or row["account_id"]
            lines.append(f"<@{row['user_id']}> — {name} — {state}")
        extra = f"\n…и ещё {len(rows) - 30}" if len(rows) > 30 else ""
        embed = discord.Embed(title="Подопечные", description="\n".join(lines) + extra, colour=discord.Colour.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="setup", description="Канал слежки за катками и модель тренера")
    @app_commands.rename(channel="канал", model="модель")
    @app_commands.describe(
        channel="Куда предлагать разбор новых каток и откуда забирать ссылки на Steam",
        model="Какой моделью разбирать, если в команде не выбрали другую",
    )
    @app_commands.choices(model=MODEL_CHOICES)
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        model: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer(thinking=True)
        try:
            me = interaction.guild.me
            if me is not None:
                perms = channel.permissions_for(me)
                if not perms.send_messages or not perms.embed_links:
                    raise DotaUserError("В этом канале мне нужны права писать сообщения и встраивать ссылки.")
            settings = await get_settings(self.bot.db, interaction.guild.id)
            model_key = model.value if model is not None else self._model_key(settings, None)
            await self.bot.db.execute(
                "UPDATE guild_settings SET dota_channel_id = ?, dota_model = ? WHERE guild_id = ?",
                (channel.id, model_key, interaction.guild.id),
            )
            found = 0
            try:
                if me is None or channel.permissions_for(me).read_message_history:
                    found = await self._harvest(channel)
            except discord.HTTPException:
                log.exception("Не удалось прочитать историю канала слежки")
            await interaction.followup.send(
                f"Тренер сидит в {channel.mention} и орёт голосом {MODEL_TITLES.get(model_key, model_key)}.\n"
                f"Из истории канала забрал профилей: {found}. "
                "Новые катки этих людей буду предлагать кнопкой, а не простынёй. "
                "Ссылку на Steam или матч можно кидать прямо туда."
            )
        except Exception as exc:
            log.exception("Не удалось настроить тренера")
            await self._fail(interaction, exc)


async def setup(bot: commands.Bot):
    await bot.add_cog(DotaCog(bot))
