"""
Эвристики для защиты от почтовых петель.

Письмо считается "рискованным" (не должно порождать ответ ИИ), если оно:
  - помечено нашим же маркером X-Email-Tunnel-Type (это наш собственный ответ);
  - является bounce/DSN-уведомлением (multipart/report);
  - содержит заголовок Auto-Submitted, отличный от "no" (RFC 3834);
  - содержит Precedence: bulk/junk/list/auto_reply;
  - содержит заголовки X-Autoreply / X-Autorespond;
  - отправлено с адреса, локальная часть которого похожа на служебный
    (mailer-daemon, postmaster, no-reply, bounce, newsletter и т.п.);
  - тема письма похожа на типичный автоответ ("Out of Office",
    "Delivery Status Notification", "Автоответ" и т.п.).
"""

import re
from email.message import Message
from typing import Optional

_LOOP_LOCAL_PART_RE = re.compile(
    r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounce(s)?|"
    r"notifications?|newsletter|automail(er)?|auto-?confirm|listserv|"
    r"majordomo|abuse|support-noreply)\b",
    re.IGNORECASE,
)

_AUTOREPLY_SUBJECT_RE = re.compile(
    r"(undelivered mail|delivery status notification|mail delivery (failed|failure)|"
    r"undeliverable|automatic reply|out of office|auto[- ]?reply|"
    r"недоставлено|доставка невозможна|автоответ|уведомление о доставке)",
    re.IGNORECASE,
)

_LOOP_RISK_PRECEDENCE = {"bulk", "junk", "list", "auto_reply"}


def is_loop_risk(msg: Message, from_email: str) -> Optional[str]:
    """Возвращает текстовую причину, если письмо — риск почтовой петли, иначе None."""

    if msg.get("X-Email-Tunnel-Type"):
        return "собственный маркер X-Email-Tunnel-Type (это наш ответ)"

    try:
        if msg.get_content_type() == "multipart/report":
            return "multipart/report (bounce / DSN-уведомление)"
    except Exception:
        pass

    auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return f"заголовок Auto-Submitted: {auto_submitted}"

    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in _LOOP_RISK_PRECEDENCE:
        return f"заголовок Precedence: {precedence}"

    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return "заголовок X-Autoreply/X-Autorespond"

    local_part = from_email.split("@")[0] if from_email and "@" in from_email else from_email
    if local_part and _LOOP_LOCAL_PART_RE.match(local_part):
        return f"адрес отправителя похож на служебный/автоматический: {from_email}"

    subject = msg.get("Subject") or ""
    if _AUTOREPLY_SUBJECT_RE.search(subject):
        return f"тема письма похожа на автоответ/bounce: {subject!r}"

    return None