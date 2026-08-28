#!/usr/bin/env python3
"""
Клиент шифрования для друзей.

Примеры:
  python tunnel_cli.py encrypt --secret "моя-фраза" --text "Привет, что такое Python?"
  python tunnel_cli.py encrypt --pubkey keys/server_public.pem --text "вопрос" --image photo.jpg
  python tunnel_cli.py decrypt --secret "моя-фраза" --file reply.txt
  python tunnel_cli.py decrypt --privkey friend_private.pem --file reply.txt
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from crypto import (
    CryptoError,
    decrypt_payload,
    encrypt_payload,
    load_private_key,
    load_public_key,
)


def build_payload(text: str, images: list[str]) -> dict:
    payload = {"text": text or ""}
    if images:
        payload["images"] = []
        for path in images:
            p = Path(path)
            data = p.read_bytes()
            suffix = p.suffix.lower()
            mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "application/octet-stream")
            payload["images"].append({
                "filename": p.name,
                "mime_type": mime,
                "data_b64": base64.b64encode(data).decode("ascii"),
            })
    return payload


def cmd_encrypt(args) -> int:
    text = args.text
    if args.in_file:
        text = Path(args.in_file).read_text(encoding="utf-8")
    if text is None:
        text = sys.stdin.read()

    public_key = load_public_key(args.pubkey) if args.pubkey else None
    if public_key is None and not args.secret:
        print("Нужен --secret или --pubkey", file=sys.stderr)
        return 2

    armored = encrypt_payload(build_payload(text, args.image or []), secret=args.secret, public_key=public_key)
    if args.out:
        Path(args.out).write_text(armored, encoding="utf-8")
        print(f"Записано в {args.out}")
    else:
        sys.stdout.write(armored)
    return 0


def cmd_decrypt(args) -> int:
    if args.file:
        data = Path(args.file).read_text(encoding="utf-8")
    else:
        data = sys.stdin.read()

    private_key = load_private_key(args.privkey) if args.privkey else None
    if private_key is None and not args.secret:
        print("Нужен --secret или --privkey", file=sys.stderr)
        return 2

    try:
        payload = decrypt_payload(data, secret=args.secret, private_key=private_key)
    except CryptoError as exc:
        print(f"Ошибка расшифровки: {exc}", file=sys.stderr)
        return 1

    text = payload.get("text") or ""
    images = payload.get("images") or []
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Текст записан в {args.out}")
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")

    if images and args.images_dir:
        out_dir = Path(args.images_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(images, start=1):
            raw = item.get("data_b64") or item.get("data") or ""
            blob = base64.b64decode(raw) if isinstance(raw, str) else raw
            name = item.get("filename") or f"image_{i}.bin"
            (out_dir / name).write_bytes(blob)
            print(f"Изображение: {out_dir / name}")
    elif images:
        print(f"(в payload ещё {len(images)} изобр.; укажите --images-dir чтобы сохранить)", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Шифрование писем Email Tunnel")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enc = sub.add_parser("encrypt", help="Зашифровать запрос")
    p_enc.add_argument("--secret", help="Общий TUNNEL_SECRET")
    p_enc.add_argument("--pubkey", help="Публичный ключ сервера (PEM)")
    p_enc.add_argument("--text", help="Текст запроса")
    p_enc.add_argument("--in-file", help="Прочитать текст из файла")
    p_enc.add_argument("--image", action="append", help="Приложить изображение (можно несколько)")
    p_enc.add_argument("--out", help="Куда записать блок (по умолчанию stdout)")

    p_dec = sub.add_parser("decrypt", help="Расшифровать ответ")
    p_dec.add_argument("--secret", help="Общий TUNNEL_SECRET")
    p_dec.add_argument("--privkey", help="Свой приватный RSA-ключ")
    p_dec.add_argument("--file", help="Файл с зашифрованным письмом")
    p_dec.add_argument("--out", help="Куда записать расшифрованный текст")
    p_dec.add_argument("--images-dir", help="Каталог для извлечённых изображений")

    args = parser.parse_args()
    if args.cmd == "encrypt":
        return cmd_encrypt(args)
    return cmd_decrypt(args)


if __name__ == "__main__":
    raise SystemExit(main())
