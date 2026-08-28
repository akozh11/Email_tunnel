#!/usr/bin/env python3
"""
Клиент отправки зашифрованного письма в Email Tunnel.

Примеры:
  python send_client.py --text "Что такое список в Python?"
  python send_client.py --text "Что на фото?" --image photo.jpg
  python send_client.py --interactive
  python send_client.py --in-file question.txt --subject "Вопрос"
"""

from __future__ import annotations

import argparse
import base64
import smtplib
import ssl
import sys
from email.mime.text import MIMEText
from pathlib import Path

from crypto import encrypt_payload, load_public_key


class ClientError(Exception):
    pass


def load_client_config():
    try:
        import client_settings as cfg
    except ImportError:
        try:
            import settings as cfg
        except ImportError as exc:
            raise ClientError(
                "Нет client_settings.py. Скопируйте client_settings.example.py "
                "в client_settings.py и заполните поля."
            ) from exc

    def get(*names, default=""):
        for name in names:
            if hasattr(cfg, name):
                value = getattr(cfg, name)
                if value not in (None, ""):
                    return value
        return default

    conf = {
        "tunnel_email": get("TUNNEL_EMAIL", "MAIL_USER"),
        "client_email": get("CLIENT_EMAIL", "MAIL_USER"),
        "client_pass": get("CLIENT_PASS", "MAIL_PASS"),
        "smtp_server": get("SMTP_SERVER", default="smtp.mail.ru"),
        "smtp_port": int(get("SMTP_PORT", default=465)),
        "secret": get("TUNNEL_SECRET") or None,
        "pubkey_path": get("SERVER_PUBLIC_KEY", "RSA_PUBLIC_KEY_PATH") or None,
        "subject": get("DEFAULT_SUBJECT", default="Email Tunnel"),
    }
    missing = [k for k in ("tunnel_email", "client_email", "client_pass") if not conf[k]]
    if missing:
        raise ClientError(f"В настройках клиента не заполнены: {', '.join(missing)}")
    return conf


def guess_mime(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")


def build_payload(text: str, images: list[str]) -> dict:
    payload = {"text": text or ""}
    if not images:
        return payload
    payload["images"] = []
    for raw_path in images:
        path = Path(raw_path)
        if not path.is_file():
            raise ClientError(f"Файл не найден: {path}")
        payload["images"].append({
            "filename": path.name,
            "mime_type": guess_mime(path),
            "data_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
        })
    return payload


def encrypt_message(payload: dict, secret: str | None, pubkey_path: str | None) -> str:
    public_key = None
    if pubkey_path:
        path = Path(pubkey_path)
        if path.is_file():
            public_key = load_public_key(path)
        elif secret is None:
            raise ClientError(f"Публичный ключ не найден: {path}")

    if public_key is None and not secret:
        raise ClientError("Задайте TUNNEL_SECRET или SERVER_PUBLIC_KEY в client_settings.py")

    if public_key is not None:
        return encrypt_payload(payload, public_key=public_key)
    return encrypt_payload(payload, secret=secret)


def send_encrypted_email(
    *,
    smtp_server: str,
    smtp_port: int,
    client_email: str,
    client_pass: str,
    tunnel_email: str,
    subject: str,
    body: str,
) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = client_email
    msg["To"] = tunnel_email
    msg["X-Email-Tunnel-Enc"] = "v1"

    raw = msg.as_string()
    context = ssl.create_default_context()

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
            server.login(client_email, client_pass)
            server.sendmail(client_email, tunnel_email, raw)
        return

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(client_email, client_pass)
        server.sendmail(client_email, tunnel_email, raw)


def read_text(args) -> str:
    if args.in_file:
        return Path(args.in_file).read_text(encoding="utf-8")
    if args.text is not None:
        return args.text
    if args.interactive or sys.stdin.isatty():
        print("Введите текст письма. Пустая строка + Enter — отправка, Ctrl+C — отмена.")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "" and lines:
                break
            if line == "" and not lines:
                continue
            lines.append(line)
        return "\n".join(lines).strip()
    return sys.stdin.read()


def parse_args():
    parser = argparse.ArgumentParser(description="Отправка зашифрованного письма в Email Tunnel")
    parser.add_argument("--text", help="Текст запроса")
    parser.add_argument("--in-file", help="Прочитать текст из файла")
    parser.add_argument("--image", action="append", default=[], help="Изображение (можно несколько раз)")
    parser.add_argument("--subject", help="Тема письма")
    parser.add_argument("--to", help="Переопределить адрес туннеля")
    parser.add_argument("--interactive", action="store_true", help="Ввести текст вручную")
    parser.add_argument("--dry-run", action="store_true", help="Только напечатать блок, не отправлять")
    parser.add_argument("--out", help="Сохранить зашифрованный блок в файл")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conf = load_client_config()
    text = read_text(args)
    if not text.strip() and not args.image:
        print("Пустое письмо: нужен текст или --image", file=sys.stderr)
        return 2

    payload = build_payload(text, args.image)
    armored = encrypt_message(payload, conf["secret"], conf["pubkey_path"])

    if args.out:
        Path(args.out).write_text(armored, encoding="utf-8")
        print(f"Блок сохранён в {args.out}")

    if args.dry_run:
        sys.stdout.write(armored)
        return 0

    to_addr = args.to or conf["tunnel_email"]
    subject = args.subject or conf["subject"]
    print(f"Отправка зашифрованного письма: {conf['client_email']} -> {to_addr}")
    send_encrypted_email(
        smtp_server=conf["smtp_server"],
        smtp_port=conf["smtp_port"],
        client_email=conf["client_email"],
        client_pass=conf["client_pass"],
        tunnel_email=to_addr,
        subject=subject,
        body=armored,
    )
    print("Письмо отправлено.")
    return 0


def compose_and_send(text: str, images: list[str] | None = None, subject: str | None = None, to_addr: str | None = None) -> str:
    """Шифрует и отправляет письмо. Возвращает адрес получателя."""
    conf = load_client_config()
    images = images or []
    if not (text or "").strip() and not images:
        raise ClientError("Пустое письмо: нужен текст или изображение")
    armored = encrypt_message(build_payload(text or "", images), conf["secret"], conf["pubkey_path"])
    dest = to_addr or conf["tunnel_email"]
    send_encrypted_email(
        smtp_server=conf["smtp_server"],
        smtp_port=conf["smtp_port"],
        client_email=conf["client_email"],
        client_pass=conf["client_pass"],
        tunnel_email=dest,
        subject=subject or conf["subject"],
        body=armored,
    )
    return dest


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
