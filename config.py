import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# ID сервера для мгновенной синхронизации слэш-команд при разработке (необязательно).
# Если не задан — команды синхронизируются глобально (может занять до часа на обновление).
GUILD_ID = os.getenv("GUILD_ID")

DB_PATH = os.getenv("DB_PATH", "data/bot.db")

# Один ключ NordRouter на все модели. ID — из каталога https://nordrouter.com (с префиксом провайдера).
NORDROUTER_API_KEY = os.getenv("NORDROUTER_API_KEY")
NORDROUTER_BASE_URL = os.getenv("NORDROUTER_BASE_URL", "https://nordrouter.com/v1").rstrip("/")

CHATGPT_MODEL = os.getenv("CHATGPT_MODEL", "openai/gpt-5.4-mini")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "anthropic/claude-sonnet-4.6")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "google/gemini-3.5-flash")
NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "google/gemini-3.1-flash-image-preview")

DEFAULT_VOTE_THRESHOLD = int(os.getenv("VOTE_THRESHOLD", "3"))
DEFAULT_VOTE_DURATION_HOURS = float(os.getenv("VOTE_DURATION_HOURS", "24"))
# Сколько часов после снятия позора нельзя снова запускать голосование за выдачу.
DEFAULT_POZOR_COOLDOWN_HOURS = float(os.getenv("POZOR_COOLDOWN_HOURS", "6"))

# Права на редактирование/удаление ролей, созданных через бота.
# Названия ролей по умолчанию — можно переопределить в .env, либо позже
# командой /role setup указать конкретные роли на сервере (это надёжнее,
# так как не зависит от точного совпадения названия).
ROLE_ADMIN_NAME = os.getenv("ROLE_ADMIN_NAME", "админ")
ROLE_TRUSTED_NAME = os.getenv("ROLE_TRUSTED_NAME", "бро <3")
DEFAULT_ROLE_EDIT_WINDOW_HOURS = float(os.getenv("ROLE_EDIT_WINDOW_HOURS", "24"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан. Создайте файл .env на основе .env.example.")
