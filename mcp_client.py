"""Мост между синхронным AI Router и MCP-серверами (например, penpot-mcp-server).

MCP Python SDK асинхронный и построен на anyio (structured concurrency), а роутер
и GUI (PySide6) — синхронные. Здесь поднимается отдельный фоновый поток с одной
постоянной async-задачей (_serve), которая держит открытыми все MCP-сессии и
обрабатывает запросы на вызов инструментов через очередь. Наружу отдаются обычные
блокирующие функции: start(), list_tools_openai_schema() и call_tool().

Важно: все обращения к сессии (list_tools, call_tool) выполняются ИЗНУТРИ той же
самой корутины, что открывала подключение. anyio привязывает cancel scope к дереву
задач, в котором он был создан — вызов session.call_tool() из отдельной задачи,
запущенной через run_coroutine_threadsafe(), ловит "Cancelled via cancel scope",
потому что такая задача не является потомком исходной. Поэтому здесь используется
паттерн "актор": один долгоживущий task, очередь запросов и concurrent.futures.Future
для возврата результата в вызывающий (синхронный) поток.

Это не влияет на переключение API-ключей/моделей в router.py — инструменты MCP
просто передаются в router.ask_with_tools(), который использует тот же fallback
по ключам, что и обычный router.ask().

Поддерживаются два транспорта MCP-сервера (задаются в config.yaml -> mcp_servers):
  - "http"  — потоковый HTTP. Так работает, например, self-hosted
              penpot-mcp-server (ancrz/penpot-mcp-server): он всегда запущен
              отдельным Docker-контейнером на своём порту (обычно :8787/mcp),
              и клиенту достаточно знать URL — процесс никто не запускает.
  - "stdio" — локальный процесс, который AI Router сам запускает и
              останавливает вместе со своим циклом инструментов.

Пакет "mcp" за последние версии менял часть внутреннего API (имя функции
streamable-http клиента, регистр полей Tool/CallToolResult). Код ниже
поддерживает старые и новые варианты через try/except и getattr, чтобы не
зависеть от того, какая именно версия окажется установлена через pip.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from contextlib import AsyncExitStack
from typing import Any

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


class MCPError(Exception):
    """Ошибка уровня MCP-моста (подключение, конфиг, неизвестный инструмент и т.п.)."""


def _get_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Достаёт первый существующий атрибут из списка имён (snake_case/camelCase)."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


async def _open_http_transport(stack: AsyncExitStack, url: str, headers: dict[str, str] | None):
    """Открывает streamable-HTTP транспорт, подстраиваясь под версию пакета mcp."""
    try:
        # Новый API (mcp >= 2.0): streamable_http_client(url, http_client=...) -> (read, write)
        from mcp.client.streamable_http import streamable_http_client

        http_client = None
        if headers:
            import httpx2

            http_client = httpx2.AsyncClient(headers=headers)
        cm = streamable_http_client(url, http_client=http_client)
        read, write = await stack.enter_async_context(cm)
        return read, write
    except ImportError:
        # Старый API (mcp 1.x): streamablehttp_client(url, headers=...) -> (read, write, session_id)
        from mcp.client.streamable_http import streamablehttp_client

        cm = streamablehttp_client(url, headers=headers or None)
        read, write, _ = await stack.enter_async_context(cm)
        return read, write


class MCPToolManager:
    """Держит подключения к одному или нескольким MCP-серверам и даёт синхронный доступ к их инструментам."""

    def __init__(self, servers_config: dict[str, dict]):
        if not MCP_AVAILABLE:
            raise MCPError("Пакет 'mcp' не установлен. Выполните: pip install mcp")
        self.servers_config = servers_config or {}
        self._tools_index: dict[str, str] = {}  # имя инструмента -> имя сервера
        self._raw_tools: dict[str, list] = {}  # имя сервера -> список mcp.types.Tool
        self._sessions: dict[str, ClientSession] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._request_queue: asyncio.Queue | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None

    # ---------- запуск/остановка фонового потока с постоянной async-задачей ----------

    def start(self, timeout: float = 30.0) -> None:
        """Поднимает фоновый поток, подключается ко всем настроенным MCP-серверам и остаётся жить."""
        if self._thread and self._thread.is_alive():
            return
        if not self.servers_config:
            raise MCPError("В конфиге нет ни одного сервера в mcp_servers.")
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._run_loop, name="mcp-client", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise MCPError(f"MCP-серверы не ответили за {timeout:.0f} секунд при запуске.")
        if self._start_error:
            raise self._start_error

    def stop(self, timeout: float = 10.0) -> None:
        if self._loop and self._loop.is_running() and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None
        self._sessions.clear()
        self._raw_tools.clear()
        self._tools_index.clear()

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._sessions)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            if not self._ready.is_set():
                # Соединение упало до того, как успели сообщить об успехе — сообщаем об ошибке.
                self._start_error = self._start_error or MCPError("MCP-сервер завершился до подключения.")
                self._ready.set()
            loop.close()

    async def _serve(self) -> None:
        """Единственная долгоживущая корутина: открывает все сессии и обрабатывает очередь вызовов.

        Всё выполняется в одном task — это принципиально для anyio-based транспортов
        (см. docstring модуля про cancel scope).
        """
        self._request_queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for name, cfg in self.servers_config.items():
                    await self._connect_one(stack, name, cfg)
                self._ready.set()  # сообщаем основному потоку об успехе только после полного подключения

                while not self._stop_event.is_set():
                    try:
                        job = await asyncio.wait_for(self._request_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    await self._handle_job(job)
        except BaseException as e:  # noqa: BLE001 — включая ExceptionGroup из anyio task group
            if not self._ready.is_set():
                self._start_error = MCPError(f"Не удалось подключиться к MCP-серверам: {e!r}")
                self._ready.set()
            # если уже были готовы и работали — просто даём потоку тихо завершиться

    async def _connect_one(self, stack: AsyncExitStack, name: str, cfg: dict) -> None:
        transport = cfg.get("transport", "http")
        if transport == "http":
            url = cfg.get("url")
            if not url:
                raise MCPError(f"mcp_servers.{name}: не указан 'url' для transport=http")
            read, write = await _open_http_transport(stack, url, cfg.get("headers"))
        elif transport == "stdio":
            params = StdioServerParameters(
                command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            raise MCPError(f"mcp_servers.{name}: неизвестный transport '{transport}' (ожидается http/stdio)")

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        # Известная гонка в streamable-HTTP транспорте mcp SDK: сразу после initialize()
        # list_tools() может вернуть пустой список, хотя сервер уже готов. Пара коротких
        # повторов решает проблему без усложнения остального кода.
        tools_result = await session.list_tools()
        for _ in range(4):
            if tools_result.tools:
                break
            await asyncio.sleep(0.25)
            tools_result = await session.list_tools()

        self._sessions[name] = session
        self._raw_tools[name] = tools_result.tools
        for tool in tools_result.tools:
            self._tools_index[tool.name] = name

    async def _handle_job(self, job: tuple[str, dict, concurrent.futures.Future]) -> None:
        name, arguments, result_future = job
        try:
            server_name = self._tools_index.get(name)
            if server_name is None:
                known = ", ".join(sorted(self._tools_index)) or "(нет подключённых инструментов)"
                raise MCPError(f"Инструмент '{name}' не найден. Доступны: {known}")
            session = self._sessions[server_name]
            result = await session.call_tool(name, arguments)
        except BaseException as e:  # noqa: BLE001
            if not result_future.done():
                result_future.set_exception(e)
            return
        if not result_future.done():
            result_future.set_result(result)

    # ---------- синхронный API для router.py ----------

    def list_tools_openai_schema(self) -> list[dict]:
        """Инструменты всех подключённых серверов в формате OpenAI tools[] (для передачи в client.chat)."""
        schema = []
        for tools in self._raw_tools.values():
            for tool in tools:
                params = _get_attr(tool, "input_schema", "inputSchema", default={"type": "object", "properties": {}})
                schema.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": params,
                    },
                })
        return schema

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 120.0) -> str:
        """Синхронно вызывает инструмент по имени, возвращает текстовый результат для модели."""
        if not self.is_running or self._loop is None or self._request_queue is None:
            raise MCPError("MCP-клиент не запущен. Вызовите start() перед call_tool().")

        result_future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._request_queue.put_nowait, (name, arguments, result_future))
        result = result_future.result(timeout=timeout)  # CallToolResult или исключение из _handle_job

        parts = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        text_out = "\n".join(parts) if parts else "(инструмент не вернул текст)"

        is_error = _get_attr(result, "is_error", "isError", default=False)
        return f"ОШИБКА инструмента: {text_out}" if is_error else text_out
