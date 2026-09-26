"""Роутер OpenAI-compatible API с fallback по ключам и моделям."""
import json
import os
import time
from pathlib import Path

import yaml

import usage as usage_mod
from client import ApiError, RateLimitError, chat, list_models

DEFAULT_LIMITS = {"per_model_per_day": 45, "per_model_per_minute": 18}
KEY_STORE_PATH = Path.home() / ".ai_router_keys.json"


def _read_key_store():
    if not KEY_STORE_PATH.exists():
        return {}
    try:
        data = json.loads(KEY_STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_api_keys(key_env: str) -> list[str]:
    keys = []
    env_value = os.environ.get(key_env, "").strip()
    if env_value:
        keys.append(env_value)
    stored = _read_key_store().get(key_env, [])
    if isinstance(stored, str):
        stored = [stored]
    if isinstance(stored, list):
        keys.extend(str(k).strip() for k in stored if str(k).strip())
    return list(dict.fromkeys(keys))


def load_api_key(key_env: str) -> str | None:
    keys = load_api_keys(key_env)
    return keys[0] if keys else None


def save_api_key(key_env: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    keys = load_api_keys(key_env)
    save_api_keys(key_env, [*keys, value])


def save_api_keys(key_env: str, values: list[str]) -> None:
    data = _read_key_store()
    data[key_env] = list(dict.fromkeys(v.strip() for v in values if v.strip()))
    KEY_STORE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(KEY_STORE_PATH, 0o600)
    except OSError:
        pass


def mask_key(key: str) -> str:
    if len(key) <= 10:
        return "••••"
    return f"{key[:6]}…{key[-4:]}"


class Router:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}
        gw = self.cfg["gateway"]
        self.base_url = gw["base_url"]
        self.key_env = gw.get("api_key_env", "OPENROUTER_API_KEY")
        if not isinstance(self.key_env, str) or self.key_env.lower().startswith("sk-") or len(self.key_env) > 80:
            raise RuntimeError("gateway.api_key_env должен содержать имя переменной, а не API-ключ.")
        self.api_keys = load_api_keys(self.key_env)
        if not self.api_keys:
            raise RuntimeError(f"Не найден API-ключ: {self.key_env} или {KEY_STORE_PATH}")
        self.limits = {**DEFAULT_LIMITS, **self.cfg.get("daily_limits", {})}
        self.retry_count = int(self.cfg.get("router", {}).get("retries_per_endpoint", 1))
        self._key_index = 0
        self.key_stats = {k: {"ok": 0, "errors": 0, "last_error": ""} for k in self.api_keys}

    @property
    def api_key(self):
        return self.api_keys[self._key_index % len(self.api_keys)]

    def reload_keys(self):
        self.api_keys = load_api_keys(self.key_env)
        if not self.api_keys:
            raise RuntimeError("В пуле больше нет API-ключей.")
        for k in self.api_keys:
            self.key_stats.setdefault(k, {"ok": 0, "errors": 0, "last_error": ""})

    def profile_names(self) -> list[str]:
        return list(self.cfg.get("profiles", {}).keys())

    def profile_info(self, profile: str) -> dict:
        item = self.cfg.get("profiles", {}).get(profile)
        if isinstance(item, list):
            return {"description": profile, "system_prompt": "", "models": item}
        if isinstance(item, dict):
            return {
                "description": item.get("description", profile),
                "system_prompt": item.get("system_prompt", ""),
                "models": item.get("models", []),
            }
        return {"description": profile, "system_prompt": "", "models": []}

    def all_models(self) -> list[str]:
        seen = []
        for name in self.profile_names():
            for model in self.profile_info(name)["models"]:
                if model not in seen:
                    seen.append(model)
        return seen

    def _call_with_fallback(
        self,
        profile: str,
        chain: list[str],
        messages: list[dict],
        verbose: bool = False,
        on_delta=None,
        tools: list[dict] | None = None,
        tool_choice=None,
    ):
        """Общая логика перебора моделей/ключей с fallback. Возвращает (message: dict, model, usage).

        Это единственное место, где реализовано переключение между API-ключами и
        моделями при 429/401/402/403/сетевых ошибках — и ask(), и ask_with_tools()
        используют именно его, чтобы поведение fallback не разъезжалось между
        обычным чатом и MCP-циклом с инструментами.
        """
        errors = []
        for model in chain:
            if not usage_mod.can_use(model, self.limits["per_model_per_day"], self.limits["per_model_per_minute"]):
                if verbose:
                    print(f"[router] {model}: локальный лимит исчерпан")
                continue
            for key_offset in range(len(self.api_keys)):
                idx = (self._key_index + key_offset) % len(self.api_keys)
                key = self.api_keys[idx]
                for attempt in range(self.retry_count + 1):
                    try:
                        message, usage = chat(
                            self.base_url, key, model, messages,
                            on_delta=on_delta, tools=tools, tool_choice=tool_choice,
                        )
                        usage_mod.record_use(model)
                        self._key_index = idx
                        self.key_stats.setdefault(key, {"ok": 0, "errors": 0, "last_error": ""})["ok"] += 1
                        return message, model, usage
                    except RateLimitError as e:
                        errors.append(str(e))
                        self.key_stats.setdefault(key, {"ok": 0, "errors": 0, "last_error": ""})["errors"] += 1
                        self.key_stats[key]["last_error"] = str(e)
                        break
                    except ApiError as e:
                        errors.append(str(e))
                        self.key_stats.setdefault(key, {"ok": 0, "errors": 0, "last_error": ""})["errors"] += 1
                        self.key_stats[key]["last_error"] = str(e)
                        if e.status_code in {401, 402, 403} or attempt >= self.retry_count:
                            break
                        time.sleep(min(1.5 * (attempt + 1), 4))
        detail = errors[-1] if errors else "нет доступных endpoints"
        raise RuntimeError(f"Все endpoints профиля '{profile}' недоступны. Последняя ошибка: {detail}")

    def ask(self, profile: str, messages: list[dict], verbose: bool = False, on_delta=None):
        """Обычный запрос без инструментов. Контракт (text, model, usage) не менялся."""
        info = self.profile_info(profile)
        chain = info["models"]
        if not chain:
            raise ValueError(f"У профиля '{profile}' нет моделей.")
        message, model, usage = self._call_with_fallback(profile, chain, messages, verbose=verbose, on_delta=on_delta)
        return message.get("content") or "", model, usage

    def ask_with_tools(
        self,
        profile: str,
        messages: list[dict],
        tools: list[dict],
        call_tool,
        max_rounds: int = 5,
        verbose: bool = False,
        on_delta=None,
    ):
        """Агентный цикл вызова инструментов (MCP) поверх обычного fallback по ключам/моделям.

        tools — список инструментов в формате OpenAI tools[] (см.
            mcp_client.MCPToolManager.list_tools_openai_schema()).
        call_tool(name, arguments) -> str — синхронная функция, которая реально
            выполняет вызов инструмента (обычно mcp_client.MCPToolManager.call_tool)
            и возвращает текстовый результат для модели.

        Возвращает (final_text, model, usage, messages) — messages содержит полную
        историю с раундами вызова инструментов, чтобы вызывающий код мог её
        сохранить/показать пользователю.
        """
        info = self.profile_info(profile)
        chain = info["models"]
        if not chain:
            raise ValueError(f"У профиля '{profile}' нет моделей.")
        messages = list(messages)
        model = None
        usage = {}
        for _ in range(max_rounds):
            message, model, usage = self._call_with_fallback(
                profile, chain, messages, verbose=verbose, tools=tools, tool_choice="auto",
            )
            tool_calls = message.get("tool_calls") or []
            assistant_msg = {"role": "assistant", "content": message.get("content")}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                if on_delta and message.get("content"):
                    on_delta(message["content"])
                return message.get("content") or "", model, usage, messages

            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if verbose:
                    print(f"[mcp] вызов {name}({args})")
                try:
                    result_text = call_tool(name, args)
                except Exception as e:
                    result_text = f"Ошибка вызова инструмента: {e}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(result_text),
                })
        raise RuntimeError(f"Превышен лимит циклов вызова инструментов ({max_rounds}) для профиля '{profile}'.")

    def model_catalog(self, free_only=False) -> list[dict]:
        errors = []
        for key in self.api_keys:
            try:
                models = list_models(self.base_url, key)
                if free_only:
                    models = [m for m in models if _is_free(m)]
                return sorted(models, key=lambda x: str(x.get("id", "")))
            except ApiError as e:
                errors.append(str(e))
        raise RuntimeError(errors[-1] if errors else "Не удалось получить список моделей")

    def status(self):
        return usage_mod.status(self.all_models())


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        return float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False
