#!/usr/bin/env python3
"""Создаёт RSA-пару сервера и каталог для ключей отправителей."""

from pathlib import Path

from crypto import generate_rsa_keypair


def main():
    keys_dir = Path("keys")
    senders = keys_dir / "senders"
    senders.mkdir(parents=True, exist_ok=True)
    priv_path = keys_dir / "server_private.pem"
    pub_path = keys_dir / "server_public.pem"

    if priv_path.exists() or pub_path.exists():
        print("Ключи уже существуют:")
        print(f"  {priv_path}")
        print(f"  {pub_path}")
        print("Удалите их вручную, если нужно перевыпустить.")
        return

    private_pem, public_pem = generate_rsa_keypair()
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)
    priv_path.chmod(0o600)
    print("Созданы ключи сервера:")
    print(f"  приватный: {priv_path}  (никому не отправляйте)")
    print(f"  публичный: {pub_path}   (раздайте друзьям для шифрования запросов)")
    print(f"Публичные ключи друзей кладите в {senders}/<email>.pem")
    print("чтобы ответы шифровались именно их ключом.")


if __name__ == "__main__":
    main()
