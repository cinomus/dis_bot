import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# ID сервера для мгновенной синхронизации слэш-команд при разработке (необязательно).
# Если не задан — команды синхронизируются глобально (может занять до часа на обновление).
GUILD_ID = os.getenv("GUILD_ID")

DB_PATH = os.getenv("DB_PATH", "data/bot.db")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

DEFAULT_VOTE_THRESHOLD = int(os.getenv("VOTE_THRESHOLD", "3"))
DEFAULT_VOTE_DURATION_HOURS = float(os.getenv("VOTE_DURATION_HOURS", "24"))

# Права на редактирование/удаление ролей, созданных через бота.
# Названия ролей по умолчанию — можно переопределить в .env, либо позже
# командой /role setup указать конкретные роли на сервере (это надёжнее,
# так как не зависит от точного совпадения названия).
ROLE_ADMIN_NAME = os.getenv("ROLE_ADMIN_NAME", "админ")
ROLE_TRUSTED_NAME = os.getenv("ROLE_TRUSTED_NAME", "бро <3")
DEFAULT_ROLE_EDIT_WINDOW_HOURS = float(os.getenv("ROLE_EDIT_WINDOW_HOURS", "24"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан. Создайте файл .env на основе .env.example.")
