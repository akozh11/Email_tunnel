import base64
import imaplib
import smtplib
import email
from email.header import decode_header
from email.utils import parseaddr
from email.mime.text import MIMEText
from datetime import datetime

from settings import MAIL_USER, MAIL_PASS, IMAP_SERVER, IMAP_PORT, SMTP_SERVER, SMTP_PORT
from enc_config import (
    ENCRYPTION_ENABLED,
    ENCRYPTION_REQUIRED,
    TUNNEL_SECRET,
    KEYS_DIR,
    encryption_ready,
    server_private_key,
)
from crypto import (
    CryptoError,
    decrypt_payload,
    encrypt_payload,
    is_encrypted,
    try_load_sender_public_key,
)


def decode_mime_str(s):
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


def get_body_and_images(msg):
    """
    Возвращает (текст письма, список изображений).
    Каждое изображение — словарь {filename, mime_type, data (bytes)}.
    """
    body = ""
    images = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")

            if content_type.startswith("image/"):
                payload = part.get_payload(decode=True)
                if payload:
                    filename = part.get_filename() or "image.jpg"
                    images.append({
                        "filename": decode_mime_str(filename),
                        "mime_type": content_type,
                        "data": payload
                    })
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")

    return body.strip(), images


def _images_from_payload(payload_images) -> list:
    out = []
    if not payload_images:
        return out
    for item in payload_images:
        if not isinstance(item, dict):
            continue
        raw = item.get("data") or item.get("data_b64")
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                data = base64.b64decode(raw)
            except Exception:
                continue
        elif isinstance(raw, bytes):
            data = raw
        else:
            continue
        out.append({
            "filename": item.get("filename") or "image.jpg",
            "mime_type": item.get("mime_type") or "image/jpeg",
            "data": data,
        })
    return out


def decrypt_incoming_letter(from_email: str, body: str, images: list):
    """
    Если тело — зашифрованный блок, расшифровывает JSON-payload.
    Возвращает (text, images, was_encrypted).
    """
    if not is_encrypted(body):
        if ENCRYPTION_REQUIRED:
            raise CryptoError("Письмо не зашифровано, а ENCRYPTION_REQUIRED=True")
        return body, images, False

    if not encryption_ready():
        raise CryptoError("Получен зашифрованный блок, но не задан TUNNEL_SECRET и нет RSA-ключа")

    payload = decrypt_payload(
        body,
        secret=TUNNEL_SECRET,
        private_key=server_private_key(),
    )
    text = payload.get("text") or payload.get("message") or ""
    payload_images = _images_from_payload(payload.get("images"))
    merged = payload_images if payload_images else images
    return str(text), merged, True


def encrypt_outgoing_text(to_addr: str, body_text: str) -> str:
    if not ENCRYPTION_ENABLED or not encryption_ready():
        return body_text

    payload = {"text": body_text}
    sender_key = try_load_sender_public_key(KEYS_DIR, to_addr)
    if sender_key is not None:
        return encrypt_payload(payload, public_key=sender_key)
    if TUNNEL_SECRET:
        return encrypt_payload(payload, secret=TUNNEL_SECRET)
    return body_text


def fetch_and_purge_allowed_with_images(allowed_senders=None, mailbox="INBOX"):
    """
    Забирает письма только с адресов из allowed_senders.
    Возвращает список словарей: from, subject, text, images, encrypted.
    Обработанные письма удаляет.
    """
    if allowed_senders is None:
        from settings import ALLOWED_SENDERS
        allowed_senders = ALLOWED_SENDERS

    allowed = {addr.lower().strip() for addr in allowed_senders if addr}

    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    imap.login(MAIL_USER, MAIL_PASS)
    imap.select(mailbox)

    status, data = imap.search(None, "ALL")
    if status != "OK":
        imap.logout()
        return []

    msg_ids = data[0].split()
    results = []
    ids_to_delete = []

    for msg_id in msg_ids:
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        if msg.get("X-Email-Tunnel-Type") == "reply":
            continue

        from_raw = decode_mime_str(msg.get("From"))
        _, from_email = parseaddr(from_raw)
        from_email = (from_email or "").lower().strip()

        if from_email not in allowed:
            continue

        body, images = get_body_and_images(msg)
        subject = decode_mime_str(msg.get("Subject"))

        try:
            text, images, encrypted = decrypt_incoming_letter(from_email, body, images)
        except CryptoError as exc:
            print(f"Пропуск письма от {from_email}: {exc}")
            ids_to_delete.append(msg_id)
            continue

        if ENCRYPTION_REQUIRED and not encrypted:
            print(f"Пропуск незашифрованного письма от {from_email}")
            ids_to_delete.append(msg_id)
            continue

        results.append({
            "from": from_email,
            "subject": subject,
            "text": text,
            "images": images,
            "encrypted": encrypted,
        })
        ids_to_delete.append(msg_id)

    for msg_id in ids_to_delete:
        imap.store(msg_id, "+FLAGS", "\\Deleted")
    if ids_to_delete:
        imap.expunge()

    imap.logout()
    return results


def send_reply(to_addr: str, subject: str, body_text: str):
    body_text = encrypt_outgoing_text(to_addr, body_text)
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_USER
    msg["To"] = to_addr
    msg["X-Email-Tunnel-Type"] = "reply"
    if ENCRYPTION_ENABLED and encryption_ready() and is_encrypted(body_text):
        msg["X-Email-Tunnel-Enc"] = "v1"

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(MAIL_USER, MAIL_PASS)
        server.sendmail(MAIL_USER, to_addr, msg.as_string())


def append_reply_to_inbox(subject: str, body_text: str, mailbox="INBOX"):
    body_text = encrypt_outgoing_text(MAIL_USER, body_text)
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_USER
    msg["To"] = MAIL_USER
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["X-Email-Tunnel-Type"] = "reply"

    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    imap.login(MAIL_USER, MAIL_PASS)

    imap.append(
        mailbox,
        None,
        imaplib.Time2Internaldate(datetime.now().timetuple()),
        msg.as_bytes()
    )

    imap.logout()