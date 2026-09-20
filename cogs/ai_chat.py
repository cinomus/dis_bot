import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("cogs.ai_chat")

PROVIDER_CHOICES = [
    app_commands.Choice(name="ChatGPT (OpenAI)", value="openai"),
    app_commands.Choice(name="Claude (Anthropic)", value="anthropic"),
    app_commands.Choice(name="DeepSeek", value="deepseek"),
]


class AIChatCog(commands.Cog):
    """Обращение к разным AI-провайдерам прямо из Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    async def _ask_openai_compatible(self, base_url: str, api_key: str, model: str, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1000}
        async with self.session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", str(data)))
            return data["choices"][0]["message"]["content"]

    async def _ask_anthropic(self, api_key: str, model: str, prompt: str) -> str:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {"model": model, "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]}
        async with self.session.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", str(data)))
            return "".join(block.get("text", "") for block in data.get("content", []))

    @app_commands.command(name="ask", description="Задать вопрос AI-модели")
    @app_commands.describe(provider="Какую модель спросить", prompt="Текст вопроса")
    @app_commands.choices(provider=PROVIDER_CHOICES)
    async def ask(self, interaction: discord.Interaction, provider: app_commands.Choice[str], prompt: str):
        await interaction.response.defer(thinking=True)
        try:
            if provider.value == "openai":
                if not config.OPENAI_API_KEY:
                    raise RuntimeError("OPENAI_API_KEY не настроен на сервере.")
                answer = await self._ask_openai_compatible(
                    "https://api.openai.com/v1", config.OPENAI_API_KEY, config.OPENAI_MODEL, prompt
                )
            elif provider.value == "anthropic":
                if not config.ANTHROPIC_API_KEY:
                    raise RuntimeError("ANTHROPIC_API_KEY не настроен на сервере.")
                answer = await self._ask_anthropic(config.ANTHROPIC_API_KEY, config.ANTHROPIC_MODEL, prompt)
            elif provider.value == "deepseek":
                if not config.DEEPSEEK_API_KEY:
                    raise RuntimeError("DEEPSEEK_API_KEY не настроен на сервере.")
                answer = await self._ask_openai_compatible(
                    "https://api.deepseek.com/v1", config.DEEPSEEK_API_KEY, config.DEEPSEEK_MODEL, prompt
                )
            else:
                raise RuntimeError("Неизвестный провайдер.")
        except Exception as exc:
            log.exception("Ошибка запроса к %s", provider.value)
            await interaction.followup.send(f"⚠️ Ошибка: {exc}")
            return

        if len(answer) > 1900:
            answer = answer[:1900] + "…"

        embed = discord.Embed(title=f"Ответ ({provider.name})", description=answer, colour=discord.Colour.green())
        embed.set_footer(text=f"Вопрос от {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))
