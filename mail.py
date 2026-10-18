"""
mail.py — работа с почтой (IMAP/SMTP).

  • fetch_incoming()       — забирает письма владельца и доверенных адресов, отбрасывает письма забаненных;
  • delete_messages()      — удаляет письма по UID (после подтверждённой отправки ответа — «двухфазная» обработка);
  • send_reply()           — ответ внешнему пользователю по SMTP;
  • append_reply_to_inbox() — ответ владельцу кладётся прямо в его INBOX.

Защита от почтовых петель: автоответы, bounce-письма и собственные ответы туннеля пропускаются.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
from contextlib import contextmanager
from datetime import datetime
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr

from settings import IMAP_PORT, IMAP_SERVER, MAIL_PASS, MAIL_USER, SMTP_PORT, SMTP_SERVER

logger = logging.getLogger("mail")

OWNER = MAIL_USER.lower()
TUNNEL_HEADER = "X-Email-Tunnel-Type"

# UID писем, которые точно не нужно разбирать повторно (свои ответы, автоответы, bounce)
_ignored_uids: set[int] = set()


def decode_mime_str(s) -> str:
    if not s:
        return ""
    decoded = ""
    for part, charset in decode_header(str(s)):
        if isinstance(part, bytes):
            decoded += part.decode(charset or "utf-8", errors="ignore")
        else:
            decoded += part
    return decoded


def _strip_html(html_text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def get_body_and_images(msg) -> tuple[str, list[dict]]:
    """(текст письма, список изображений {filename, mime_type, data})."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    images: list[dict] = []

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))

        if content_type in ("text/plain", "text/html") and "attachment" not in disposition:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="ignore")
                (plain_parts if content_type == "text/plain" else html_parts).append(decoded)

        elif content_type.startswith("image/"):
            payload = part.get_payload(decode=True)
            if payload:
                images.append({
                    "filename": decode_mime_str(part.get_filename() or "image.jpg"),
                    "mime_type": content_type,
                    "data": payload,
                })

    body = "\n".join(plain_parts).strip()
    if not body and html_parts:
        body = _strip_html("\n".join(html_parts))
    return body, images


def is_auto_generated(msg) -> bool:
    """Свои ответы, автоответы, рассылки и уведомления о недоставке — не обрабатываем."""
    if msg.get(TUNNEL_HEADER):
        return True
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    if (msg.get("Precedence") or "").strip().lower() in {"bulk", "junk", "list", "auto_reply"}:
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return True
    if msg.get_content_type() == "multipart/report":
        return True
    local_part = parseaddr(decode_mime_str(msg.get("From")))[1].lower().split("@")[0]
    return local_part in {"mailer-daemon", "postmaster"}


@contextmanager
def _imap(mailbox: str = "INBOX"):
    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=30)
    try:
        imap.login(MAIL_USER, MAIL_PASS)
        imap.select(mailbox)
        yield imap
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def _fetch_message(imap, uid: int, headers_only: bool):
    section = "BODY.PEEK[HEADER]" if headers_only else "BODY.PEEK[]"
    status, data = imap.uid("FETCH", str(uid), f"({section})")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])


def fetch_incoming(allowed: set[str], banned: set[str]) -> dict:
    """
    Возвращает {"letters": [...], "dropped": [(uid, sender), ...]}.

    letter: {uid, sender, subject, message_id, text, images}.
    • письма владельца и доверенных адресов — в letters;
    • письма забаненных — в dropped (вызывающий код удалит их и учтёт в статистике);
    • письма незнакомых адресов остаются нетронутыми в ящике.
    """
    allowed = {a.lower() for a in allowed}
    banned = {b.lower() for b in banned}
    letters: list[dict] = []
    dropped: list[tuple[int, str]] = []

    with _imap() as imap:
        status, data = imap.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return {"letters": letters, "dropped": dropped}

        for raw_uid in data[0].split():
            uid = int(raw_uid)
            if uid in _ignored_uids:
                continue

            header = _fetch_message(imap, uid, headers_only=True)
            if header is None:
                continue
            if is_auto_generated(header):
                _ignored_uids.add(uid)
                continue

            sender = parseaddr(decode_mime_str(header.get("From")))[1].lower()
            if not sender:
                continue

            if sender != OWNER and sender in banned:
                dropped.append((uid, sender))
                continue
            if sender != OWNER and sender not in allowed:
                continue

            msg = _fetch_message(imap, uid, headers_only=False)
            if msg is None:
                continue
            text, images = get_body_and_images(msg)
            letters.append({
                "uid": uid,
                "sender": sender,
                "subject": decode_mime_str(msg.get("Subject")),
                "message_id": msg.get("Message-ID"),
                "text": text,
                "images": images,
            })

    return {"letters": letters, "dropped": dropped}


def delete_messages(uids: list[int]) -> None:
    if not uids:
        return
    with _imap() as imap:
        imap.uid("STORE", ",".join(str(u) for u in uids), "+FLAGS", "(\\Deleted)")
        imap.expunge()


def send_reply(to_addr: str, subject: str, body_text: str, in_reply_to: str | None = None) -> None:
    """Ответ внешнему пользователю через SMTP (strict routing: только на адрес отправителя)."""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}" if subject else "Ответ от нейросети"

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_USER
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=MAIL_USER.split("@")[-1])
    msg["Auto-Submitted"] = "auto-replied"
    msg[TUNNEL_HEADER] = "reply"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg)


def append_reply_to_inbox(subject: str, body_text: str, mailbox: str = "INBOX") -> None:
    """Ответ владельцу кладётся прямо в его ящик с маркером «это ответ, не запрос»."""
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_USER
    msg["To"] = MAIL_USER
    msg["Date"] = formatdate(localtime=True)
    msg[TUNNEL_HEADER] = "reply"

    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=30)
    try:
        imap.login(MAIL_USER, MAIL_PASS)
        imap.append(mailbox, None, imaplib.Time2Internaldate(datetime.now().timetuple()), msg.as_bytes())
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
