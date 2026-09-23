import discord
from discord import app_commands
from discord.ext import commands


def _allowed(member: discord.Member, command: app_commands.Command) -> bool:
    """Команда видна в Discord, если хватает прав. Администратор проходит всегда."""
    if member.guild_permissions.administrator:
        return True
    required = command.default_permissions
    parent = command.parent
    while parent is not None:
        if parent.default_permissions is not None:
            if required is None:
                required = parent.default_permissions
            else:
                required = discord.Permissions(required.value | parent.default_permissions.value)
        parent = getattr(parent, "parent", None)
    if required is None:
        return True
    return (required.value & member.guild_permissions.value) == required.value


def _sections(member: discord.Member, commands: list[app_commands.Command]) -> list[str]:
    grouped: dict[str, list[app_commands.Command]] = {}
    order: list[str] = []
    for command in commands:
        if not _allowed(member, command):
            continue
        parent = command.parent
        key = parent.qualified_name if parent is not None else ""
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(command)

    blocks = []
    for key in order:
        items = sorted(grouped[key], key=lambda item: item.qualified_name)
        parent = items[0].parent
        if parent is not None:
            header = f"**/{parent.qualified_name}** — {parent.description}"
            lines = [f"`/{item.qualified_name}` — {item.description}" for item in items]
            blocks.append(header + "\n" + "\n".join(lines))
        else:
            blocks.extend(f"`/{item.qualified_name}` — {item.description}" for item in items)
    return blocks


def _pages(blocks: list[str], limit: int = 3900) -> list[str]:
    pages: list[str] = []
    current = ""
    for block in blocks:
        piece = block if not current else f"{current}\n\n{block}"
        if len(piece) <= limit:
            current = piece
            continue
        if current:
            pages.append(current)
        current = block
    if current:
        pages.append(current)
    return pages


class HelpCog(commands.Cog):
    """Список слэш-команд, которые этому участнику можно вызвать."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Показать команды, которые вам доступны, и что они делают")
    @app_commands.guild_only()
    async def help_command(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Список команд смотрите на сервере.", ephemeral=True)
            return

        listed = list(self.bot.tree.walk_commands(guild=interaction.guild))
        if not listed:
            listed = list(self.bot.tree.walk_commands())
        blocks = _sections(member, listed)
        if not blocks:
            await interaction.response.send_message("Вам сейчас не доступна ни одна команда.", ephemeral=True)
            return

        pages = _pages(blocks)
        embeds = []
        for index, page in enumerate(pages, start=1):
            title = "Доступные команды" if len(pages) == 1 else f"Доступные команды ({index}/{len(pages)})"
            embeds.append(discord.Embed(title=title, description=page, colour=discord.Colour.blurple()))
        await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
