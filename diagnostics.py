"""Проверка проекта и цикл: сгенерировать изменения -> применить -> проверить -> исправить."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".ai-router-backup"}
TEXT_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".md", ".txt", ".html", ".css", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".sh", ".sql", ".toml", ".ini", ".cfg"}


def detect_project_type(path: str = ".") -> str | None:
    if os.path.exists(os.path.join(path, "package.json")):
        return "node"
    if os.path.exists(os.path.join(path, "pyproject.toml")) or os.path.exists(os.path.join(path, "requirements.txt")):
        return "python"
    return None


def _run(cmd: list[str], cwd: str = ".", timeout: int = 600):
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, (result.stdout + result.stderr).strip(), False
    except FileNotFoundError:
        return False, f"Команда не найдена: {' '.join(cmd)}", True
    except subprocess.TimeoutExpired:
        return False, f"Таймаут выполнения: {' '.join(cmd)}", False


def run_checks(project_type: str, cfg: dict, path: str = "."):
    """Запускает встроенную синтаксическую проверку + настроенные static/lint/test."""
    steps = cfg.get("diagnostics", {}).get(project_type, {})
    report = []
    all_ok = True
    blocked = False

    if project_type == "python":
        ok, out, missing = _run(["python", "-m", "compileall", "-q", "."], path)
        report.append(f"--- syntax / compile [{('OK' if ok else 'FAIL')}] ---\n{out}")
        all_ok &= ok
        blocked |= missing
    elif project_type == "node" and os.path.exists(os.path.join(path, "tsconfig.json")):
        ok, out, missing = _run(["npx", "tsc", "--noEmit"], path)
        report.append(f"--- typecheck [{('OK' if ok else 'FAIL')}] ---\n{out}")
        all_ok &= ok
        blocked |= missing

    for name in ("dead_code", "lint", "test"):
        cmd = steps.get(name)
        if not cmd:
            continue
        ok, output, missing = _run(cmd, cwd=path)
        status = "WARN / TOOL MISSING" if missing else ("OK" if ok else "FAIL")
        report.append(f"--- {name} ({' '.join(cmd)}) [{status}] ---\n{output}")
        if not missing:
            all_ok &= ok
        else:
            blocked = True

    if not report:
        return True, "Проверки для этого типа проекта не настроены."
    if blocked and all_ok:
        return False, "\n\n".join(report) + "\n\n=== Нужны инструменты для полной проверки ==="
    return all_ok, "\n\n".join(report)


def read_workspace(path: str, max_chars: int = 350_000) -> str:
    root = Path(path).resolve()
    parts, total = [], 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            p = Path(base) / name
            if p.suffix.lower() not in TEXT_EXTS or p.stat().st_size > 200_000:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            rel = p.relative_to(root).as_posix()
            chunk = f"\n--- FILE: {rel} ---\n{text}\n"
            if total + len(chunk) > max_chars:
                return "".join(parts) + "\n--- CONTEXT TRUNCATED ---\n"
            parts.append(chunk)
            total += len(chunk)
    return "".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("ИИ не вернул JSON-план изменений.")
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"Не удалось разобрать план изменений: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("changes"), list):
        raise ValueError("В ответе ИИ нет массива changes.")
    return data


def generate_changes(router, profile: str, project: str, task: str, extra: str = "", system_prompt_override: str | None = None, on_delta=None):
    info = router.profile_info(profile)
    system = system_prompt_override if system_prompt_override is not None else info.get("system_prompt", "")
    system += """
