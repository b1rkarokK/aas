"""Локальная статистика запросов по моделям: день + последняя минута."""
import json
import time
from pathlib import Path

STATE_FILE = Path.home() / ".ai_router_usage.json"


def _load():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}


def _save(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _day_key(): return time.strftime("%Y-%m-%d", time.gmtime())


def _entry(state, model):
    day = _day_key(); m = state.setdefault(model, {"day": day, "count_day": 0, "minute_ts": []})
    if m.get("day") != day: m["day"], m["count_day"] = day, 0
    m["minute_ts"] = [t for t in m.get("minute_ts", []) if time.time() - t < 60]
    return m


def can_use(model, per_day, per_minute):
    state = _load(); m = _entry(state, model); ok = m["count_day"] < per_day and len(m["minute_ts"]) < per_minute; _save(state); return ok


def record_use(model):
    state = _load(); m = _entry(state, model); m["count_day"] += 1; m["minute_ts"].append(time.time()); _save(state)


def status(models):
    state = _load(); day = _day_key(); now = time.time(); rows = []
    for model in models:
        m = state.get(model, {}); count = m.get("count_day", 0) if m.get("day") == day else 0
        minute = len([t for t in m.get("minute_ts", []) if now - t < 60])
        rows.append((model, count, minute))
    return rows
