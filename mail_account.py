"""
Работа с одним почтовым ящиком: чтение новых писем (UID-инкрементально),
фильтрация по loop_guard, извлечение текста/изображений, отправка ответа
строго на адрес отправителя (или дописывание ответа в тот же INBOX —
режим reply_mode="append_inbox", как в исходной версии проекта).
"""

import email
import imaplib
import logging
import re
import smtplib
import ssl
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header
from email.message import Message
from email.mime.text import MIMEText
from email.utils import parseaddr, formatdate, make_msgid
from typing import List, Optional

from config import Account
from loop_guard import is_loop_risk
from state import StateManager

MAX_IMAGE_BYTES_TOTAL = 20 * 1024 * 1024  # 20 МБ суммарно на письмо — защита от раздувания запроса к Gemini
IMAP_CONNECT_RETRIES = 3
IMAP_CONNECT_RETRY_DELAY = 5

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_BR_RE = re.compile(r"(?i)<br\s*/?>")
_HTML_P_CLOSE_RE = re.compile(r"(?i)</p>")

logger = logging.getLogger("email_tunnel.mail")


def decode_mime_str(s: Optional[str]) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    decoded = ""
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded += text.decode(charset or "utf-8", errors="ignore")
        else:
            decoded += text
    return decoded


def _html_to_text(html: str) -> str:
    text = _HTML_BR_RE.sub("\n", html)
    text = _HTML_P_CLOSE_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    return text.strip()


def get_body_and_images(msg: Message):
    """
    Возвращает (текст письма, список изображений).
    Каждое изображение — словарь {filename, mime_type, data (bytes)}.
    Если text/plain отсутствует, но есть text/html — используется он
    (с грубой конвертацией в текст) как запасной вариант.
    """
    plain_body = ""
    html_body = ""
    images = []
    total_image_bytes = 0

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in content_disposition and not plain_body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    plain_body = payload.decode(charset, errors="ignore")

            elif content_type == "text/html" and "attachment" not in content_disposition and not html_body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_body = payload.decode(charset, errors="ignore")

            if content_type.startswith("image/"):
                payload = part.get_payload(decode=True)
                if payload:
                    if total_image_bytes + len(payload) > MAX_IMAGE_BYTES_TOTAL:
                        logger.warning("Пропускаю изображение — превышен лимит %d байт на письмо.", MAX_IMAGE_BYTES_TOTAL)
                        continue
                    total_image_bytes += len(payload)
                    filename = part.get_filename() or "image.jpg"
                    images.append({
                        "filename": decode_mime_str(filename),
                        "mime_type": content_type,
                        "data": payload,
                    })
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            if msg.get_content_type() == "text/html":
                html_body = payload.decode(charset, errors="ignore")
            else:
                plain_body = payload.decode(charset, errors="ignore")

    body = plain_body.strip() or _html_to_text(html_body)
    return body.strip(), images


def _extract_reply_address(msg: Message) -> str:
    """
    Строгое извлечение адреса для ответа.
    Приоритет: Reply-To (корректный способ по RFC 5322) -> From -> Return-Path.
    """
    for header_name in ("Reply-To", "From", "Return-Path"):
        raw_value = msg.get(header_name)
        if not raw_value:
            continue
        _, address = parseaddr(decode_mime_str(raw_value))
        address = address.strip().strip("<>")
        if address:
            return address
    return ""


@dataclass
class IncomingRequest:
    uid: int
    message_id: str
    from_addr: str
    reply_addr: str
    subject: str
    text: str
    images: List[dict] = field(default_factory=list)


