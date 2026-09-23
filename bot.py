import asyncio
import logging
import os

import discord
from discord.ext import commands

import config
from database.db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(config.DB_PATH)

    async def setup_hook(self):
        await self.db.connect()
        log.info("База данных подключена: %s", config.DB_PATH)

        # Автозагрузка всех когов из папки cogs/.
        # Чтобы добавить новую функцию бота — просто положите новый файл в cogs/
        # с функцией `async def setup(bot): await bot.add_cog(...)` внутри.
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        for filename in sorted(os.listdir(cogs_dir)):
            if filename.endswith(".py") and not filename.startswith("_"):
                extension = f"cogs.{filename[:-3]}"
                try:
                    await self.load_extension(extension)
                    log.info("Загружен ког: %s", extension)
                except Exception:
                    log.exception("Не удалось загрузить ког %s", extension)

        if config.GUILD_ID:
            guild = discord.Object(id=int(config.GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Слэш-команды синхронизированы для гильдии %s (%d команд)", config.GUILD_ID, len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Слэш-команды синхронизированы глобально (%d команд, обновление может занять до часа)", len(synced))

    async def close(self):
        await self.db.close()
        await super().close()


bot = MyBot()


@bot.event
async def on_ready():
    log.info("Бот запущен как %s (ID: %s)", bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Game(name="/role, /pozor, /ask, /музыка"))


async def main():
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
