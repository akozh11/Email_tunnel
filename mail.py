import imaplib
import smtplib
import email
from email.header import decode_header
from email.utils import parseaddr
from email.mime.text import MIMEText
from datetime import datetime

from settings import MAIL_USER, MAIL_PASS, IMAP_SERVER, IMAP_PORT, SMTP_SERVER, SMTP_PORT

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

            # Текстовая часть
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")

            # Изображения (как вложения, так и inline)
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


def fetch_and_purge_self_sent_with_images(mailbox="INBOX"):
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

        # Пропускаем письма-ответы нейросети — не обрабатываем их как запросы
        if msg.get("X-Email-Tunnel-Type") == "reply":
            continue

        from_raw = decode_mime_str(msg.get("From"))
        _, from_email = parseaddr(from_raw)

        if from_email.lower() != MAIL_USER.lower():
            continue

        body, images = get_body_and_images(msg)
        results.append({"text": body, "images": images})
        ids_to_delete.append(msg_id)

    for msg_id in ids_to_delete:
        imap.store(msg_id, "+FLAGS", "\\Deleted")
    if ids_to_delete:
        imap.expunge()

    imap.logout()
    return results

def send_reply(to_addr: str, subject: str, body_text: str):
    """Отправляет ответ письмом через SMTP mail.ru"""
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_USER
    msg["To"] = to_addr

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(MAIL_USER, MAIL_PASS)
        server.sendmail(MAIL_USER, to_addr, msg.as_string())


def append_reply_to_inbox(subject: str, body_text: str, mailbox="INBOX"):
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_USER
    msg["To"] = MAIL_USER
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["X-Email-Tunnel-Type"] = "reply"  # маркер — это ответ, не запрос

    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    imap.login(MAIL_USER, MAIL_PASS)

    imap.append(
        mailbox,
        None,
        imaplib.Time2Internaldate(datetime.now().timetuple()),
        msg.as_bytes()
    )

    imap.logout()