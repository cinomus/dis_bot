import json
import re

DUMP_VERSION = 1
MAX_DUMP_BYTES = 8 * 1024 * 1024

# Порядок фиксированный: и выгрузка, и загрузка идут по этому списку.
TABLES = (
    "guild_settings",
    "managed_roles",
    "role_grants",
    "shame_records",
    "shame_votes",
)

_COLUMN_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class DumpError(ValueError):
    """Файл копии повреждён или записан в неизвестном формате."""


async def _columns(conn, table: str) -> list[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return [row[1] for row in rows]


def _cell(value):
    if value is None or isinstance(value, (str, int, float)):
        if isinstance(value, bool):
            return int(value)
        return value
    raise DumpError("В файле есть значение, которое нельзя записать в базу.")


async def export_text(db) -> str:
    """Вся база одним текстовым JSON. Числа и русский текст сохраняются как есть."""
    async with db.lock:
        return await _export_text(db)


async def _export_text(db) -> str:
    payload = {"version": DUMP_VERSION, "tables": {}}
    for table in TABLES:
        columns = await _columns(db.conn, table)
        column_sql = ", ".join(columns)
        cur = await db.conn.execute(f"SELECT {column_sql} FROM {table}")
        fetched = await cur.fetchall()
        await cur.close()
        payload["tables"][table] = {
            "columns": columns,
            "rows": [list(row) for row in fetched],
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def import_text(db, text: str) -> dict[str, int]:
    """Заменяет содержимое всех таблиц данными из файла. Возвращает число строк по таблицам."""
    if len(text.encode("utf-8")) > MAX_DUMP_BYTES:
        raise DumpError("Файл больше 8 МБ.")
    async with db.lock:
        return await _import_text(db, text)


async def _import_text(db, text: str) -> dict[str, int]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DumpError("Файл не похож на копию базы. Нужен текст, который выгрузил бот.") from exc
    if not isinstance(payload, dict) or payload.get("version") != DUMP_VERSION:
        raise DumpError("Эта версия файла боту не знакома.")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise DumpError("В файле нет данных таблиц.")

    unknown = sorted(set(tables) - set(TABLES))
    if unknown:
        raise DumpError("В файле есть неизвестные таблицы: " + ", ".join(unknown))

    prepared: list[tuple[str, list[str], list[list]]] = []
    for table in TABLES:
        block = tables.get(table, {"columns": [], "rows": []})
        if not isinstance(block, dict):
            raise DumpError(f"Таблица {table} записана неверно.")
        columns = block.get("columns", [])
        rows = block.get("rows", [])
        if not isinstance(columns, list) or not all(isinstance(name, str) for name in columns):
            raise DumpError(f"У таблицы {table} сломан список колонок.")
        if not isinstance(rows, list):
            raise DumpError(f"У таблицы {table} сломан список строк.")
        if any(not _COLUMN_NAME.fullmatch(name) for name in columns):
            raise DumpError(f"У таблицы {table} недопустимое имя колонки.")
        existing = set(await _columns(db.conn, table))
        missing = [name for name in columns if name not in existing]
        if missing:
            raise DumpError(f"В текущей базе нет колонок {table}: {', '.join(missing)}")
        if len(columns) != len(set(columns)):
            raise DumpError(f"В таблице {table} колонка указана дважды.")
        clean_rows = []
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns):
                raise DumpError(f"В таблице {table} есть строка не той длины.")
            clean_rows.append([_cell(value) for value in row])
        prepared.append((table, columns, clean_rows))

    # Закрываем случайно открытую транзакцию, иначе BEGIN упадёт.
    await db.conn.commit()
    try:
        await db.conn.execute("BEGIN IMMEDIATE")
        for table, columns, rows in prepared:
            await db.conn.execute(f"DELETE FROM {table}")
            if not rows or not columns:
                continue
            placeholders = ", ".join("?" * len(columns))
            column_sql = ", ".join(columns)
            await db.conn.executemany(
                f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                rows,
            )
        await db.conn.commit()
    except Exception:
        await db.conn.rollback()
        raise
    return {table: len(rows) for table, _, rows in prepared}
