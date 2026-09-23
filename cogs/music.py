import asyncio
import logging
import shutil
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cogs.music")

MAX_QUEUE = 30
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

YDL_OPTS = {
    "format": "bestaudio[acodec!=none]/bestaudio/best",
    "noplaylist": True,
    "playlist_items": "1",
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "skip_download": True,
    "socket_timeout": 20,
    "source_address": "0.0.0.0",
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}


@dataclass
class Track:
    title: str
    stream_url: str
    webpage_url: str
    duration: int | None
    requester_id: int


class Player:
    def __init__(self):
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.volume: float = 0.4
        self.generation = 0
        self.lock = asyncio.Lock()


def _is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _safe_title(title: str) -> str:
    title = title.replace("`", "'")
    if len(title) > 80:
        return title[:77] + "..."
    return title


def _stream_url(info: dict) -> str | None:
    formats = info.get("formats") or []
    audio = [
        item
        for item in formats
        if item.get("url") and item.get("acodec") not in (None, "none") and item.get("vcodec") in (None, "none")
    ]
    if not audio:
        audio = [item for item in formats if item.get("url") and item.get("acodec") not in (None, "none")]
    if audio:
        audio.sort(key=lambda item: item.get("abr") or item.get("tbr") or 0, reverse=True)
        return audio[0]["url"]
    url = info.get("url")
    if url and info.get("_type") != "playlist":
        return url
    return None


def _extract(query: str) -> dict:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("Не установлен пакет yt-dlp. Пересоберите образ бота.") from exc

    search = query if _is_url(query) else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(search, download=False)
    if not info:
        raise RuntimeError("Ничего не найдено.")
    if info.get("entries") is not None:
        info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise RuntimeError("Ничего не найдено.")
    stream = _stream_url(info)
    if not stream:
        raise RuntimeError("У этого ролика нет доступного аудио.")
    return {
        "title": info.get("title") or "Без названия",
        "stream_url": stream,
        "webpage_url": info.get("webpage_url") or query,
        "duration": info.get("duration"),
    }


def _ensure_opus() -> None:
    if discord.opus.is_loaded():
        return
    discord.opus._load_default()
    if discord.opus.is_loaded():
        return
    for name in ("libopus.so.0", "libopus.so"):
        try:
            discord.opus.load_opus(name)
            return
        except OSError:
            continue
    raise RuntimeError("Не удалось загрузить кодек Opus. В Docker нужен пакет libopus0.")


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "Не найден FFmpeg. В Docker он ставится при сборке образа, на Windows его нужно установить и добавить в PATH."
        )


def _track_line(track: Track) -> str:
    title = _safe_title(track.title)
    length = _fmt_duration(track.duration)
    suffix = f" ({length})" if length else ""
    return f"**{title}**{suffix} — <@{track.requester_id}>"


