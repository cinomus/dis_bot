"""Общий вызов NordRouter для /ask и тренера.

GPT-5 думает внутри max_tokens и может вернуть пустой content.
Часть моделей кладёт ответ не строкой, а списком блоков, а ошибку — в message, не в error.
"""

import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("utils.nordrouter")


class NordRouterError(RuntimeError):
    """Текст уже можно показать человеку."""


def _error_message(data, status: int) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error.strip():
            return error.strip()
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return f"NordRouter ответил {status}."


def choice_message(data: dict) -> dict:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list) or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message") or {}
    return message if isinstance(message, dict) else {}


def finish_reason(data: dict) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list) or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or "")


def assistant_text(message: dict) -> str:
    """Видимый ответ. Цепочку рассуждений сюда не подмешиваем."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if isinstance(text, str):
            parts.append(text)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "")
            if kind in ("reasoning", "thinking", "redacted_thinking"):
                continue
            text = part.get("text") or part.get("content") or part.get("output_text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _reasons_by_default(model: str) -> bool:
    name = (model or "").lower()
    return name.startswith("openai/gpt-5") or name.startswith("openai/o")


def _hit_length(data: dict) -> bool:
    if assistant_text(choice_message(data)):
        return False
    return finish_reason(data) == "length"


async def _post(session, url: str, headers: dict, payload: dict) -> tuple[int, dict]:
    last_status = 0
    last_data: dict = {}
    for attempt in range(2):
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                raw = await resp.read()
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    snippet = raw.decode("utf-8", errors="replace").strip()[:300]
                    data = {"message": snippet or f"NordRouter ответил {resp.status}."}
                if not isinstance(data, dict):
                    data = {"message": str(data)[:300]}
                if resp.status in (429, 502, 503) and attempt == 0:
                    await asyncio.sleep(2)
                    last_status, last_data = resp.status, data
                    continue
                return resp.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == 0:
                log.warning("NordRouter не ответил, повтор: %s", exc)
                await asyncio.sleep(2)
                continue
            raise NordRouterError("NordRouter не отвечает. Попробуй ещё раз через минуту.") from exc
    return last_status, last_data


async def chat_complete(
    session,
    api_key: str,
    base_url: str,
    *,
    model: str,
    messages: list,
    max_tokens: int | None = None,
    modalities: list[str] | None = None,
) -> dict:
    if not api_key:
        raise NordRouterError("NORDROUTER_API_KEY не настроен.")
    if session is None:
        raise NordRouterError("HTTP-сессия ещё не готова.")

    payload: dict = {"model": model, "messages": messages}
    if modalities:
        payload["modalities"] = modalities
    elif max_tokens:
        payload["max_tokens"] = max_tokens
        if _reasons_by_default(model):
            payload["reasoning"] = {"effort": "low"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    status, data = await _post(session, url, headers, payload)
    if status == 400 and "reasoning" in payload:
        log.info("Модель %s не приняла reasoning, повторяю без него", model)
        payload.pop("reasoning", None)
        status, data = await _post(session, url, headers, payload)
    if status != 200:
        raise NordRouterError(_error_message(data, status))

    if not modalities and max_tokens and _hit_length(data):
        payload["max_tokens"] = max(int(payload.get("max_tokens") or max_tokens) * 2, 6000)
        if _reasons_by_default(model):
            payload["reasoning"] = {"effort": "low"}
        log.info("Ответ %s обрезан лимитом, повтор с max_tokens=%s", model, payload["max_tokens"])
        status, data = await _post(session, url, headers, payload)
        if status == 400 and "reasoning" in payload:
            payload.pop("reasoning", None)
            status, data = await _post(session, url, headers, payload)
        if status != 200:
            raise NordRouterError(_error_message(data, status))

    if not modalities:
        message = choice_message(data)
        if not assistant_text(message) and (message.get("refusal") or finish_reason(data) == "content_filter"):
            raise NordRouterError("Модель отказалась отвечать. Попробуй другую: ChatGPT, Claude или Gemini.")
    return data
