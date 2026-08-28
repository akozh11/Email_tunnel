"""
Цикл опроса одного почтового аккаунта. Запускается в отдельном потоке на
каждый аккаунт (main.py), потоки не блокируют друг друга.
"""

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from config import Account
from gemini_client import ask_gemini
from mail_account import IncomingRequest, MailAccount
from state import StateManager

logger = logging.getLogger("email_tunnel.worker")

_MAX_BACKOFF_SECONDS = 300


class _RateLimiter:
    """Не более N ответов одному адресату в течение скользящего часа.
    Дополнительный уровень защиты от петель поверх loop_guard: даже если
    заголовки автоответчика не распознаны, бот не "зациклится" насмерть."""

    def __init__(self, max_per_hour: int):
        self.max_per_hour = max_per_hour
        self._history: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, address: str) -> bool:
        now = time.time()
        history = self._history[address]
        while history and now - history[0] > 3600:
            history.popleft()
        if len(history) >= self.max_per_hour:
            return False
        history.append(now)
        return True


def _handle_request(mail: MailAccount, account: Account, request: IncomingRequest, rate_limiter: _RateLimiter) -> None:
    if not rate_limiter.allow(request.reply_addr):
        logger.warning(
            "[%s] Превышен лимит ответов адресату %s (%d/час) — письмо UID=%s пропущено без ответа.",
            account.name, request.reply_addr, rate_limiter.max_per_hour, request.uid,
        )
        mail.mark_resolved(request.uid, request.message_id, delete=False)
        return

    logger.info(
        "[%s] Обрабатываю запрос UID=%s от %s (%d фото)",
        account.name, request.uid, request.from_addr, len(request.images),
    )

    answer = ask_gemini(text=request.text, images=request.images, model=account.gemini_model)

    mail.deliver_reply(request, body_text=answer, subject=f"Re: {request.subject}")
    mail.mark_resolved(request.uid, request.message_id, delete=True)

    logger.info("[%s] Ответ на UID=%s отправлен (%s), запрос помечен обработанным.", account.name, request.uid, account.reply_mode)


def run_account_loop(account: Account, stop_event: threading.Event) -> None:
    state = StateManager(account.name)
    mail = MailAccount(account, state)
    rate_limiter = _RateLimiter(account.max_replies_per_sender_per_hour)

    consecutive_errors = 0
    logger.info("[%s] Поток запущен, интервал опроса: %d сек.", account.name, account.poll_interval_seconds)

    while not stop_event.is_set():
        try:
            requests = mail.fetch_new_requests()

            for request in requests:
                if stop_event.is_set():
                    break
                try:
                    _handle_request(mail, account, request, rate_limiter)
                except Exception:
                    # Не резолвим и не удаляем письмо — будет повторная попытка
                    # в одном из следующих циклов опроса (см. state.mark_uid_resolved).
                    logger.exception(
                        "[%s] Ошибка при обработке письма UID=%s — запрос будет повторён позже.",
                        account.name, request.uid,
                    )

            consecutive_errors = 0

        except Exception:
            consecutive_errors += 1
            logger.exception("[%s] Ошибка в цикле опроса (подряд: %d)", account.name, consecutive_errors)

        if state.has_stuck_gap:
            logger.warning(
                "[%s] Есть письма, застрявшие из-за повторяющихся ошибок обработки — "
                "проверьте логи выше и состояние аккаунта.",
                account.name,
            )

        backoff = min(account.poll_interval_seconds * (2 ** consecutive_errors), _MAX_BACKOFF_SECONDS) \
            if consecutive_errors else account.poll_interval_seconds
        stop_event.wait(backoff)

    logger.info("[%s] Поток остановлен.", account.name)