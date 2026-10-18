"""
security.py — проверка запросов ИИ-классификатором, автобан и уведомления владельцу в Telegram.

Политика:
  • dangerous и уверенность ≥ BAN_CONFIDENCE          → бан сразу, запрос не обрабатывается;
  • dangerous с меньшей уверенностью                   → предупреждение, запрос отклонён;
  • suspicious                                         → предупреждение, запрос всё же обработан;
  • набралось STRIKES_LIMIT предупреждений за окно     → бан;
  • sensitive (человек в кризисе)                      → БЕЗ бана и предупреждений, запрос обработан,
                                                          владельцу приходит уведомление без текста запроса;
  • сбой классификатора                                → запрос обрабатывается (fail-open), ошибка в логе.

Каждое событие пишется в журнал инцидентов, владельцу приходит сообщение с кнопками
(«Разблокировать», «Забанить», «Сбросить предупреждения») — их обрабатывает bot.py.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass

import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

import gemini
import storage
from settings import OWNER_ID, TG_BOT_TOKEN

logger = logging.getLogger("security")

STRIKES_LIMIT = storage.STRIKES_LIMIT


@dataclass
class Decision:
    action: str = "allow"        # allow | block | ban
    verdict: str = "safe"
    category: str = ""
    confidence: float = 0.0
    reason: str = ""
    strikes: int = 0
    flagged: bool = False        # sensitive: обработать, но отметить в статистике

    @property
    def blocked(self) -> bool:
        return self.action in ("block", "ban")


# ──────────────────────────── Уведомления в Telegram ────────────────────────────

_bot: telebot.TeleBot | None = None


def _get_bot() -> telebot.TeleBot:
    global _bot
    if _bot is None:
        _bot = telebot.TeleBot(TG_BOT_TOKEN, parse_mode="HTML", threaded=False)
    return _bot


def notify_owner(text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    """Отправляет сообщение владельцу. Ошибки не прерывают работу почтового цикла."""
    for attempt in (1, 2, 3):
        try:
            _get_bot().send_message(OWNER_ID, text, reply_markup=kb)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось отправить уведомление владельцу (попытка %d): %s", attempt, e)
            time.sleep(2)
    return False


def _who(email: str) -> str:
    note = storage.get_note(email)
    if note:
        return f"{html.escape(note)} ({html.escape(email)})"
    return html.escape(email)


def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def _preview(category: str, text: str) -> str:
    """Для материалов о детях текст в уведомление не включаем вовсе."""
    if category == "child_safety":
        return ""
    return " ".join((text or "").split())[:200]


def _alert(kind: str, sender: str, decision: Decision, preview: str) -> None:
    sid = storage.sender_id(sender)
    category = html.escape(storage.category_label(decision.category))
    confidence = int(decision.confidence * 100)
    reason = html.escape(decision.reason or "—")
    quote = f"\n📝 «{html.escape(preview)}»" if preview else ""

    if kind == "ban":
        text = (
            f"🚨 <b>Пользователь заблокирован ИИ</b>\n"
            f"👤 {_who(sender)}\n"
            f"🏷 {category} (уверенность {confidence}%)\n"
            f"💬 {reason}{quote}\n\n"
            f"Письма от него больше не обрабатываются."
        )
        kb = _kb([("🔓 Разблокировать", f"unban:{sid}"), ("📇 Карточка", f"card:{sid}:0")])
    elif kind == "block":
        text = (
            f"⛔ <b>Запрос отклонён</b> · предупреждение {decision.strikes}/{STRIKES_LIMIT}\n"
            f"👤 {_who(sender)}\n"
            f"🏷 {category} (уверенность {confidence}%)\n"
            f"💬 {reason}{quote}"
        )
        kb = _kb([("🚫 Забанить", f"ban:{sid}"), ("🧹 Сбросить", f"clr:{sid}")],
                 [("📇 Карточка", f"card:{sid}:0")])
    elif kind == "strike":
        text = (
            f"⚠️ <b>Подозрительный запрос</b> · предупреждение {decision.strikes}/{STRIKES_LIMIT}\n"
            f"👤 {_who(sender)}\n"
            f"🏷 {category} (уверенность {confidence}%)\n"
            f"💬 {reason}{quote}\n\n"
            f"Запрос обработан."
        )
        kb = _kb([("🚫 Забанить", f"ban:{sid}"), ("🧹 Сбросить", f"clr:{sid}")],
                 [("📇 Карточка", f"card:{sid}:0")])
    else:  # flag
        text = (
            f"💬 <b>Чувствительный запрос</b> (без бана)\n"
            f"👤 {_who(sender)}\n"
            f"Похоже, человек в непростой ситуации. Запрос обработан, текст в уведомление не включён — "
            f"возможно, стоит с ним связаться."
        )
        kb = _kb([("📇 Карточка", f"card:{sid}:0")])

    notify_owner(text, kb)


# ──────────────────────────── Политика ────────────────────────────

def _apply_policy(sender: str, text: str, result: dict) -> Decision:
    verdict = result["verdict"]
    category = result["category"]
    confidence = result["confidence"]
    reason = result["reason"]
    base = dict(verdict=verdict, category=category, confidence=confidence, reason=reason)

    if verdict == "safe":
        return Decision()

    preview = _preview(category, text)

    if verdict == "sensitive":
        storage.add_incident(sender, verdict, category, confidence, reason, "flag")
        decision = Decision(action="allow", flagged=True, **base)
        _alert("flag", sender, decision, "")
        return decision

    if verdict == "dangerous" and confidence >= storage.BAN_CONFIDENCE:
        return _ban(sender, base, preview, strikes=storage.get_strike_count(sender))

    strikes = storage.add_strike(sender, category, reason)
    if strikes >= storage.STRIKES_LIMIT:
        return _ban(sender, base, preview, strikes=strikes)

    if verdict == "dangerous":
        decision = Decision(action="block", strikes=strikes, **base)
        storage.add_incident(sender, verdict, category, confidence, reason, "block", preview)
        _alert("block", sender, decision, preview)
        return decision

    decision = Decision(action="allow", strikes=strikes, **base)
    storage.add_incident(sender, verdict, category, confidence, reason, "strike", preview)
    _alert("strike", sender, decision, preview)
    return decision


def _ban(sender: str, base: dict, preview: str, strikes: int) -> Decision:
    storage.ban_user(sender, base["reason"], base["category"], preview, by="ai")
    storage.add_incident(sender, base["verdict"], base["category"], base["confidence"],
                         base["reason"], "ban", preview)
    decision = Decision(action="ban", strikes=strikes, **base)
    _alert("ban", sender, decision, preview)
    logger.warning("Автобан: %s (%s, %.2f)", sender, base["category"], base["confidence"])
    return decision


# ──────────────────────────── Публичный API ────────────────────────────

def check_request(sender: str, subject: str, text: str, images: list | None = None) -> Decision:
    """Проверяет запрос внешнего пользователя. Для владельца вызывать не нужно."""
    if not storage.SECURITY_ENABLED:
        return Decision()
    try:
        result = gemini.classify_request(text, images, subject)
    except gemini.GeminiBlocked as e:
        logger.warning("Классификатор заблокирован фильтрами Gemini (%s): %s", sender, e)
        result = {
            "verdict": "dangerous",
            "category": "model_blocked",
            "confidence": 0.5,
            "reason": "Запрос заблокирован встроенными фильтрами Gemini.",
        }
    except gemini.GeminiError as e:
        logger.error("Классификатор недоступен, запрос пропущен без проверки (%s): %s", sender, e)
        return Decision()
    return _apply_policy(sender, text, result)


def on_model_block(sender: str, text: str) -> Decision:
    """Gemini отказался отвечать на запрос, который классификатор пропустил — это тоже предупреждение."""
    result = {
        "verdict": "dangerous",
        "category": "model_blocked",
        "confidence": 0.5,
        "reason": "Gemini отказался отвечать: сработали встроенные фильтры безопасности.",
    }
    return _apply_policy(sender, text, result)
