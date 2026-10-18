"""
main.py — почтовый цикл Email Tunnel.

Для каждого письма:
  1. внешний отправитель → проверка ИИ-классификатором (security.py): бан / предупреждение / пропуск;
  2. ответ Gemini (с повторными попытками при сбоях);
  3. доставка ответа: владельцу — в INBOX, остальным — по SMTP строго на адрес отправителя;
  4. только после успешной доставки письмо удаляется из ящика (двухфазная обработка);
  5. история и статистика пишутся в storage (их показывает bot.py).
"""

from __future__ import annotations

import logging
import time

import gemini
import mail
import security
import storage
from settings import MAIL_USER

POLL_INTERVAL_SECONDS = 10
MAX_ATTEMPTS = 3

OWNER = MAIL_USER.lower()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

_attempts: dict[int, int] = {}
_decisions: dict[int, security.Decision] = {}

NOTICE_BAN = (
    "Ваш запрос отклонён: он нарушает правила использования сервиса. "
    "Доступ приостановлен. Если вы считаете, что это ошибка, свяжитесь с владельцем."
)
NOTICE_BLOCK = (
    "Ваш запрос отклонён: он нарушает правила использования сервиса. "
    "Предупреждение {strikes} из {limit}: при повторении доступ будет закрыт."
)
NOTICE_ERROR = "Не удалось получить ответ нейросети. Попробуйте отправить запрос позже."


def _summary(text: str, images: list) -> str:
    return f"{text} [+ {len(images)} фото]" if images else text


def _deliver(letter: dict, body: str) -> None:
    if letter["sender"] == OWNER:
        mail.append_reply_to_inbox("Ответ от нейросети", body)
    else:
        mail.send_reply(letter["sender"], letter["subject"], body, in_reply_to=letter["message_id"])


def _try_notice(letter: dict, body: str) -> None:
    """Уведомление об отказе: ошибка доставки не должна ломать цикл и не повторяется."""
    try:
        _deliver(letter, body)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить уведомление об отказе %s: %s", letter["sender"], e)


def _notice_for(decision: security.Decision) -> str:
    if decision.action == "ban":
        return NOTICE_BAN
    return NOTICE_BLOCK.format(strikes=decision.strikes, limit=security.STRIKES_LIMIT)


def _register_failure(uid: int) -> bool:
    """True — исчерпаны попытки, письмо пора закрыть."""
    _attempts[uid] = _attempts.get(uid, 0) + 1
    return _attempts[uid] >= MAX_ATTEMPTS


def _finish(uid: int) -> None:
    _attempts.pop(uid, None)
    _decisions.pop(uid, None)


def _reject(letter: dict, decision: security.Decision) -> None:
    sender, text, images = letter["sender"], letter["text"], letter["images"]
    _try_notice(letter, _notice_for(decision))
    storage.log_request(sender, letter["subject"], _summary(text, images),
                        f"[Отклонено: {storage.category_label(decision.category)}]",
                        status="blocked", category=decision.category)
    storage.record_stat(sender, "blocked", images=len(images))


def process_letter(letter: dict) -> bool:
    """True — письмо обработано (или безнадёжно) и его можно удалять; False — повторить позже."""
    uid, sender = letter["uid"], letter["sender"]
    text, images = letter["text"], letter["images"]
    is_owner = sender == OWNER

    if not text.strip() and not images:
        _finish(uid)
        return True

    flagged = False
    if not is_owner:
        decision = _decisions.get(uid)
        if decision is None:
            decision = security.check_request(sender, letter["subject"], text, images)
            _decisions[uid] = decision
        if decision.blocked:
            _reject(letter, decision)
            _finish(uid)
            return True
        flagged = decision.flagged

    started = time.monotonic()
    try:
        answer = gemini.ask_gemini(text, images)
    except gemini.GeminiBlocked:
        if is_owner:
            _try_notice(letter, "Запрос отклонён фильтрами безопасности Gemini.")
            storage.record_stat(sender, "blocked", images=len(images))
        else:
            _reject(letter, security.on_model_block(sender, text))
        _finish(uid)
        return True
    except gemini.GeminiError as e:
        logger.error("Ошибка Gemini для %s: %s", sender, e)
        if not _register_failure(uid):
            return False
        _try_notice(letter, NOTICE_ERROR)
        storage.log_request(sender, letter["subject"], _summary(text, images), f"[Ошибка ИИ] {e}", status="error")
        storage.record_stat(sender, "error", images=len(images))
        _finish(uid)
        return True

    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        _deliver(letter, answer)
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось доставить ответ %s: %s", sender, e)
        if not _register_failure(uid):
            return False
        storage.log_request(sender, letter["subject"], _summary(text, images),
                            f"[Ответ не доставлен] {e}", status="error")
        storage.record_stat(sender, "error", images=len(images))
        _finish(uid)
        return True

    storage.log_request(sender, letter["subject"], _summary(text, images), answer, latency_ms=latency_ms)
    storage.record_stat(sender, "ok", images=len(images), latency_ms=latency_ms, flagged=flagged)
    logger.info("Ответ отправлен: %s (%d мс)", sender, latency_ms)
    _finish(uid)
    return True


def run_once() -> None:
    result = mail.fetch_incoming(
        allowed=set(storage.get_allowed_senders_list()),
        banned=storage.get_banned_set(),
    )

    to_delete: list[int] = []
    for uid, sender in result["dropped"]:
        storage.record_stat(sender, "dropped")
        to_delete.append(uid)

    for letter in result["letters"]:
        try:
            if process_letter(letter):
                to_delete.append(letter["uid"])
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка при обработке письма uid=%s", letter.get("uid"))

    mail.delete_messages(to_delete)


def run_forever() -> None:
    logger.info("Email-tunnel запущен. Опрос каждые %d сек. Ctrl+C для остановки.", POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка в цикле обработки")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        logger.info("Остановлено.")
