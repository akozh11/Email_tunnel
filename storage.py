"""
storage.py — общий слой данных Email Tunnel.

Им пользуются оба независимых процесса (main.py — почтовый цикл, bot.py — Telegram-панель),
поэтому все изменения идут через один файловый замок (fcntl) и атомарную запись.

Файлы в каталоге data/:
  • allowed_senders.json   — доверенные адреса;
  • requests_history.json  — история запросов по каждому адресу;
  • stats.json             — счётчики по дням, по адресам и общие (для раздела «Статистика»);
  • security.json          — баны, предупреждения и журнал инцидентов.

Необязательные настройки в settings.py (есть значения по умолчанию):
  SECURITY_ENABLED = True     # проверка запросов ИИ-классификатором
  BAN_CONFIDENCE = 0.75       # «опасно» с такой уверенностью и выше — бан сразу
  STRIKES_LIMIT = 3           # сколько предупреждений ведут к бану
  STRIKE_WINDOW_DAYS = 30     # за какой срок считаются предупреждения
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import settings

logger = logging.getLogger("storage")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

SENDERS_FILE = DATA_DIR / "allowed_senders.json"
HISTORY_FILE = DATA_DIR / "requests_history.json"
STATS_FILE = DATA_DIR / "stats.json"
SECURITY_FILE = DATA_DIR / "security.json"
LOCK_FILE = DATA_DIR / ".storage.lock"

SETTINGS_FILE = BASE_DIR / "settings.py"
ALLOWED_SENDERS_RE = re.compile(r"ALLOWED_SENDERS\s*=\s*\[[^\]]*\]", re.DOTALL)

SECURITY_ENABLED = bool(getattr(settings, "SECURITY_ENABLED", True))
BAN_CONFIDENCE = float(getattr(settings, "BAN_CONFIDENCE", 0.75))
STRIKES_LIMIT = int(getattr(settings, "STRIKES_LIMIT", 3))
STRIKE_WINDOW_DAYS = int(getattr(settings, "STRIKE_WINDOW_DAYS", 30))

MAX_HISTORY_PER_SENDER = 300
MAX_INCIDENTS = 200
DAILY_KEEP_DAYS = 120

CATEGORY_LABELS = {
    "weapons_explosives": "Оружие / взрывчатка",
    "malware_hacking": "Вредоносный код / взлом",
    "child_safety": "Безопасность детей",
    "fraud_phishing": "Мошенничество / фишинг",
    "violence_threats": "Насилие / угрозы",
    "drugs_synthesis": "Изготовление наркотиков",
    "jailbreak_attempt": "Обход ограничений ИИ",
    "harassment": "Травля / оскорбления",
    "self_harm": "Кризисная ситуация",
    "model_blocked": "Заблокировано фильтрами Gemini",
    "manual": "Решение владельца",
    "other": "Другое",
}


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category or "", category or "—")


# ──────────────────────────── Низкоуровневые операции ────────────────────────────

@contextmanager
def _locked():
    """Межпроцессный замок. Не вкладывать друг в друга — будет взаимная блокировка."""
    with open(LOCK_FILE, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Не удалось прочитать %s: %s", path, e)
        return default


def _write(path: Path, data) -> None:
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def _mutate(path: Path, default, fn):
    """Читает файл, применяет fn(data) под замком и атомарно сохраняет. Возвращает результат fn."""
    with _locked():
        data = _read(path, default)
        result = fn(data)
        _write(path, data)
    return result


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sender_id(email: str) -> str:
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()[:12]


# ──────────────────────────── Доверенные адреса ────────────────────────────

def load_senders() -> dict:
    """{sender_id: {"email", "note", "added_at", "added_by"}}"""
    return _read(SENDERS_FILE, {})


def get_allowed_senders_list() -> list[str]:
    return [item["email"] for item in load_senders().values()]


def get_note(email: str) -> str:
    item = load_senders().get(sender_id(email))
    return (item or {}).get("note", "")


def email_by_sid(sid: str) -> str | None:
    """Находит e-mail по короткому id — среди доверенных и среди забаненных."""
    item = load_senders().get(sid)
    if item:
        return item["email"]
    for email in get_bans():
        if sender_id(email) == sid:
            return email
    return None


def _format_allowed_senders_block(emails: list[str]) -> str:
    if not emails:
        return "ALLOWED_SENDERS = [\n]"
    body = ",\n".join(f'    "{e}"' for e in emails)
    return f"ALLOWED_SENDERS = [\n{body},\n]"


def _sync_settings_allowed_senders() -> None:
    """Зеркалирует список в ALLOWED_SENDERS внутри settings.py (best-effort, остальное не трогает)."""
    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Не удалось прочитать settings.py для синхронизации: %s", e)
        return

    if not ALLOWED_SENDERS_RE.search(text):
        logger.warning("В settings.py не найден блок ALLOWED_SENDERS — синхронизация пропущена")
        return

    emails = sorted(get_allowed_senders_list())
    new_text = ALLOWED_SENDERS_RE.sub(lambda _m: _format_allowed_senders_block(emails), text, count=1)
    if new_text == text:
        return

    try:
        tmp_path = SETTINGS_FILE.with_suffix(".py.tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(SETTINGS_FILE)
    except OSError as e:
        logger.warning("Не удалось записать settings.py: %s", e)
        return
    logger.info("settings.py: ALLOWED_SENDERS синхронизирован (%d адрес(ов))", len(emails))


def add_sender(email: str, note: str, added_by: int) -> bool:
    """Возвращает False, если такой адрес уже есть."""
    email = email.lower().strip()
    sid = sender_id(email)

    def fn(data):
        if sid in data:
            return False
        data[sid] = {"email": email, "note": note or "", "added_at": now_iso(), "added_by": added_by}
        return True

    added = _mutate(SENDERS_FILE, {}, fn)
    if added:
        _sync_settings_allowed_senders()
    return added


def remove_sender(sid: str) -> str | None:
    def fn(data):
        item = data.pop(sid, None)
        return item["email"] if item else None

    email = _mutate(SENDERS_FILE, {}, fn)
    if email:
        _sync_settings_allowed_senders()
    return email


def update_note(sid: str, note: str) -> bool:
    def fn(data):
        item = data.get(sid)
        if item is None:
            return False
        item["note"] = note or ""
        return True

    return _mutate(SENDERS_FILE, {}, fn)


# ──────────────────────────── История запросов ────────────────────────────

def load_history() -> dict:
    """{email: [{"timestamp", "subject", "request", "response", "status", "category", "latency_ms"}]}"""
    return _read(HISTORY_FILE, {})


def log_request(email: str, subject: str, request_text: str, response_text: str,
                status: str = "ok", category: str = "", latency_ms: int = 0) -> None:
    """status: ok | error | blocked."""
    email = email.lower().strip()

    def fn(history):
        entries = history.setdefault(email, [])
        entries.append({
            "timestamp": now_iso(),
            "subject": (subject or "")[:200],
            "request": (request_text or "")[:2000],
            "response": (response_text or "")[:2000],
            "status": status,
            "category": category,
            "latency_ms": int(latency_ms),
        })
        if len(entries) > MAX_HISTORY_PER_SENDER:
            del entries[: len(entries) - MAX_HISTORY_PER_SENDER]

    _mutate(HISTORY_FILE, {}, fn)


# ──────────────────────────── Статистика ────────────────────────────

_COUNTERS = ("requests", "ok", "errors", "blocked", "dropped", "flagged", "images", "latency_ms", "latency_n")


def _empty() -> dict:
    return {key: 0 for key in _COUNTERS}


def _apply(bucket: dict, outcome: str, images: int, latency_ms: int, flagged: bool) -> None:
    for key in _COUNTERS:
        bucket.setdefault(key, 0)
    if outcome == "dropped":
        bucket["dropped"] += 1
        return
    bucket["requests"] += 1
    if outcome == "ok":
        bucket["ok"] += 1
        bucket["latency_ms"] += int(latency_ms)
        bucket["latency_n"] += 1
    elif outcome == "error":
        bucket["errors"] += 1
    elif outcome == "blocked":
        bucket["blocked"] += 1
    bucket["images"] += images
    if flagged:
        bucket["flagged"] += 1


def record_stat(email: str, outcome: str, images: int = 0, latency_ms: int = 0, flagged: bool = False) -> None:
    """
    outcome: ok — ответ отправлен; error — ошибка ИИ/отправки; blocked — запрос отклонён
    из-за безопасности; dropped — письмо забаненного адреса отброшено без обработки.
    """
    email = email.lower().strip()
    now = datetime.now()
    day = now.date().isoformat()

    def fn(st):
        daily = st.setdefault("daily", {})
        day_bucket = daily.setdefault(day, _empty())
        totals = st.setdefault("totals", _empty())
        sender = st.setdefault("senders", {}).setdefault(email, {**_empty(), "first_seen": now_iso(), "last_seen": ""})
        hours = st.setdefault("hours", [0] * 24)

        for bucket in (day_bucket, totals, sender):
            _apply(bucket, outcome, images, latency_ms, flagged)

        if outcome != "dropped":
            by_sender = day_bucket.setdefault("by_sender", {})
            by_sender[email] = by_sender.get(email, 0) + 1
            hours[now.hour] += 1
            sender["last_seen"] = now_iso()

        cutoff = (now.date() - timedelta(days=DAILY_KEEP_DAYS)).isoformat()
        for old in [d for d in daily if d < cutoff]:
            del daily[old]

    _mutate(STATS_FILE, {}, fn)


def get_stats(days: int | None, chart_days: int = 7) -> dict:
    """
    days: 1 — сегодня, N — последние N дней (включая сегодня), None — за всё время.
    Возвращает: totals, by_sender {email: запросов}, series [(дата, запросов)], hours, active.
    """
    st = _read(STATS_FILE, {})
    daily = st.get("daily", {})
    today = date.today()

    if days is None:
        totals = {**_empty(), **st.get("totals", {})}
        by_sender = {e: v.get("requests", 0) for e, v in st.get("senders", {}).items()}
    else:
        cutoff = (today - timedelta(days=days - 1)).isoformat()
        totals = _empty()
        by_sender = {}
        for day_str, bucket in daily.items():
            if day_str < cutoff:
                continue
            for key in _COUNTERS:
                totals[key] += bucket.get(key, 0)
            for email, n in bucket.get("by_sender", {}).items():
                by_sender[email] = by_sender.get(email, 0) + n

    series = []
    for offset in range(chart_days - 1, -1, -1):
        day_str = (today - timedelta(days=offset)).isoformat()
        series.append((day_str, daily.get(day_str, {}).get("requests", 0)))

    return {
        "totals": totals,
        "by_sender": by_sender,
        "series": series,
        "hours": st.get("hours", [0] * 24),
        "active": sum(1 for n in by_sender.values() if n > 0),
    }


def get_sender_stats(email: str) -> dict:
    st = _read(STATS_FILE, {})
    data = st.get("senders", {}).get(email.lower().strip(), {})
    return {**_empty(), "first_seen": "", "last_seen": "", **data}


# ──────────────────────────── Безопасность: баны, предупреждения, инциденты ────────────────────────────

def _sec_defaults(sec: dict) -> dict:
    sec.setdefault("bans", {})
    sec.setdefault("strikes", {})
    sec.setdefault("incidents", [])
    return sec


def _sec_read() -> dict:
    return _sec_defaults(_read(SECURITY_FILE, {}))


def get_bans() -> dict:
    """{email: {"banned_at", "reason", "category", "preview", "by"}}"""
    return _sec_read()["bans"]


def get_banned_set() -> set[str]:
    return set(get_bans())


def is_banned(email: str) -> bool:
    return email.lower().strip() in get_bans()


def ban_user(email: str, reason: str, category: str = "", preview: str = "", by: str = "ai") -> bool:
    """Возвращает False, если адрес уже забанен."""
    email = email.lower().strip()

    def fn(sec):
        _sec_defaults(sec)
        if email in sec["bans"]:
            return False
        sec["bans"][email] = {
            "banned_at": now_iso(),
            "reason": (reason or "")[:300],
            "category": category,
            "preview": (preview or "")[:300],
            "by": by,
        }
        return True

    return _mutate(SECURITY_FILE, {}, fn)


def unban_user(email: str) -> bool:
    """Снимает бан и обнуляет предупреждения."""
    email = email.lower().strip()

    def fn(sec):
        _sec_defaults(sec)
        sec["strikes"].pop(email, None)
        return sec["bans"].pop(email, None) is not None

    return _mutate(SECURITY_FILE, {}, fn)


def _active_strikes(entries: list, window_days: int) -> int:
    cutoff = datetime.now() - timedelta(days=window_days)
    count = 0
    for entry in entries:
        try:
            if datetime.fromisoformat(entry["ts"]) >= cutoff:
                count += 1
        except (KeyError, ValueError):
            continue
    return count


def add_strike(email: str, category: str, reason: str) -> int:
    """Добавляет предупреждение, возвращает число активных предупреждений."""
    email = email.lower().strip()

    def fn(sec):
        _sec_defaults(sec)
        entries = sec["strikes"].setdefault(email, [])
        entries.append({"ts": now_iso(), "category": category, "reason": (reason or "")[:300]})
        return _active_strikes(entries, STRIKE_WINDOW_DAYS)

    return _mutate(SECURITY_FILE, {}, fn)


def get_strike_count(email: str) -> int:
    sec = _sec_read()
    return _active_strikes(sec["strikes"].get(email.lower().strip(), []), STRIKE_WINDOW_DAYS)


def count_active_strikes() -> int:
    sec = _sec_read()
    return sum(_active_strikes(entries, STRIKE_WINDOW_DAYS) for entries in sec["strikes"].values())


def clear_strikes(email: str) -> None:
    email = email.lower().strip()

    def fn(sec):
        _sec_defaults(sec)
        sec["strikes"].pop(email, None)

    _mutate(SECURITY_FILE, {}, fn)


def add_incident(email: str, verdict: str, category: str, confidence: float,
                 reason: str, action: str, preview: str = "") -> None:
    """action: ban | block | strike | flag."""
    def fn(sec):
        _sec_defaults(sec)
        sec["incidents"].append({
            "ts": now_iso(),
            "email": email.lower().strip(),
            "verdict": verdict,
            "category": category,
            "confidence": round(float(confidence), 2),
            "reason": (reason or "")[:300],
            "action": action,
            "preview": (preview or "")[:300],
        })
        if len(sec["incidents"]) > MAX_INCIDENTS:
            del sec["incidents"][: len(sec["incidents"]) - MAX_INCIDENTS]

    _mutate(SECURITY_FILE, {}, fn)


def get_incidents() -> list[dict]:
    """Новые сверху."""
    return list(reversed(_sec_read()["incidents"]))
