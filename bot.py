"""
bot.py — Telegram-панель владельца Email Tunnel (pyTelegramBotAPI / telebot).

Разделы (кнопки внизу экрана):
  📊 Статистика   — запросы, успешность, ошибки, отказы, время ответа, график по дням, топ адресов;
  📋 Адреса       — доверенные адреса, карточки со статистикой и историей, смена подписи, бан/разбан;
  🛡 Безопасность — забаненные, предупреждения, журнал инцидентов;
  ➕ Добавить     — или просто пришлите «email Подпись».

Уведомления об автобане приходят сюда же от main.py (security.py) с кнопками действий.
Данные общие с main.py через storage.py. Запускать отдельным процессом:  python bot.py
"""

from __future__ import annotations

import html
import logging
from datetime import date, datetime

import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import storage
from settings import OWNER_ID, TG_BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("senders_bot")

SENDERS_PAGE_SIZE = 8
HISTORY_PAGE_SIZE = 5
INCIDENTS_PAGE_SIZE = 5
TOP_PAGE_SIZE = 8
PREVIEW_LEN = 160
DETAIL_LEN = 1800

PERIODS = [("today", "Сегодня"), ("7", "7 дн"), ("30", "30 дн"), ("all", "Всё")]
PERIOD_DAYS = {"today": 1, "7": 7, "30": 30, "all": None}

STATUS_ICONS = {"ok": "✅", "error": "⚠️", "blocked": "🚫"}
ACTION_ICONS = {"ban": "🚫", "block": "⛔", "strike": "⚠️", "flag": "💬"}
ACTION_NAMES = {"ban": "бан", "block": "отклонён", "strike": "предупреждение", "flag": "отмечен"}


# ──────────────────────────── Вспомогательное ────────────────────────────

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return ts or "—"


def _shorten(text: str, limit: int = PREVIEW_LEN) -> str:
    text = " ".join((text or "").split())
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def _pct(part: int, whole: int) -> str:
    return f"{part * 100 // whole}%" if whole else "—"


def _latency(totals: dict) -> str:
    n = totals.get("latency_n", 0)
    return f"{totals['latency_ms'] / n / 1000:.1f} с" if n else "—"


def _name(email: str) -> str:
    return storage.get_note(email) or email


def _chart(series: list[tuple[str, int]]) -> str:
    peak = max((n for _, n in series), default=0)
    lines = []
    for day_str, n in series:
        label = date.fromisoformat(day_str).strftime("%d.%m")
        bar = "█" * (round(n / peak * 10) if peak else 0)
        if n and not bar:
            bar = "▏"
        lines.append(f"{label} {bar} {n}")
    return "<pre>" + "\n".join(lines) + "</pre>"


# ──────────────────────────── Экраны ────────────────────────────

def render_stats(period: str) -> tuple[str, InlineKeyboardMarkup]:
    days = PERIOD_DAYS[period]
    label = dict(PERIODS)[period]
    stats = storage.get_stats(days, chart_days=7 if days in (1, 7) else 14)
    t = stats["totals"]
    requests = t["requests"]

    lines = [f"<b>📊 Статистика · {label}</b>", ""]
    if requests == 0 and t["dropped"] == 0:
        lines.append("За этот период запросов не было.")
    else:
        lines += [
            f"📨 Запросов: <b>{requests}</b>",
            f"✅ Успешно: {t['ok']} ({_pct(t['ok'], requests)})",
            f"⚠️ Ошибок: {t['errors']}",
            f"🚫 Отклонено по безопасности: {t['blocked']}",
            f"🗑 Отброшено у забаненных: {t['dropped']}",
            f"💬 Чувствительных: {t['flagged']}",
            f"🖼 Фото: {t['images']}",
            f"⏱ Среднее время ответа: {_latency(t)}",
            f"👥 Активных адресов: {stats['active']}",
        ]
        hours = stats["hours"]
        if sum(hours) > 0:
            peak = max(range(24), key=lambda h: hours[h])
            lines.append(f"🕐 Пик активности (за всё время): {peak:02d}:00–{(peak + 1) % 24:02d}:00")

    lines.append("")
    lines.append("<b>📈 По дням</b>")
    lines.append(_chart(stats["series"]))

    top = sorted(((e, n) for e, n in stats["by_sender"].items() if n > 0), key=lambda x: x[1], reverse=True)[:5]
    if top:
        lines.append("<b>🏆 Топ адресов</b>")
        for i, (email, n) in enumerate(top, 1):
            ban = " 🚫" if storage.is_banned(email) else ""
            lines.append(f"{i}. {html.escape(_name(email))} — {n}{ban}")

    bans = len(storage.get_bans())
    if bans:
        lines.append(f"\n🛡 Забанено: {bans}")

    period_row = [_btn(("• " if p == period else "") + title, f"st:{p}") for p, title in PERIODS]
    kb = InlineKeyboardMarkup([
        period_row,
        [_btn("👥 По адресам", f"top:{period}:0"), _btn("🛡 Безопасность", "sec"), _btn("🔄", f"st:{period}")],
    ])
    return "\n".join(lines), kb