\nТы работаешь как coding agent. Верни ТОЛЬКО JSON без markdown:
{"summary":"кратко","questions":[],"options":[],"changes":[{"path":"relative/path","content":"полное новое содержимое файла"}],"delete":[]}
Правила: пути только относительные внутри workspace; не меняй секреты, .git и зависимости; не создавай лишние файлы; content должен быть полным содержимым изменяемого файла; если задача неоднозначна и без уточнения можно сделать разные несовместимые вещи — заполни questions и/или options, а changes оставь пустым; если есть 2–3 разумных подхода — опиши их в options и дождись выбора; не изменяй файлы в таком случае; сначала используй только файлы из контекста.
"""
    user = f"Задача:\n{task}\n\nWorkspace:\n{read_workspace(project)}"
    if extra:
        user += f"\n\nДополнительная информация:\n{extra}"
    text, model, usage = router.ask(profile, [{"role": "system", "content": system}, {"role": "user", "content": user}], on_delta=on_delta)
    return _extract_json(text), model, usage


def apply_changes(project: str, plan: dict):
    root = Path(project).resolve()
    changes = plan.get("changes", [])
    deletes = plan.get("delete", [])
    if not changes and not deletes:
        return [], "ИИ не предложил изменений."
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root / ".ai-router-backup" / stamp
    changed = []
    for item in changes:
        rel = str(item.get("path", "")).replace("\\", "/").strip()
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError(f"Небезопасный путь в плане ИИ: {rel!r}")
        if "content" not in item:
            raise ValueError(f"В изменении {rel!r} отсутствует content.")
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"Путь выходит за workspace: {rel}")
        if target.exists():
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(item["content"]), encoding="utf-8")
        changed.append(rel)
    for rel in deletes:
        rel = str(rel).replace("\\", "/").strip()
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError(f"Небезопасный путь удаления: {rel!r}")
        target = (root / rel).resolve()
        if root not in target.parents:
            raise ValueError(f"Путь удаления выходит за workspace: {rel}")
        if target.exists() and target.is_file():
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            target.unlink()
            changed.append(f"DELETE {rel}")
    return changed, str(backup)


def suggest_fix(router, report: str, profile: str = "smart", project: str | None = None):
    context = read_workspace(project) if project else ""
    messages = [
        {"role": "system", "content": "Ты исправляешь код после реального запуска проверок. Верни конкретный минимальный patch/diff или точные изменения файлов. Не выдумывай ошибки."},
        {"role": "user", "content": f"Результат проверок:\n{report}\n\nТекущий код:\n{context}"},
    ]
    text, model, _usage = router.ask(profile, messages)
    return text, model


def agent_edit_and_verify(router, profile: str, project: str, task: str, max_cycles: int = 3, system_prompt_override: str | None = None, on_event=None, on_delta=None):
    """Генерирует код, применяет, запускает проверки и при ошибках делает до max_cycles автофиксов."""
    events = []
    extra = ""

    def emit(event):
        events.append(event)
        if on_event:
            on_event(event)
    for cycle in range(1, max_cycles + 1):
        emit({"type": "phase", "cycle": cycle, "phase": "generate", "message": f"Генерирую изменения · цикл {cycle}/{max_cycles}"})
        plan, model, _usage = generate_changes(router, profile, project, task, extra, system_prompt_override, on_delta=on_delta)
        questions = plan.get("questions") or []
        options = plan.get("options") or []
        if questions or options:
            emit({"type": "clarify", "cycle": cycle, "model": model, "questions": questions, "options": options, "summary": plan.get("summary", "")})
            return events
        changed, backup = apply_changes(project, plan)
        emit({"type": "changes", "cycle": cycle, "model": model, "summary": plan.get("summary", ""), "files": changed, "backup": backup})
        if not changed:
            emit({"type": "done", "ok": True, "report": "Изменений не потребовалось."})
            return events
        ptype = detect_project_type(project)
        if not ptype:
            emit({"type": "done", "ok": True, "report": "Изменения применены; тип проекта не определён, поэтому автоматические проверки не запущены."})
            return events
        emit({"type": "phase", "cycle": cycle, "phase": "check", "message": "Проверяю syntax/typecheck · dead-code · lint · tests"})
        ok, report = run_checks(ptype, router.cfg, project)
        emit({"type": "check", "cycle": cycle, "ok": ok, "report": report})
        if "TOOL MISSING" in report:
            emit({"type": "done", "ok": False, "report": "Часть проверок не запущена: установи указанные инструменты (например, ruff/pytest/eslint), затем повтори проверку."})
            return events
        if ok:
            emit({"type": "done", "ok": True, "report": "Код изменён и проверки прошли."})
            return events
        extra = f"Предыдущий цикл изменил файлы: {changed}\nПроверки завершились ошибками:\n{report}\nИсправь именно эти проблемы и снова верни JSON изменений."
    emit({"type": "done", "ok": False, "report": f"После {max_cycles} циклов проверки всё ещё не пройдены."})
    return events
