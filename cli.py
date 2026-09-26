#!/usr/bin/env python3
"""airouter — переключение между бесплатными нейронками + автопроверка кода.

Примеры:
    python cli.py ask "объясни этот кусок кода" --profile smart
    python cli.py check --fix
    python cli.py status
"""
import argparse
import sys

from diagnostics import detect_project_type, run_checks, suggest_fix
from router import Router


def _make_mcp_manager(router: Router):
    """Поднимает MCPToolManager из mcp_servers в конфиге. Понятная ошибка, если mcp не установлен/не настроен."""
    try:
        from mcp_client import MCPToolManager
    except ImportError as e:
        print(
            "MCP-функции недоступны: не установлен пакет 'mcp'. Выполните: pip install mcp",
            file=sys.stderr,
        )
        raise SystemExit(1) from e

    servers = router.cfg.get("mcp_servers") or {}
    if not servers:
        print("В config.yaml нет секции mcp_servers — добавьте хотя бы один сервер (см. config.example.yaml).",
              file=sys.stderr)
        raise SystemExit(1)

    manager = MCPToolManager(servers)
    manager.start()
    return manager


def main():
    parser = argparse.ArgumentParser(prog="airouter")
    parser.add_argument("--config", default="config.yaml", help="путь к конфигу (по умолчанию config.yaml)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ask_p = sub.add_parser("ask", help="отправить запрос выбранному профилю моделей")
    ask_p.add_argument("prompt")
    ask_p.add_argument("--profile", default="smart")
    ask_p.add_argument("-v", "--verbose", action="store_true")

    check_p = sub.add_parser("check", help="прогнать линтер/тесты, при провале — попросить фикс")
    check_p.add_argument("--path", default=".")
    check_p.add_argument("--fix", action="store_true")
    check_p.add_argument("--profile", default="smart")

    sub.add_parser("status", help="сколько запросов на каждую модель использовано сегодня")

    sub.add_parser("mcp-tools", help="показать инструменты, которые отдают серверы из mcp_servers")

    mcp_ask_p = sub.add_parser("mcp-ask", help="запрос к модели с доступом к инструментам из mcp_servers (агентный цикл)")
    mcp_ask_p.add_argument("prompt")
    mcp_ask_p.add_argument("--profile", default="design")
    mcp_ask_p.add_argument("--max-rounds", type=int, default=5)
    mcp_ask_p.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    try:
        router = Router(args.config)
    except Exception as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(1)

    if args.cmd == "ask":
        try:
            text, model, _usage = router.ask(
                args.profile, [{"role": "user", "content": args.prompt}], verbose=args.verbose
            )
        except Exception as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"[{model}]\n{text}")

    elif args.cmd == "check":
        ptype = detect_project_type(args.path)
        if not ptype:
            print("Не удалось определить тип проекта (нет package.json / pyproject.toml / requirements.txt)")
            sys.exit(1)

        ok, report = run_checks(ptype, router.cfg, args.path)
        print(report)
        print(f"\n=== РЕЗУЛЬТАТ: {'OK' if ok else 'FAIL'} ===")

        if not ok and args.fix:
            print("\nЗапрашиваю фикс у модели...\n")
            try:
                fix_text, model = suggest_fix(router, report, args.profile)
                print(f"[{model}]\n{fix_text}")
            except Exception as e:
                print(f"Не удалось получить фикс: {e}", file=sys.stderr)

        sys.exit(0 if ok else 1)

    elif args.cmd == "status":
        limit = router.limits["per_model_per_day"]
        for model, count, minute in router.status():
            print(f"{model:38s} {count}/{limit} сегодня | {minute}/{router.limits['per_model_per_minute']} за минуту")

    elif args.cmd == "mcp-tools":
        manager = _make_mcp_manager(router)
        try:
            tools = manager.list_tools_openai_schema()
            if not tools:
                print("Подключились, но ни один сервер не отдал инструментов.")
            for t in tools:
                fn = t["function"]
                print(f"- {fn['name']}: {fn['description']}")
        finally:
            manager.stop()

    elif args.cmd == "mcp-ask":
        manager = _make_mcp_manager(router)
        try:
            tools = manager.list_tools_openai_schema()
            if not tools:
                print("Ни один MCP-сервер не отдал инструментов — проверьте mcp_servers в конфиге.",
                      file=sys.stderr)
                sys.exit(1)
            try:
                text, model, _usage, _messages = router.ask_with_tools(
                    args.profile,
                    [{"role": "user", "content": args.prompt}],
                    tools=tools,
                    call_tool=manager.call_tool,
                    max_rounds=args.max_rounds,
                    verbose=args.verbose,
                )
            except Exception as e:
                print(f"Ошибка: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"[{model}]\n{text}")
        finally:
            manager.stop()


if __name__ == "__main__":
    main()
