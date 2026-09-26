"""Минимальный OpenAI-compatible HTTP-клиент для OpenRouter с поддержкой streaming."""
import json
import requests


class RateLimitError(Exception):
    def __init__(self, model: str, message: str = "HTTP 429"):
        super().__init__(f"{model}: {message}")
        self.model = model
        self.status_code = 429


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/",
        "X-Title": "ai-router",
    }


def _raise_http(resp, model: str = "API"):
    if resp.status_code == 429:
        raise RateLimitError(model, resp.text[:300])
    if resp.status_code >= 400:
        raise ApiError(f"{model}: HTTP {resp.status_code} — {resp.text[:500]}", resp.status_code)


def chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    timeout: int = 180,
    on_delta=None,
    tools: list[dict] | None = None,
    tool_choice=None,
):
    """Выполнить chat completion.

    Возвращает (message: dict, usage: dict), где message — это полное сообщение
    ассистента (ключи "content" и, если модель попросила вызвать инструмент,
    "tool_calls"). При tools стриминг отключается: дельты tool_calls нельзя
    надёжно собрать по частям без риска повредить JSON аргументов, поэтому
    запрос с инструментами всегда уходит без stream=True, даже если передан
    on_delta (он в этом случае будет вызван один раз с уже полным текстом,
    если текст вообще есть).
    """
    payload = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    try:
        if on_delta is None or tools:
            resp = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=_headers(api_key), json=payload, timeout=timeout,
            )
            _raise_http(resp, model)
            try:
                data = resp.json()
                message = data["choices"][0]["message"] or {}
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise ApiError(f"{model}: неожиданный формат ответа: {resp.text[:500]}") from e
            if on_delta and message.get("content"):
                on_delta(message["content"])
            return message, data.get("usage", {})

        payload["stream"] = True
        with requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={**_headers(api_key), "Accept": "text/event-stream"},
            json=payload, timeout=(15, timeout), stream=True,
        ) as resp:
            _raise_http(resp, model)
            chunks = []
            usage = {}
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    chunks.append(delta)
                    on_delta(delta)
            return {"role": "assistant", "content": "".join(chunks)}, usage
    except requests.exceptions.RequestException as e:
        raise ApiError(f"{model}: сетевая ошибка — {e}") from e


def list_models(base_url: str, api_key: str, timeout: int = 30) -> list[dict]:
    """Возвращает модели, которые реально видит текущий API-ключ."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", headers=_headers(api_key), timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise ApiError(f"Не удалось получить список моделей: {e}") from e
    _raise_http(resp, "Список моделей")
    try:
        data = resp.json().get("data", [])
    except (ValueError, AttributeError) as e:
        raise ApiError("Список моделей: сервер вернул не JSON") from e
    return [x for x in data if isinstance(x, dict) and x.get("id")]