def render_top(period: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    days = PERIOD_DAYS[period]
    label = dict(PERIODS)[period]
    stats = storage.get_stats(days)
    by_sender = stats["by_sender"]
    senders = storage.load_senders()

    items = sorted(senders.items(), key=lambda kv: (-by_sender.get(kv[1]["email"], 0), kv[1]["email"]))
    if not items:
        return "Список доверенных адресов пуст.", InlineKeyboardMarkup([[_btn("◀️ К статистике", f"st:{period}")]])

    total_pages = max(1, (len(items) + TOP_PAGE_SIZE - 1) // TOP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * TOP_PAGE_SIZE

    rows = []
    for rank, (sid, item) in enumerate(items[start:start + TOP_PAGE_SIZE], start + 1):
        email = item["email"]
        ban = "🚫 " if storage.is_banned(email) else ""
        rows.append([_btn(f"{rank}. {ban}{item['note'] or email} — {by_sender.get(email, 0)}", f"card:{sid}:0")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("⬅️", f"top:{period}:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(_btn("➡️", f"top:{period}:{page + 1}"))
        rows.append(nav)
    rows.append([_btn("◀️ К статистике", f"st:{period}")])

    text = f"<b>👥 Запросы по адресам · {label}</b>\nВсего: {stats['totals']['requests']}. Нажмите на адрес — откроется карточка."
    return text, InlineKeyboardMarkup(rows)


def render_senders_list(page: int) -> tuple[str, InlineKeyboardMarkup]:
    senders = storage.load_senders()
    if not senders:
        return (
            "Список доверенных адресов пуст.\n\nПришлите e-mail (можно сразу с подписью: "
            "<code>user@mail.ru Имя</code>) или нажмите кнопку ниже.",
            InlineKeyboardMarkup([[_btn("➕ Добавить адрес", "add")]]),
        )

    items = sorted(senders.items(), key=lambda kv: (kv[1]["note"] or kv[1]["email"]).lower())
    total_pages = max(1, (len(items) + SENDERS_PAGE_SIZE - 1) // SENDERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * SENDERS_PAGE_SIZE:(page + 1) * SENDERS_PAGE_SIZE]

    rows = []
    for sid, item in chunk:
        email = item["email"]
        s = storage.get_sender_stats(email)
        ban = "🚫 " if storage.is_banned(email) else ""
        last = f" · {_fmt_ts(s['last_seen'])}" if s["last_seen"] else ""
        rows.append([_btn(f"{ban}{item['note'] or email} ({s['requests']}){last}", f"card:{sid}:0")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("⬅️", f"list:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(_btn("➡️", f"list:{page + 1}"))
        rows.append(nav)
    rows.append([_btn("➕ Добавить адрес", "add"), _btn("📊 Статистика", "st:today")])

    banned = sum(1 for _, i in items if storage.is_banned(i["email"]))
    text = f"<b>📋 Доверенные адреса ({len(items)})</b>"
    if banned:
        text += f"\n🚫 Из них забанено: {banned}"
    text += "\nВ скобках — число запросов, затем время последнего."
    return text, InlineKeyboardMarkup(rows)


def render_sender_card(sid: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    item = storage.load_senders().get(sid)
    if item is None:
        return "Этот адрес больше не в списке доверенных.", InlineKeyboardMarkup([[_btn("◀️ К списку", "list:0")]])

    email = item["email"]
    s = storage.get_sender_stats(email)
    history = list(reversed(storage.load_history().get(email, [])))
    total = len(history)
    ban = storage.get_bans().get(email)
    strikes = storage.get_strike_count(email)

    lines = [f"<b>📇 {html.escape(item['note'] or email)}</b>"]
    if item["note"]:
        lines.append(f"✉️ {html.escape(email)}")
    if ban:
        lines.append(f"🚫 <b>ЗАБАНЕН</b> {_fmt_ts(ban['banned_at'])} · {html.escape(storage.category_label(ban['category']))}")
        if ban.get("reason"):
            lines.append(f"💬 {html.escape(ban['reason'])}")
    elif strikes:
        lines.append(f"⚠️ Предупреждений: {strikes}/{storage.STRIKES_LIMIT}")
    lines.append(f"📅 Добавлен: {_fmt_ts(item['added_at'])}")

    lines.append("")
    lines.append(f"📨 Запросов: <b>{s['requests']}</b> · ✅ {_pct(s['ok'], s['requests'])}")
    lines.append(f"⚠️ Ошибок: {s['errors']} · 🚫 Отклонено: {s['blocked']} · 🗑 Отброшено: {s['dropped']}")
    lines.append(f"🖼 Фото: {s['images']} · ⏱ {_latency(s)}")
    if s["last_seen"]:
        lines.append(f"🕒 Последний запрос: {_fmt_ts(s['last_seen'])}")

    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * HISTORY_PAGE_SIZE

    rows = []
    if total:
        lines.append(f"\n<b>История (стр. {page + 1}/{total_pages}):</b>")
        for offset, entry in enumerate(history[start:start + HISTORY_PAGE_SIZE]):
            idx = start + offset
            icon = STATUS_ICONS.get(entry.get("status", "ok"), "✅")
            lines.append(f"\n{icon} <b>#{total - idx}</b> · {_fmt_ts(entry.get('timestamp', ''))}\n"
                         f"{html.escape(_shorten(entry.get('request', '')))}")
            subject = entry.get("subject") or "(без темы)"
            rows.append([_btn(f"🔍 #{total - idx} · {_shorten(subject, 30)}", f"ent:{sid}:{idx}")])
    else:
        lines.append("\nИстории запросов пока нет.")

    if total > HISTORY_PAGE_SIZE:
        nav = []
        if page > 0:
            nav.append(_btn("⬅️", f"card:{sid}:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(_btn("➡️", f"card:{sid}:{page + 1}"))
        rows.append(nav)

    ban_btn = _btn("🔓 Разбанить", f"unban:{sid}") if ban else _btn("🚫 Забанить", f"ban:{sid}")
    rows.append([_btn("✏️ Подпись", f"edit:{sid}"), ban_btn])
    if strikes and not ban:
        rows.append([_btn("🧹 Сбросить предупреждения", f"clr:{sid}")])
    rows.append([_btn("🗑 Удалить адрес", f"del:{sid}"), _btn("◀️ К списку", "list:0")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_entry(sid: str, idx: int) -> tuple[str, InlineKeyboardMarkup]:
    item = storage.load_senders().get(sid)
    if item is None:
        return "Этот адрес больше не в списке.", InlineKeyboardMarkup([[_btn("◀️ К списку", "list:0")]])

    history = list(reversed(storage.load_history().get(item["email"], [])))
    if not 0 <= idx < len(history):
        return "Запись не найдена.", InlineKeyboardMarkup([[_btn("◀️ К карточке", f"card:{sid}:0")]])

    entry = history[idx]
    icon = STATUS_ICONS.get(entry.get("status", "ok"), "✅")
    latency = entry.get("latency_ms", 0)
    meta = f" · ⏱ {latency / 1000:.1f} с" if latency else ""
    text = (
        f"{icon} <b>{_fmt_ts(entry.get('timestamp', ''))}</b> · {html.escape(_name(item['email']))}{meta}\n"
        f"<b>Тема:</b> {html.escape(entry.get('subject') or '(без темы)')}\n\n"
        f"<b>📤 Запрос:</b>\n{html.escape((entry.get('request') or '')[:DETAIL_LEN]) or '—'}\n\n"
        f"<b>🤖 Ответ:</b>\n{html.escape((entry.get('response') or '')[:DETAIL_LEN]) or '—'}"
    )

    nav = []
    if idx + 1 < len(history):
        nav.append(_btn("⬅️ Старее", f"ent:{sid}:{idx + 1}"))
    if idx > 0:
        nav.append(_btn("Новее ➡️", f"ent:{sid}:{idx - 1}"))
    rows = [nav] if nav else []
    rows.append([_btn("◀️ К карточке", f"card:{sid}:{idx // HISTORY_PAGE_SIZE}")])
    return text, InlineKeyboardMarkup(rows)


def render_security() -> tuple[str, InlineKeyboardMarkup]:
    bans = storage.get_bans()
    week_ago = datetime.now().timestamp() - 7 * 86400
    recent = 0
    for inc in storage.get_incidents():
        try:
            if datetime.fromisoformat(inc["ts"]).timestamp() >= week_ago:
                recent += 1
        except (KeyError, ValueError):
            continue

    lines = [
        "<b>🛡 Безопасность</b>",
        f"Проверка ИИ: {'включена' if storage.SECURITY_ENABLED else 'выключена'}",
        f"🚫 Забанено: {len(bans)}",
        f"⚠️ Активных предупреждений: {storage.count_active_strikes()}",
        f"🚨 Инцидентов за 7 дней: {recent}",
        "",
        f"<i>Бан сразу при уверенности ≥ {int(storage.BAN_CONFIDENCE * 100)}%, иначе после "
        f"{storage.STRIKES_LIMIT} предупреждений за {storage.STRIKE_WINDOW_DAYS} дн.</i>",
    ]

    rows = []
    if bans:
        lines.append("\n<b>Забанены:</b>")
        for email, info in sorted(bans.items(), key=lambda kv: kv[1]["banned_at"], reverse=True)[:8]:
            who = "ИИ" if info.get("by") == "ai" else "вы"
            lines.append(f"• {html.escape(_name(email))} — {_fmt_ts(info['banned_at'])} · "
                         f"{html.escape(storage.category_label(info.get('category', '')))} · {who}")
            rows.append([_btn(f"🔓 Разбанить: {_shorten(_name(email), 28)}", f"unban:{storage.sender_id(email)}")])
    else:
        lines.append("\nЗабаненных нет.")

    rows.append([_btn("📜 Журнал инцидентов", "inc:0"), _btn("📊 Статистика", "st:today")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_incidents(page: int) -> tuple[str, InlineKeyboardMarkup]:
    incidents = storage.get_incidents()
    if not incidents:
        return "<b>📜 Журнал инцидентов</b>\n\nПока пусто.", InlineKeyboardMarkup([[_btn("◀️ Назад", "sec")]])

    total_pages = max(1, (len(incidents) + INCIDENTS_PAGE_SIZE - 1) // INCIDENTS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = incidents[page * INCIDENTS_PAGE_SIZE:(page + 1) * INCIDENTS_PAGE_SIZE]

    lines = [f"<b>📜 Журнал инцидентов (стр. {page + 1}/{total_pages})</b>"]
    for inc in chunk:
        icon = ACTION_ICONS.get(inc["action"], "•")
        lines.append(
            f"\n{icon} <b>{_fmt_ts(inc['ts'])}</b> · {html.escape(_name(inc['email']))} · {ACTION_NAMES.get(inc['action'], inc['action'])}\n"
            f"🏷 {html.escape(storage.category_label(inc['category']))} ({int(inc['confidence'] * 100)}%)\n"
            f"💬 {html.escape(inc.get('reason') or '—')}"
        )
        if inc.get("preview"):
            lines.append(f"📝 «{html.escape(_shorten(inc['preview'], 100))}»")

    rows = []
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("⬅️", f"inc:{page - 1}"))
        nav.append(_btn(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(_btn("➡️", f"inc:{page + 1}"))
        rows.append(nav)
    rows.append([_btn("◀️ Безопасность", "sec")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ──────────────────────────── Клавиатуры и состояния ────────────────────────────

BTN_STATS = "📊 Статистика"
BTN_LIST = "📋 Адреса"
BTN_SEC = "🛡 Безопасность"
BTN_ADD = "➕ Добавить"


def kb_reply_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True,
                             input_field_placeholder="email Подпись — добавить быстро")
    kb.row(KeyboardButton(BTN_STATS), KeyboardButton(BTN_LIST))
    kb.row(KeyboardButton(BTN_SEC), KeyboardButton(BTN_ADD))
    return kb


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("✖️ Отмена", "cancel")]])


def kb_skip_note() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("⏭ Без подписи", "skipnote"), _btn("✖️ Отмена", "cancel")]])


ST_EMAIL = "waiting_email"
ST_NOTE = "waiting_note"
ST_EDIT_NOTE = "waiting_edit_note"

user_states: dict[int, dict] = {}


def set_state(user_id: int, state: str, **data) -> None:
    user_states[user_id] = {"state": state, **data}


def get_state(user_id: int) -> dict:
    return user_states.get(user_id, {})


def clear_state(user_id: int) -> None:
    user_states.pop(user_id, None)


# ──────────────────────────── Бот ────────────────────────────

bot = telebot.TeleBot(TG_BOT_TOKEN, parse_mode="HTML", threaded=False)


def is_owner(obj) -> bool:
    return obj.from_user is not None and obj.from_user.id == OWNER_ID


def _owner_in_state(state: str):
    return lambda m: is_owner(m) and get_state(m.from_user.id).get("state") == state


def _owner_text(text: str):
    return lambda m: is_owner(m) and (m.text or "") == text


def _edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup,
          answer_text: str | None = None, show_alert: bool = False) -> None:
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise
    bot.answer_callback_query(call.id, text=answer_text, show_alert=show_alert)


def parse_email_and_note(text: str) -> tuple[str, str] | None:
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    email = parts[0].strip().lower()
    if not storage_email_ok(email):
        return None
    return email, (parts[1].strip() if len(parts) > 1 else "")


def storage_email_ok(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and " " not in email and email.count("@") == 1 and not domain.endswith(".")


def _finish_add(chat_id: int, user_id: int, email: str, note: str) -> None:
    clear_state(user_id)
    if storage.add_sender(email, note, added_by=user_id):
        logger.info("Добавлен доверенный адрес: %s", email)
        text = f"✅ Добавлен: <b>{html.escape(note or email)}</b>\n✉️ {html.escape(email)}"
    else:
        text = f"ℹ️ Адрес {html.escape(email)} уже есть в списке."
    bot.send_message(chat_id, text, reply_markup=kb_reply_menu())
    list_text, list_kb = render_senders_list(0)
    bot.send_message(chat_id, list_text, reply_markup=list_kb)


def _ask_email(chat_id: int, user_id: int) -> None:
    set_state(user_id, ST_EMAIL)
    bot.send_message(chat_id, "Пришлите e-mail адрес. Можно сразу с подписью:\n<code>user@mail.ru Имя друга</code>",
                     reply_markup=kb_cancel())


def _handle_email_input(message: Message) -> None:
    parsed = parse_email_and_note(message.text or "")
    if parsed is None:
        bot.send_message(message.chat.id,
                         "⚠️ Не вижу e-mail. Пришлите адрес ещё раз (например, <code>user@mail.ru Имя</code>) "
                         "или нажмите «Отмена».", reply_markup=kb_cancel())
        return
    email, note = parsed
    if note:
        _finish_add(message.chat.id, message.from_user.id, email, note)
        return
    set_state(message.from_user.id, ST_NOTE, pending_email=email)
    bot.send_message(message.chat.id,
                     f"Адрес: <b>{html.escape(email)}</b>\n\nПришлите подпись (например, имя друга) "
                     f"или нажмите «Без подписи».", reply_markup=kb_skip_note())


# ─── команды и кнопки нижнего меню ───

@bot.message_handler(commands=["start", "help", "menu"], func=is_owner)
def cmd_start(message: Message) -> None:
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "👋 Панель управления Email Tunnel.\nМеню внизу всегда под рукой. Чтобы быстро добавить адрес, "
        "пришлите <code>email Подпись</code>.\nО блокировках ИИ вы будете узнавать сразу — "
        "уведомления приходят в этот чат.",
        reply_markup=kb_reply_menu(),
    )
    text, kb = render_stats("today")
    bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.message_handler(commands=["cancel"], func=is_owner)
def cmd_cancel(message: Message) -> None:
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id, "Отменено.", reply_markup=kb_reply_menu())


@bot.message_handler(commands=["stats"], func=is_owner)
@bot.message_handler(func=_owner_text(BTN_STATS), content_types=["text"])
def show_stats(message: Message) -> None:
    clear_state(message.from_user.id)
    text, kb = render_stats("today")
    bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.message_handler(commands=["list"], func=is_owner)
@bot.message_handler(func=_owner_text(BTN_LIST), content_types=["text"])
def show_list(message: Message) -> None:
    clear_state(message.from_user.id)
    text, kb = render_senders_list(0)
    bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.message_handler(commands=["security", "bans"], func=is_owner)
@bot.message_handler(func=_owner_text(BTN_SEC), content_types=["text"])
def show_security(message: Message) -> None:
    clear_state(message.from_user.id)
    text, kb = render_security()
    bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.message_handler(commands=["add"], func=is_owner)
def cmd_add(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        message.text = parts[1]
        _handle_email_input(message)
    else:
        _ask_email(message.chat.id, message.from_user.id)


@bot.message_handler(func=_owner_text(BTN_ADD), content_types=["text"])
def btn_add(message: Message) -> None:
    _ask_email(message.chat.id, message.from_user.id)


# ─── ввод данных по шагам ───

@bot.message_handler(func=_owner_in_state(ST_EMAIL), content_types=["text"])
def process_email(message: Message) -> None:
    _handle_email_input(message)


@bot.message_handler(func=_owner_in_state(ST_NOTE), content_types=["text"])
def process_note(message: Message) -> None:
    user_id = message.from_user.id
    email = get_state(user_id).get("pending_email")
    note = (message.text or "").strip()
    if not email:
        clear_state(user_id)
        bot.send_message(message.chat.id, "Что-то пошло не так, попробуйте снова.", reply_markup=kb_reply_menu())
        return
    _finish_add(message.chat.id, user_id, email, "" if note == "-" else note)


@bot.message_handler(func=_owner_in_state(ST_EDIT_NOTE), content_types=["text"])
def process_edit_note(message: Message) -> None:
    user_id = message.from_user.id
    sid = get_state(user_id).get("sender_id")
    note = (message.text or "").strip()
    clear_state(user_id)
    if sid and storage.update_note(sid, "" if note == "-" else note):
        text, kb = render_sender_card(sid, 0)
        bot.send_message(message.chat.id, "✅ Подпись обновлена.", reply_markup=kb_reply_menu())
        bot.send_message(message.chat.id, text, reply_markup=kb)
    else:
        bot.send_message(message.chat.id, "Адрес не найден.", reply_markup=kb_reply_menu())


# ─── callback-кнопки ───

def _cb(prefix_or_exact: str, exact: bool = False):
    if exact:
        return lambda c: is_owner(c) and c.data == prefix_or_exact
    return lambda c: is_owner(c) and (c.data or "").startswith(prefix_or_exact)


@bot.callback_query_handler(func=_cb("noop", exact=True))
def cb_noop(call: CallbackQuery) -> None:
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=_cb("cancel", exact=True))
def cb_cancel(call: CallbackQuery) -> None:
    clear_state(call.from_user.id)
    text, kb = render_senders_list(0)
    _edit(call, text, kb, answer_text="Отменено")


@bot.callback_query_handler(func=_cb("add", exact=True))
def cb_add(call: CallbackQuery) -> None:
    set_state(call.from_user.id, ST_EMAIL)
    _edit(call, "Пришлите e-mail адрес. Можно сразу с подписью:\n<code>user@mail.ru Имя друга</code>", kb_cancel())


@bot.callback_query_handler(func=_cb("skipnote", exact=True))
def cb_skip_note(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    email = get_state(user_id).get("pending_email")
    bot.answer_callback_query(call.id)
    if not email:
        clear_state(user_id)
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except ApiTelegramException:
        pass
    _finish_add(call.message.chat.id, user_id, email, "")


@bot.callback_query_handler(func=_cb("st:"))
def cb_stats(call: CallbackQuery) -> None:
    clear_state(call.from_user.id)
    period = call.data.split(":")[1]
    if period not in PERIOD_DAYS:
        period = "today"
    text, kb = render_stats(period)
    _edit(call, text, kb)


@bot.callback_query_handler(func=_cb("top:"))
def cb_top(call: CallbackQuery) -> None:
    _, period, page = call.data.split(":")
    if period not in PERIOD_DAYS:
        period = "today"
    text, kb = render_top(period, int(page))
    _edit(call, text, kb)


@bot.callback_query_handler(func=_cb("sec", exact=True))
def cb_security(call: CallbackQuery) -> None:
    text, kb = render_security()
    _edit(call, text, kb)


@bot.callback_query_handler(func=_cb("inc:"))
def cb_incidents(call: CallbackQuery) -> None:
    text, kb = render_incidents(int(call.data.split(":")[1]))
    _edit(call, text, kb)


@bot.callback_query_handler(func=_cb("list:"))
def cb_list(call: CallbackQuery) -> None:
    clear_state(call.from_user.id)
    text, kb = render_senders_list(int(call.data.split(":")[1]))
    _edit(call, text, kb)


@bot.callback_query_handler(func=_cb("card:"))
def cb_card(call: CallbackQuery) -> None:
    clear_state(call.from_user.id)
    _, sid, page = call.data.split(":")
    text, kb = render_sender_card(sid, int(page))
    _edit(call, text, kb)


@bot.callback_query_handler(func=_cb("ent:"))
def cb_entry(call: CallbackQuery) -> None:
    _, sid, idx = call.data.split(":")
    text, kb = render_entry(sid, int(idx))
    _edit(call, text, kb)


@bot.callback_query_handler(func=_cb("edit:"))
def cb_edit_note(call: CallbackQuery) -> None:
    sid = call.data.split(":", 1)[1]
    item = storage.load_senders().get(sid)
    if item is None:
        text, kb = render_senders_list(0)
        _edit(call, text, kb, answer_text="Адрес уже удалён", show_alert=True)
        return
    set_state(call.from_user.id, ST_EDIT_NOTE, sender_id=sid)
    _edit(call, f"Новая подпись для <b>{html.escape(item['email'])}</b>?\nПришлите текст или «-», чтобы убрать подпись.",
          InlineKeyboardMarkup([[_btn("✖️ Отмена", f"card:{sid}:0")]]))


@bot.callback_query_handler(func=_cb("ban_yes:"))
def cb_ban_confirmed(call: CallbackQuery) -> None:
    sid = call.data.split(":", 1)[1]
    email = storage.email_by_sid(sid)
    if email is None:
        bot.answer_callback_query(call.id, "Адрес не найден", show_alert=True)
        return
    storage.ban_user(email, "Заблокирован владельцем", category="manual", by="owner")
    storage.add_incident(email, "manual", "manual", 1.0, "Заблокирован владельцем", "ban")
    logger.info("Владелец забанил: %s", email)
    text, kb = render_sender_card(sid, 0)
    _edit(call, text, kb, answer_text="Забанен")


@bot.callback_query_handler(func=_cb("ban:"))
def cb_ban_ask(call: CallbackQuery) -> None:
    sid = call.data.split(":", 1)[1]
    email = storage.email_by_sid(sid)
    if email is None:
        bot.answer_callback_query(call.id, "Адрес не найден", show_alert=True)
        return
    _edit(call, f"Забанить <b>{html.escape(email)}</b>?\nПисьма от него перестанут обрабатываться и будут удаляться.",
          InlineKeyboardMarkup([[_btn("✅ Да, забанить", f"ban_yes:{sid}"), _btn("↩️ Нет", f"card:{sid}:0")]]))


@bot.callback_query_handler(func=_cb("unban:"))
def cb_unban(call: CallbackQuery) -> None:
    sid = call.data.split(":", 1)[1]
    email = storage.email_by_sid(sid)
    if email is None:
        bot.answer_callback_query(call.id, "Адрес не найден", show_alert=True)
        return
    changed = storage.unban_user(email)
    logger.info("Разбанен: %s", email)
    text, kb = render_security()
    _edit(call, text, kb, answer_text="🔓 Разблокирован" if changed else "Уже не в бане")


@bot.callback_query_handler(func=_cb("clr:"))
def cb_clear_strikes(call: CallbackQuery) -> None:
    sid = call.data.split(":", 1)[1]
    email = storage.email_by_sid(sid)
    if email is None:
        bot.answer_callback_query(call.id, "Адрес не найден", show_alert=True)
        return
    storage.clear_strikes(email)
    text, kb = render_sender_card(sid, 0)
    _edit(call, text, kb, answer_text="Предупреждения сброшены")


@bot.callback_query_handler(func=_cb("del_yes:"))
def cb_delete_confirmed(call: CallbackQuery) -> None:
    sid = call.data.split(":", 1)[1]
    email = storage.remove_sender(sid)
    if email:
        logger.info("Удалён доверенный адрес: %s", email)
    text, kb = render_senders_list(0)
    _edit(call, text, kb, answer_text="Адрес удалён" if email else "Адрес уже был удалён")


@bot.callback_query_handler(func=_cb("del:"))
def cb_delete_ask(call: CallbackQuery) -> None:
    sid = call.data.split(":", 1)[1]
    item = storage.load_senders().get(sid)
    if item is None:
        text, kb = render_senders_list(0)
        _edit(call, text, kb, answer_text="Адрес уже удалён", show_alert=True)
        return
    _edit(call, f"Удалить <b>{html.escape(item['email'])}</b> из доверенных?\nИстория и статистика сохранятся в файлах.",
          InlineKeyboardMarkup([[_btn("✅ Да, удалить", f"del_yes:{sid}"), _btn("↩️ Нет", f"card:{sid}:0")]]))


# ─── заглушки владельца (после всех его обработчиков) ───

@bot.message_handler(func=is_owner, content_types=["text"])
def fallback_owner_text(message: Message) -> None:
    parsed = parse_email_and_note(message.text or "")
    if parsed:
        _finish_add(message.chat.id, message.from_user.id, *parsed)
        return
    clear_state(message.from_user.id)
    bot.send_message(message.chat.id,
                     "Не понял. Пользуйтесь кнопками внизу или пришлите <code>email Подпись</code>, "
                     "чтобы добавить адрес.", reply_markup=kb_reply_menu())


@bot.message_handler(func=is_owner, content_types=["photo", "document", "sticker", "voice"])
def fallback_owner_other(message: Message) -> None:
    bot.send_message(message.chat.id, "Я принимаю только текст.", reply_markup=kb_reply_menu())


@bot.callback_query_handler(func=is_owner)
def fallback_owner_callback(call: CallbackQuery) -> None:
    bot.answer_callback_query(call.id, "Кнопка устарела. Откройте раздел заново.")


# ─── доступ для всех остальных ───

@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "document", "sticker", "voice"])
def deny_message(message: Message) -> None:
    bot.send_message(message.chat.id, "⛔ Доступ к этому боту ограничен.")


@bot.callback_query_handler(func=lambda c: True)
def deny_callback(call: CallbackQuery) -> None:
    bot.answer_callback_query(call.id, "⛔ Доступ ограничен", show_alert=True)


# ──────────────────────────── Точка входа ────────────────────────────

def main() -> None:
    bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен.")
    bot.infinity_polling()


if __name__ == "__main__":
    main()
