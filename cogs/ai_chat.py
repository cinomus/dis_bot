import base64
import json
import logging
import re
from io import BytesIO

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("cogs.ai_chat")

_DATA_URL = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL | re.IGNORECASE)
_DATA_URL_IN_TEXT = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+",
    re.IGNORECASE,
)
_IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MIME_BY_SUFFIX = (
    (".png", "image/png"),
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".webp", "image/webp"),
    (".gif", "image/gif"),
)

MODELS = {
    "chatgpt": {"name": "ChatGPT", "model": config.CHATGPT_MODEL, "image": False},
    "claude": {"name": "Claude", "model": config.CLAUDE_MODEL, "image": False},
    "gemini": {"name": "Gemini", "model": config.GEMINI_MODEL, "image": False},
    "nano_banana": {"name": "Nano Banana", "model": config.NANO_BANANA_MODEL, "image": True},
}

PROVIDER_CHOICES = [
    app_commands.Choice(name="ChatGPT", value="chatgpt"),
    app_commands.Choice(name="Claude", value="claude"),
    app_commands.Choice(name="Gemini", value="gemini"),
    app_commands.Choice(name="Nano Banana (картинка)", value="nano_banana"),
]


def _error_message(data, status: int) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    return f"NordRouter ответил {status}."


def _decode_data_url(url: str) -> tuple[bytes, str] | None:
    match = _DATA_URL.match(url.strip())
    if not match:
        return None
    mime, payload = match.group(1).lower(), match.group(2)
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    if not raw:
        return None
    return raw, _IMAGE_EXT.get(mime, "png")


def _image_urls(value) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        urls.extend(_DATA_URL_IN_TEXT.findall(value))
    elif isinstance(value, dict):
        direct = value.get("url")
        if isinstance(direct, str):
            urls.append(direct)
        nested = value.get("image_url")
        urls.extend(_image_urls(nested))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_image_urls(item))
    return urls


def _message_images(message: dict) -> list[tuple[bytes, str]]:
    images: list[tuple[bytes, str]] = []
    seen: set[str] = set()
    for url in _image_urls(message.get("images")) + _image_urls(message.get("content")):
        if url in seen:
            continue
        seen.add(url)
        decoded = _decode_data_url(url)
        if decoded:
            images.append(decoded)
    return images


def _attachment_mime(attachment: discord.Attachment) -> str | None:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type in _IMAGE_EXT:
        return content_type
    name = (attachment.filename or "").lower()
    for suffix, mime in _MIME_BY_SUFFIX:
        if name.endswith(suffix):
            return mime
    return None


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


class AIChatCog(commands.Cog):
    """ChatGPT, Claude, Gemini и Nano Banana через NordRouter."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        timeout = aiohttp.ClientTimeout(total=180)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def _complete(
        self,
        model: str,
        prompt: str,
        *,
        image: bool,
        source: tuple[bytes, str] | None = None,
    ) -> dict:
        if not config.NORDROUTER_API_KEY:
            raise RuntimeError("NORDROUTER_API_KEY не настроен на сервере.")
        if self.session is None:
            raise RuntimeError("HTTP-сессия ещё не готова.")

        headers = {
            "Authorization": f"Bearer {config.NORDROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        if source is not None:
            raw, mime = source
            encoded = base64.b64encode(raw).decode("ascii")
            content: str | list = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ]
        else:
            content = prompt
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        if image:
            payload["modalities"] = ["image", "text"]
        else:
            payload["max_tokens"] = 2000

        url = f"{config.NORDROUTER_BASE_URL}/chat/completions"
        async with self.session.post(url, headers=headers, json=payload) as resp:
            raw = await resp.read()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                snippet = raw.decode("utf-8", errors="replace").strip()[:300]
                raise RuntimeError(snippet or f"NordRouter ответил {resp.status}.")
            if resp.status != 200:
                raise RuntimeError(_error_message(data, resp.status))
            return data

    @app_commands.command(name="ask", description="Спросить ChatGPT, Claude, Gemini или сгенерировать и править картинку Nano Banana")
    @app_commands.rename(provider="модель", prompt="запрос", picture="картинка")
    @app_commands.describe(
        provider="Какую модель спросить",
        prompt="Вопрос, описание новой картинки или что изменить",
        picture="Картинка для правки. Работает только с Nano Banana",
    )
    @app_commands.choices(provider=PROVIDER_CHOICES)
    async def ask(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
        prompt: str,
        picture: discord.Attachment | None = None,
    ):
        await interaction.response.defer(thinking=True)
        try:
            spec = MODELS.get(provider.value)
            if spec is None:
                await interaction.followup.send("⚠️ Неизвестная модель.")
                return

            source = None
            if picture is not None:
                if not spec["image"]:
                    await interaction.followup.send("⚠️ Картинку можно передать только модели Nano Banana.")
                    return
                mime = _attachment_mime(picture)
                if mime is None:
                    await interaction.followup.send("⚠️ Нужен файл PNG, JPEG, WEBP или GIF.")
                    return
                if picture.size > _MAX_IMAGE_BYTES:
                    await interaction.followup.send("⚠️ Картинка больше 8 МБ.")
                    return
                source = (await picture.read(), mime)

            data = await self._complete(spec["model"], prompt, image=spec["image"], source=source)
            choices = data.get("choices") or []
            message = (choices[0].get("message") if choices else None) or {}
            if spec["image"]:
                images = _message_images(message)
                if not images:
                    await interaction.followup.send("⚠️ Модель не вернула картинку.")
                    return
                files = [
                    discord.File(BytesIO(raw), filename=f"nano-banana-{index}.{ext}")
                    for index, (raw, ext) in enumerate(images[:4], start=1)
                ]
                await interaction.followup.send(files=files)
                return

            answer = _message_text(message)
            if not answer:
                await interaction.followup.send("⚠️ Модель не вернула ответ.")
                return

            if len(answer) > 1900:
                answer = answer[:1900] + "…"

            embed = discord.Embed(
                title=f"Ответ ({spec['name']})",
                description=answer,
                colour=discord.Colour.green(),
            )
            embed.set_footer(text=f"Вопрос от {interaction.user.display_name}")
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.exception("Ошибка запроса к %s", provider.value)
            try:
                await interaction.followup.send(f"⚠️ Ошибка: {exc}")
            except Exception:
                log.exception("Не удалось отправить сообщение об ошибке")


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))