class MusicCog(commands.GroupCog, name="музыка", description="Музыка в голосовом канале"):
    """Поиск и очередь треков. Источник — ссылка или название, поток качает yt-dlp, играет FFmpeg."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        self.players: dict[int, Player] = {}
        self._leaving: set[int] = set()

    def _player(self, guild_id: int) -> Player:
        player = self.players.get(guild_id)
        if player is None:
            player = Player()
            self.players[guild_id] = player
        return player

    def _member(self, interaction: discord.Interaction) -> discord.Member | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return None
        return interaction.user

    async def _deny(self, interaction: discord.Interaction, text: str):
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def _connect(self, member: discord.Member) -> discord.VoiceClient:
        voice = member.voice
        channel = voice.channel if voice else None
        if channel is None:
            raise RuntimeError("Сначала зайдите в голосовой канал.")
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise RuntimeError("Это не голосовой канал.")
        me = member.guild.me
        if me is None:
            raise RuntimeError("Бот ещё не видит себя на сервере. Подождите пару секунд и повторите.")
        perms = channel.permissions_for(me)
        if not perms.connect or not perms.speak:
            raise RuntimeError("Нет прав зайти в канал или говорить в нём.")

        vc = member.guild.voice_client
        if isinstance(vc, discord.VoiceClient) and vc.is_connected():
            if vc.channel and vc.channel.id != channel.id:
                if vc.is_playing() or vc.is_paused():
                    raise RuntimeError(
                        f"Бот уже играет в {vc.channel.name}. Зайдите туда или остановите музыку командой /музыка стоп."
                    )
                await vc.move_to(channel)
            await self._unsuppress_stage(me, channel)
            return vc
        vc = await channel.connect(self_deaf=True)
        await self._unsuppress_stage(me, channel)
        return vc

    async def _unsuppress_stage(self, me: discord.Member, channel: discord.abc.GuildChannel):
        if isinstance(channel, discord.StageChannel):
            try:
                await me.edit(suppress=False)
            except discord.HTTPException:
                log.warning("Не удалось выйти на сцену в %s", channel.name)

    async def cog_unload(self):
        for guild in list(self.bot.guilds):
            player = self.players.get(guild.id)
            if player:
                player.generation += 1
            vc = guild.voice_client
            if isinstance(vc, discord.VoiceClient):
                await vc.disconnect(force=True)

    def _controlled_by(self, member: discord.Member, vc: discord.VoiceClient | None) -> bool:
        if vc is None or not vc.is_connected() or vc.channel is None:
            return False
        voice = member.voice
        return voice is not None and voice.channel is not None and voice.channel.id == vc.channel.id

    def _begin(self, guild_id: int, vc: discord.VoiceClient, player: Player, track: Track):
        _ensure_opus()
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track.stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS),
            volume=player.volume,
        )
        generation = player.generation
        player.current = track
        try:
            vc.play(source, after=lambda error, gen=generation: self._after(guild_id, gen, error))
        except Exception:
            player.current = None
            raise

    def _after(self, guild_id: int, generation: int, error: Exception | None):
        if error:
            log.error("Ошибка воспроизведения: %s", error)
        asyncio.run_coroutine_threadsafe(self._advance(guild_id, generation), self.bot.loop)

    async def _advance(self, guild_id: int, generation: int, failures: int = 0):
        player = self.players.get(guild_id)
        guild = self.bot.get_guild(guild_id)
        if player is None or guild is None:
            return
        async with player.lock:
            if player.generation != generation:
                return
            vc = guild.voice_client
            if not isinstance(vc, discord.VoiceClient) or not vc.is_connected() or not player.queue:
                player.current = None
                return
            track = player.queue.pop(0)
            try:
                self._begin(guild_id, vc, player, track)
            except Exception:
                log.exception("Не удалось включить %s", track.title)
                player.current = None
                if failures < 3:
                    asyncio.create_task(self._advance(guild_id, generation, failures + 1))

    async def _disconnect(self, guild_id: int):
        if guild_id in self._leaving:
            return
        self._leaving.add(guild_id)
        try:
            player = self.players.get(guild_id)
            if player:
                async with player.lock:
                    player.generation += 1
                    player.queue.clear()
                    player.current = None
            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            if isinstance(vc, discord.VoiceClient):
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                await vc.disconnect(force=True)
        finally:
            self._leaving.discard(guild_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        vc = member.guild.voice_client
        if not isinstance(vc, discord.VoiceClient) or not vc.is_connected() or vc.channel is None:
            return
        if before.channel != vc.channel and after.channel != vc.channel:
            return
        if any(not person.bot for person in vc.channel.members):
            return
        await self._disconnect(member.guild.id)

    @app_commands.command(name="играть", description="Включить трек по ссылке или названию")
    @app_commands.describe(запрос="Название песни или ссылка")
    async def play(self, interaction: discord.Interaction, запрос: str):
        member = self._member(interaction)
        if member is None:
            await self._deny(interaction, "Музыка работает только на сервере.")
            return
        query = запрос.strip()
        if not query:
            await self._deny(interaction, "Напишите название или ссылку.")
            return
        if member.voice is None or member.voice.channel is None:
            await self._deny(interaction, "Сначала зайдите в голосовой канал.")
            return
        try:
            _require_ffmpeg()
            _ensure_opus()
        except RuntimeError as exc:
            await self._deny(interaction, str(exc))
            return

        await interaction.response.defer()
        try:
            info = await asyncio.to_thread(_extract, query)
        except Exception as exc:
            log.exception("Поиск трека не удался")
            message = str(exc).strip() or "неизвестная ошибка"
            if len(message) > 300:
                message = message[:297] + "..."
            await interaction.followup.send(f"Не удалось найти трек: {message}")
            return

        track = Track(
            title=info["title"],
            stream_url=info["stream_url"],
            webpage_url=info["webpage_url"],
            duration=info["duration"],
            requester_id=member.id,
        )
        player = self._player(member.guild.id)
        try:
            async with player.lock:
                vc = await self._connect(member)
                busy = vc.is_playing() or vc.is_paused() or player.current is not None
                if busy:
                    if len(player.queue) >= MAX_QUEUE:
                        await interaction.followup.send(f"Очередь заполнена ({MAX_QUEUE}).", ephemeral=True)
                        return
                    player.queue.append(track)
                    position = len(player.queue)
                else:
                    self._begin(member.guild.id, vc, player, track)
                    position = 0
        except Exception as exc:
            log.exception("Не удалось начать воспроизведение")
            text = str(exc).strip() or "не получилось подключиться к голосовому каналу"
            await interaction.followup.send(text)
            return

        line = _track_line(track)
        if position:
            await interaction.followup.send(f"В очереди под номером {position}: {line}")
        else:
            await interaction.followup.send(f"Сейчас играет: {line}")

    @app_commands.command(name="пауза", description="Поставить музыку на паузу")
    async def pause(self, interaction: discord.Interaction):
        await self._toggle(interaction, pause=True)

    @app_commands.command(name="продолжить", description="Продолжить воспроизведение")
    async def resume(self, interaction: discord.Interaction):
        await self._toggle(interaction, pause=False)

    async def _toggle(self, interaction: discord.Interaction, pause: bool):
        member = self._member(interaction)
        if member is None:
            await self._deny(interaction, "Музыка работает только на сервере.")
            return
        vc = member.guild.voice_client
        if not self._controlled_by(member, vc if isinstance(vc, discord.VoiceClient) else None):
            await self._deny(interaction, "Зайдите в голосовой канал бота, чтобы управлять музыкой.")
            return
        assert isinstance(vc, discord.VoiceClient)
        if pause:
            if not vc.is_playing():
                await self._deny(interaction, "Сейчас ничего не играет.")
                return
            vc.pause()
            await interaction.response.send_message("Пауза.")
            return
        if not vc.is_paused():
            await self._deny(interaction, "Музыка не на паузе.")
            return
        vc.resume()
        await interaction.response.send_message("Продолжаю.")

    @app_commands.command(name="пропустить", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction):
        member = self._member(interaction)
        if member is None:
            await self._deny(interaction, "Музыка работает только на сервере.")
            return
        vc = member.guild.voice_client
        if not isinstance(vc, discord.VoiceClient) or not self._controlled_by(member, vc):
            await self._deny(interaction, "Зайдите в голосовой канал бота, чтобы управлять музыкой.")
            return
        player = self._player(member.guild.id)
        current = player.current
        if not vc.is_playing() and not vc.is_paused():
            await self._deny(interaction, "Сейчас ничего не играет.")
            return
        vc.stop()
        title = _safe_title(current.title) if current else "трек"
        await interaction.response.send_message(f"Пропускаю **{title}**.")

    @app_commands.command(name="стоп", description="Остановить музыку и очистить очередь")
    async def stop(self, interaction: discord.Interaction):
        member = self._member(interaction)
        if member is None:
            await self._deny(interaction, "Музыка работает только на сервере.")
            return
        vc = member.guild.voice_client
        if not isinstance(vc, discord.VoiceClient) or not self._controlled_by(member, vc):
            await self._deny(interaction, "Зайдите в голосовой канал бота, чтобы управлять музыкой.")
            return
        player = self._player(member.guild.id)
        async with player.lock:
            player.generation += 1
            player.queue.clear()
            player.current = None
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await interaction.response.send_message("Остановил и очистил очередь. Бот остаётся в канале.")

    @app_commands.command(name="выйти", description="Отключить бота от голосового канала")
    async def leave(self, interaction: discord.Interaction):
        member = self._member(interaction)
        if member is None:
            await self._deny(interaction, "Музыка работает только на сервере.")
            return
        vc = member.guild.voice_client
        if not isinstance(vc, discord.VoiceClient) or not vc.is_connected():
            await self._deny(interaction, "Бот и так не в голосовом канале.")
            return
        if not self._controlled_by(member, vc):
            await self._deny(interaction, "Зайдите в голосовой канал бота, чтобы его отключить.")
            return
        await self._disconnect(member.guild.id)
        await interaction.response.send_message("Вышел из голосового канала.")

    @app_commands.command(name="очередь", description="Показать, что сейчас играет и что дальше")
    async def queue(self, interaction: discord.Interaction):
        member = self._member(interaction)
        if member is None:
            await self._deny(interaction, "Музыка работает только на сервере.")
            return
        player = self.players.get(member.guild.id)
        if player is None or (player.current is None and not player.queue):
            await interaction.response.send_message("Очередь пустая. Включите трек командой /музыка играть.")
            return
        lines = []
        if player.current:
            lines.append(f"Сейчас: {_track_line(player.current)}")
        if player.queue:
            shown = player.queue[:10]
            lines.append("Дальше:")
            lines.extend(f"{index}. {_track_line(track)}" for index, track in enumerate(shown, start=1))
            if len(player.queue) > len(shown):
                lines.append(f"…и ещё {len(player.queue) - len(shown)}")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="громкость", description="Изменить громкость")
    @app_commands.describe(уровень="От 1 до 100")
    async def volume(self, interaction: discord.Interaction, уровень: app_commands.Range[int, 1, 100]):
        member = self._member(interaction)
        if member is None:
            await self._deny(interaction, "Музыка работает только на сервере.")
            return
        player = self._player(member.guild.id)
        player.volume = уровень / 100
        vc = member.guild.voice_client
        if isinstance(vc, discord.VoiceClient) and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = player.volume
        await interaction.response.send_message(f"Громкость: {уровень}%.")


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
