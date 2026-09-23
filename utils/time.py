from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def fmt_msk(value: str | None) -> str:
    """Показывает сохранённое время по Москве. В базе по-прежнему UTC."""
    if not value:
        return "—"
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")
