import base64
import json
import logging
import re
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from xml.etree import ElementTree as ET

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

_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TEXT_CHARS = 80_000
_MIME_BY_SUFFIX = (
    (".png", "image/png"),
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".webp", "image/webp"),
    (".gif", "image/gif"),
)
_TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx",
    ".py", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".rb", ".php", ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".log", ".sh", ".ps1", ".bat", ".vue", ".svelte",
})
_TEXT_MIMES = frozenset({
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/typescript",
    "application/yaml",
    "application/x-yaml",
    "application/csv",
    "application/x-sh",
    "application/sql",
})
_DOCX_SUFFIXES = frozenset({".docx", ".docm"})
_XLSX_SUFFIXES = frozenset({".xlsx", ".xlsm"})
_DOCX_MIMES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
})
_XLSX_MIMES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
})
_LEGACY_OFFICE_SUFFIXES = frozenset({".doc", ".xls"})
_LEGACY_OFFICE_MIMES = frozenset({
    "application/msword",
    "application/vnd.ms-excel",
})
_EXCEL_DATE_FMT_IDS = frozenset(str(item) for item in (*range(14, 23), 45, 46, 47))

MODELS = {
    "chatgpt": {"name": "ChatGPT", "model": config.CHATGPT_MODEL, "generates_image": False},
    "claude": {"name": "Claude", "model": config.CLAUDE_MODEL, "generates_image": False},
    "gemini": {"name": "Gemini", "model": config.GEMINI_MODEL, "generates_image": False},
    "nano_banana": {"name": "Nano Banana", "model": config.NANO_BANANA_MODEL, "generates_image": True},
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


def _content_type(attachment: discord.Attachment) -> str:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type == "image/jpg":
        return "image/jpeg"
    return content_type


def _suffix(filename: str) -> str:
    dot = filename.rfind(".")
    if dot < 0:
        return ""
    return filename[dot:].lower()


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = (name or "").replace("\n", " ").replace("\r", " ").replace("`", "").strip()
    return cleaned or fallback


def _attachment_mime(attachment: discord.Attachment) -> str | None:
    content_type = _content_type(attachment)
    if content_type in _IMAGE_EXT:
        return content_type
    name = (attachment.filename or "").lower()
    for suffix, mime in _MIME_BY_SUFFIX:
        if name.endswith(suffix):
            return mime
    return None


def _local(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


class OfficeReadError(Exception):
    """Файл Word или Excel не удалось разобрать."""


def _paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        local = _local(node.tag)
        if local == "t" and node.text:
            parts.append(node.text)
        elif local == "tab":
            parts.append("\t")
        elif local == "br":
            parts.append("\n")
    return "".join(parts).strip()


def _cell_text(cell: ET.Element) -> str:
    parts: list[str] = []

    def walk(node: ET.Element):
        for child in list(node):
            local = _local(child.tag)
            if local == "tbl":
                continue
            if local == "p":
                text = _paragraph_text(child)
                if text:
                    parts.append(text)
            else:
                walk(child)

    walk(cell)
    return " ".join(parts)


def _docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise OfficeReadError from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise OfficeReadError from exc

    lines: list[str] = []

    def walk(node: ET.Element):
        for child in list(node):
            local = _local(child.tag)
            if local == "p":
                text = _paragraph_text(child)
                if text:
                    lines.append(text)
            elif local == "tbl":
                for row in child:
                    if _local(row.tag) != "tr":
                        continue
                    cells = [_cell_text(cell) for cell in row if _local(cell.tag) == "tc"]
                    if any(cells):
                        lines.append(" | ".join(cells))
            elif local in ("body", "sdt", "sdtContent"):
                walk(child)

    walk(root)
    return "\n".join(lines).strip()


def _looks_like_date_format(code: str) -> bool:
    cleaned = re.sub(r'"[^"]*"|\[[^\]]*\]', "", code).lower()
    return any(token in cleaned for token in ("y", "d", "h"))


def _excel_date_styles(archive: zipfile.ZipFile) -> set[int]:
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return set()
    custom_dates: set[str] = set(_EXCEL_DATE_FMT_IDS)
    for node in root.iter():
        if _local(node.tag) != "numFmt":
            continue
        fmt_id = node.get("numFmtId")
        if fmt_id and _looks_like_date_format(node.get("formatCode") or ""):
            custom_dates.add(fmt_id)
    indexes: set[int] = set()
    cell_xfs = next((node for node in root.iter() if _local(node.tag) == "cellXfs"), None)
    if cell_xfs is None:
        return set()
    for index, xf in enumerate(child for child in cell_xfs if _local(child.tag) == "xf"):
        if xf.get("numFmtId") in custom_dates:
            indexes.add(index)
    return indexes


def _excel_serial(value: str) -> str:
    try:
        number = float(value)
    except ValueError:
        return value
    if number < 0 or number > 2958465:
        return value
    moment = datetime(1899, 12, 30) + timedelta(days=number)
    if number < 1:
        return moment.strftime("%H:%M")
    if abs(number - round(number)) < 1e-6:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ET.ParseError as exc:
        raise OfficeReadError from exc
    strings: list[str] = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        strings.append("".join(node.text or "" for node in item.iter() if _local(node.tag) == "t"))
    return strings


def _column_index(ref: str) -> int:
    index = 0
    for char in ref:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return index


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise OfficeReadError from exc
    targets: dict[str, str] = {}
    for node in rels:
        rel_id = node.get("Id")
        target = node.get("Target")
        if not rel_id or not target:
            continue
        path = target.lstrip("/")
        targets[rel_id] = path if path.startswith("xl/") else f"xl/{path}"
    sheets: list[tuple[str, str]] = []
    for node in workbook.iter():
        if _local(node.tag) != "sheet":
            continue
        rel_id = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        if rel_id is None:
            rel_id = next((value for key, value in node.attrib.items() if key.endswith("}id") or key == "id"), None)
        path = targets.get(rel_id or "")
        if path:
            sheets.append((node.get("name") or "Лист", path))
    return sheets


def _cell_value(cell: ET.Element, strings: list[str], date_styles: set[int]) -> str:
    kind = cell.get("t") or ""
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
    value = next((node.text or "" for node in cell if _local(node.tag) == "v"), "")
    if kind == "s":
        try:
            return strings[int(value)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    if kind == "str":
        return value
    style = cell.get("s")
    if value and style is not None and style.isdigit() and int(style) in date_styles:
        return _excel_serial(value)
    if not value and kind != "inlineStr":
        formula = next((node.text or "" for node in cell if _local(node.tag) == "f"), "")
        return formula
    return value


def _xlsx_text(raw: bytes) -> str:
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise OfficeReadError from exc
    with archive:
        strings = _shared_strings(archive)
        date_styles = _excel_date_styles(archive)
        blocks: list[str] = []
        for name, path in _sheet_targets(archive):
            try:
                root = ET.fromstring(archive.read(path))
            except (KeyError, ET.ParseError) as exc:
                raise OfficeReadError from exc
            rows: list[str] = []
            for row in root.iter():
                if _local(row.tag) != "row":
                    continue
                values: dict[int, str] = {}
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    column = _column_index(cell.get("r") or "")
                    if column <= 0:
                        column = max(values, default=0) + 1
                    text = _cell_value(cell, strings, date_styles).replace("\n", " ").strip()
                    if text:
                        values[column] = text
                if not values:
                    continue
                last = max(values)
                rows.append("\t".join(values.get(index, "") for index in range(1, last + 1)))
            if rows:
                blocks.append(f"## {name}\n" + "\n".join(rows))
    return "\n\n".join(blocks).strip()


def _office_text(kind: str, raw: bytes) -> str:
    if kind == "docx":
        return _docx_text(raw)
    return _xlsx_text(raw)


def _classify_attachment(attachment: discord.Attachment) -> tuple[str, str] | None:
    """Возвращает (image|text|pdf|docx|xlsx|legacy, mime) или None."""
    image_mime = _attachment_mime(attachment)
    if image_mime:
        return "image", image_mime
    content_type = _content_type(attachment)
    suffix = _suffix((attachment.filename or "").lower())
    if suffix == ".pdf" or content_type == "application/pdf":
        return "pdf", "application/pdf"
    if suffix in _DOCX_SUFFIXES or content_type in _DOCX_MIMES:
        return "docx", content_type or "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix in _XLSX_SUFFIXES or content_type in _XLSX_MIMES:
        return "xlsx", content_type or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix in _LEGACY_OFFICE_SUFFIXES or content_type in _LEGACY_OFFICE_MIMES:
        return "legacy", content_type or "application/octet-stream"
    if suffix in _TEXT_SUFFIXES or content_type.startswith("text/") or content_type in _TEXT_MIMES:
        mime = content_type if content_type.startswith("text/") or content_type in _TEXT_MIMES else "text/plain"
        return "text", mime
    return None


def _user_content(prompt: str, attachment: tuple[str, bytes, str, str] | None) -> str | list:
    if attachment is None:
        return prompt
    kind, raw, mime, filename = attachment
    if kind == "text":
        text = raw.decode("utf-8-sig", errors="replace").strip()
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS] + "\n…(файл обрезан)"
        return f"{prompt}\n\n--- файл: {filename} ---\n{text}"
    encoded = base64.b64encode(raw).decode("ascii")
    if kind == "image":
        extra: dict = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
    else:
        extra = {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": f"data:{mime};base64,{encoded}",
            },
        }
    return [
        {"type": "text", "text": prompt},
        extra,
    ]


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
        generates_image: bool,
        attachment: tuple[str, bytes, str, str] | None = None,
    ) -> dict:
        if not config.NORDROUTER_API_KEY:
            raise RuntimeError("NORDROUTER_API_KEY не настроен на сервере.")
        if self.session is None:
            raise RuntimeError("HTTP-сессия ещё не готова.")

        headers = {
            "Authorization": f"Bearer {config.NORDROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": _user_content(prompt, attachment)}],
        }
        if generates_image:
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
    @app_commands.rename(provider="модель", prompt="запрос", file="файл")
    @app_commands.describe(
        provider="Какую модель спросить",
        prompt="Вопрос, описание новой картинки или что изменить",
        file="Картинка, текст, PDF, Word или Excel. Nano Banana — только картинка",
    )
    @app_commands.choices(provider=PROVIDER_CHOICES)
    async def ask(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
        prompt: str,
        file: discord.Attachment | None = None,
    ):
        await interaction.response.defer(thinking=True)
        try:
            spec = MODELS.get(provider.value)
            if spec is None:
                await interaction.followup.send("⚠️ Неизвестная модель.")
                return

            attachment = None
            if file is not None:
                classified = _classify_attachment(file)
                if classified is None:
                    await interaction.followup.send(
                        "⚠️ Этот тип файла не поддерживается. Нужна картинка, текст, PDF, Word (.docx) или Excel (.xlsx)."
                    )
                    return
                kind, mime = classified
                if kind == "legacy":
                    await interaction.followup.send(
                        "⚠️ Старые .doc и .xls не читаются. Сохраните файл как .docx или .xlsx."
                    )
                    return
                if spec["generates_image"] and kind != "image":
                    await interaction.followup.send(
                        "⚠️ Nano Banana принимает только картинку (PNG, JPEG, WEBP или GIF)."
                    )
                    return
                if file.size > _MAX_FILE_BYTES:
                    await interaction.followup.send("⚠️ Файл больше 8 МБ.")
                    return
                raw = await file.read()
                if not raw:
                    await interaction.followup.send("⚠️ Файл пустой.")
                    return
                filename = _safe_filename(
                    file.filename,
                    {"image": "image.png", "pdf": "file.pdf", "text": "file.txt", "docx": "file.docx", "xlsx": "file.xlsx"}[kind],
                )
                if kind in ("docx", "xlsx"):
                    try:
                        extracted = _office_text(kind, raw)
                    except OfficeReadError:
                        await interaction.followup.send("⚠️ Не удалось прочитать файл Word или Excel.")
                        return
                    if not extracted:
                        await interaction.followup.send("⚠️ В файле нет текста.")
                        return
                    attachment = ("text", extracted.encode("utf-8"), "text/plain", filename)
                else:
                    if kind == "text" and b"\x00" in raw:
                        await interaction.followup.send("⚠️ Файл не похож на текст.")
                        return
                    if kind == "text" and not raw.decode("utf-8-sig", errors="replace").strip():
                        await interaction.followup.send("⚠️ В файле нет текста.")
                        return
                    attachment = (kind, raw, mime, filename)

            data = await self._complete(
                spec["model"],
                prompt,
                generates_image=spec["generates_image"],
                attachment=attachment,
            )
            choices = data.get("choices") or []
            message = (choices[0].get("message") if choices else None) or {}
            if spec["generates_image"]:
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