class MailAccount:
    def __init__(self, account: Account, state: StateManager):
        self.account = account
        self.state = state

    # ---------------------------------------------------------------- IMAP

    @contextmanager
    def _imap_connection(self):
        imap = None
        last_error = None
        for attempt in range(1, IMAP_CONNECT_RETRIES + 1):
            try:
                imap = imaplib.IMAP4_SSL(self.account.imap_server, self.account.imap_port)
                imap.login(self.account.mail_user, self.account.mail_pass)
                break
            except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
                last_error = exc
                logger.warning(
                    "[%s] Ошибка подключения к IMAP (попытка %d/%d): %s",
                    self.account.name, attempt, IMAP_CONNECT_RETRIES, exc,
                )
                if imap is not None:
                    try:
                        imap.logout()
                    except Exception:
                        pass
                time.sleep(IMAP_CONNECT_RETRY_DELAY)
        else:
            raise ConnectionError(f"Не удалось подключиться к IMAP {self.account.imap_server}: {last_error}")

        try:
            yield imap
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _select_inbox_and_check_uidvalidity(self, imap: imaplib.IMAP4_SSL, mailbox: str = "INBOX") -> None:
        typ, _ = imap.select(mailbox)
        if typ != "OK":
            raise RuntimeError(f"Не удалось выбрать mailbox '{mailbox}': ответ IMAP = {typ}")

        uidvalidity_raw = imap.untagged_responses.get("UIDVALIDITY", [b""])[0]
        uidvalidity = uidvalidity_raw.decode() if isinstance(uidvalidity_raw, bytes) else str(uidvalidity_raw)

        if self.state.uidvalidity is not None and uidvalidity and uidvalidity != self.state.uidvalidity:
            logger.warning(
                "[%s] UIDVALIDITY изменился (%s -> %s) — mailbox был пересоздан, сбрасываю состояние.",
                self.account.name, self.state.uidvalidity, uidvalidity,
            )
            self.state.reset_for_new_mailbox(uidvalidity)
        elif self.state.uidvalidity is None:
            self.state.uidvalidity = uidvalidity
            self.state.save()

    def _get_uidnext(self, imap: imaplib.IMAP4_SSL) -> int:
        uidnext_raw = imap.untagged_responses.get("UIDNEXT", [b"1"])[0]
        try:
            return int(uidnext_raw)
        except (TypeError, ValueError):
            return 1

    def fetch_new_requests(self) -> List[IncomingRequest]:
        """
        Забирает новые письма (UID > last_uid) из INBOX, фильтрует их по
        loop_guard / self_only, и возвращает список валидных запросов.
        Письма НЕ удаляются здесь — удаление/подтверждение происходит
        отдельным вызовом mark_resolved() после успешной отправки ответа,
        чтобы при сбое на этапе генерации ответа запрос не терялся.
        """
        requests: List[IncomingRequest] = []

        with self._imap_connection() as imap:
            self._select_inbox_and_check_uidvalidity(imap)

            uidnext = self._get_uidnext(imap)
            start_uid = self.state.last_uid + 1

            first_run = self.state.last_uid == 0
            if first_run and not self.account.process_backlog_on_first_run:
                # Не обрабатываем историю писем — начинаем отслеживать только новые.
                self.state.fast_forward(max(uidnext - 1, 0))
                self.state.save()
                logger.info(
                    "[%s] Первый запуск без обработки истории — начинаю с UID %d.",
                    self.account.name, self.state.last_uid,
                )
                return []

            if start_uid >= uidnext:
                # Известный нюанс imaplib: диапазон "start:*" при start > максимального
                # существующего UID вернёт последнее письмо вместо пустого результата.
                # Поэтому явно проверяем UIDNEXT и не делаем запрос вовсе.
                return []

            typ, data = imap.uid("search", None, f"({start_uid}:*)")
            if typ != "OK" or not data or not data[0]:
                return []

            uids = [int(u) for u in data[0].split() if u.isdigit() and int(u) >= start_uid]
            uids.sort()
            uids = uids[: self.account.max_messages_per_cycle]

            for uid in uids:
                typ, msg_data = imap.uid("fetch", str(uid), "(RFC822)")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    logger.warning("[%s] Не удалось прочитать письмо UID=%s, повторю в следующем цикле.", self.account.name, uid)
                    continue  # не резолвим — попробуем снова в следующем цикле опроса

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                message_id = (msg.get("Message-ID") or "").strip()
                if self.state.has_processed(message_id):
                    self.state.mark_uid_resolved(uid)
                    continue

                from_raw = decode_mime_str(msg.get("From"))
                _, from_email = parseaddr(from_raw)
                from_email = from_email.lower()

                loop_reason = is_loop_risk(msg, from_email)
                if loop_reason:
                    logger.info("[%s] Пропускаю письмо UID=%s: %s", self.account.name, uid, loop_reason)
                    self.state.mark_processed(message_id)
                    self.state.mark_uid_resolved(uid)
                    imap.uid("store", str(uid), "+FLAGS", "\\Seen")
                    continue

                if self.account.self_only and from_email != self.account.mail_user.lower():
                    logger.debug(
                        "[%s] Пропускаю письмо UID=%s: self_only=True, отправитель %s != владельца ящика.",
                        self.account.name, uid, from_email,
                    )
                    self.state.mark_processed(message_id)
                    self.state.mark_uid_resolved(uid)
                    imap.uid("store", str(uid), "+FLAGS", "\\Seen")
                    continue

                body, images = get_body_and_images(msg)
                if not body.strip() and not images:
                    self.state.mark_processed(message_id)
                    self.state.mark_uid_resolved(uid)
                    continue

                reply_addr = _extract_reply_address(msg)
                if not reply_addr:
                    logger.warning("[%s] Не удалось извлечь адрес для ответа, письмо UID=%s пропущено.", self.account.name, uid)
                    self.state.mark_processed(message_id)
                    self.state.mark_uid_resolved(uid)
                    continue

                requests.append(IncomingRequest(
                    uid=uid,
                    message_id=message_id,
                    from_addr=from_email,
                    reply_addr=reply_addr,
                    subject=decode_mime_str(msg.get("Subject")) or "(без темы)",
                    text=body,
                    images=images,
                ))

            self.state.save()

        return requests

    def mark_resolved(self, uid: int, message_id: str, delete: bool = True) -> None:
        """Подтверждает обработку письма: помечает Message-ID как обработанный
        и, если delete=True, удаляет письмо из INBOX (эквивалент purge из
        исходной версии, но выполняется только после успешной отправки ответа)."""
        with self._imap_connection() as imap:
            self._select_inbox_and_check_uidvalidity(imap)
            if delete:
                imap.uid("store", str(uid), "+FLAGS", "\\Deleted")
                imap.expunge()
            else:
                imap.uid("store", str(uid), "+FLAGS", "\\Seen")
        self.state.mark_processed(message_id)
        self.state.mark_uid_resolved(uid)
        self.state.save()

    # ---------------------------------------------------------------- SMTP / ответы

    def send_reply(self, to_addr: str, subject: str, body_text: str) -> None:
        """Отправляет ответ реальным письмом по SMTP на указанный адрес."""
        msg = MIMEText(body_text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.account.mail_user
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg["X-Email-Tunnel-Type"] = "reply"
        # Эти два заголовка — вежливая просьба к чужим автоответчикам не
        # отвечать нам в ответ (снижает риск петли со стороны получателя).
        msg["Auto-Submitted"] = "auto-replied"
        msg["Precedence"] = "bulk"

        last_error = None
        for attempt in range(1, IMAP_CONNECT_RETRIES + 1):
            try:
                if self.account.smtp_use_ssl:
                    server = smtplib.SMTP_SSL(self.account.smtp_server, self.account.smtp_port, timeout=30)
                else:
                    server = smtplib.SMTP(self.account.smtp_server, self.account.smtp_port, timeout=30)
                    server.starttls()
                with server:
                    server.login(self.account.mail_user, self.account.mail_pass)
                    server.sendmail(self.account.mail_user, [to_addr], msg.as_string())
                return
            except (smtplib.SMTPException, OSError) as exc:
                last_error = exc
                logger.warning(
                    "[%s] Ошибка SMTP при отправке ответа на %s (попытка %d/%d): %s",
                    self.account.name, to_addr, attempt, IMAP_CONNECT_RETRIES, exc,
                )
                time.sleep(IMAP_CONNECT_RETRY_DELAY)

        raise ConnectionError(f"Не удалось отправить письмо через SMTP: {last_error}")

    def append_reply_to_inbox(self, subject: str, body_text: str, mailbox: str = "INBOX") -> None:
        """Дописывает ответ прямо в INBOX того же ящика (режим 'append_inbox'),
        как в исходной версии проекта — без реальной отправки по SMTP."""
        msg = MIMEText(body_text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.account.mail_user
        msg["To"] = self.account.mail_user
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg["X-Email-Tunnel-Type"] = "reply"
        msg["Auto-Submitted"] = "auto-replied"

        with self._imap_connection() as imap:
            imap.append(
                mailbox,
                None,
                imaplib.Time2Internaldate(datetime.now().timetuple()),
                msg.as_bytes(),
            )

    def deliver_reply(self, request: IncomingRequest, body_text: str, subject: str = "Ответ от нейросети") -> None:
        """Единая точка отправки ответа согласно reply_mode аккаунта.
        Адрес назначения для reply_mode='smtp' берётся строго из request.reply_addr
        (Reply-To -> From -> Return-Path исходного письма)."""
        if self.account.reply_mode == "smtp":
            self.send_reply(to_addr=request.reply_addr, subject=subject, body_text=body_text)
        else:
            self.append_reply_to_inbox(subject=subject, body_text=body_text)