import logging
from datetime import datetime
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.dump import DumpError, export_text, import_text
from utils.settings import get_settings, resolve_role
from utils.time import MSK

log = logging.getLogger("cogs.backup")

CONFIRM_WORD = "ЗАМЕНИТЬ"


@app_commands.default_permissions(administrator=True)
class BackupCog(commands.GroupCog, name="backup", description="Копия базы данных"):
    """Выгрузка всей SQLite-базы в текстовый файл и загрузка этого файла обратно."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if interaction.guild is None or not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.administrator:
            return True
        settings = await get_settings(self.bot.db, interaction.guild.id)
        admin_role = resolve_role(interaction.guild, settings["admin_role_id"], config.ROLE_ADMIN_NAME)
        return admin_role is not None and admin_role in user.roles

    @app_commands.command(name="export", description="Выгрузить всю базу в текстовый файл")
    async def export_backup(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("Выгружать базу может только админ.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await export_text(self.bot.db)
        except Exception:
            log.exception("Не удалось выгрузить базу")
            await interaction.followup.send("Не удалось выгрузить базу.", ephemeral=True)
            return
        stamp = datetime.now(MSK).strftime("%Y%m%d-%H%M%S")
        file = discord.File(BytesIO(text.encode("utf-8")), filename=f"backup-{stamp}.txt")
        await interaction.followup.send(
            "Вся база в этом файле. Чтобы вернуть её, вызовите /backup import "
            f"и в подтверждении напишите {CONFIRM_WORD}.",
            file=file,
            ephemeral=True,
        )

    @app_commands.command(name="import", description="Заменить базу содержимым текстового файла")
    @app_commands.describe(
        file="Файл backup-….txt, который раньше выгрузил бот",
        confirm="Напишите ЗАМЕНИТЬ. Текущие данные бота будут стёрты",
    )
    async def import_backup(self, interaction: discord.Interaction, file: discord.Attachment, confirm: str):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("Загружать базу может только админ.", ephemeral=True)
            return
        if confirm.strip().casefold() != CONFIRM_WORD.casefold():
            await interaction.response.send_message(
                f"База не тронута. Чтобы заменить её, в подтверждении напишите {CONFIRM_WORD}.",
                ephemeral=True,
            )
            return
        if file.size > 8 * 1024 * 1024:
            await interaction.response.send_message("Файл больше 8 МБ.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            raw = await file.read()
            text = raw.decode("utf-8-sig")
            counts = await import_text(self.bot.db, text)
        except DumpError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except UnicodeDecodeError:
            await interaction.followup.send("Файл должен быть текстом в UTF-8.", ephemeral=True)
            return
        except Exception:
            log.exception("Не удалось загрузить базу из файла")
            await interaction.followup.send("Не удалось загрузить базу из файла.", ephemeral=True)
            return
        lines = [f"{name}: {count}" for name, count in counts.items()]
        await interaction.followup.send(
            "База заменена содержимым файла.\n" + "\n".join(lines),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))
