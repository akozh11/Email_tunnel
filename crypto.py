"""
Шифрование полезной нагрузки Email Tunnel.

Два режима (можно использовать вместе):

1. Симметричный — общий секрет TUNNEL_SECRET.
   AES-256-GCM, ключ выводится через HKDF-SHA256.

2. Асимметричный — RSA-OAEP оборачивает случайный AES-ключ.
   Сервер держит свой ключ в keys/server_private.pem.
   Публичные ключи отправителей: keys/senders/<email>.pem

Формат письма (текстовый, безопасный для SMTP):

-----BEGIN EMAIL-TUNNEL-----
v=1
mode=secret|rsa
alg=AES-256-GCM
key=<base64 RSA-wrapped key, только для mode=rsa>
nonce=<base64>
data=<base64 ciphertext+tag>
-----END EMAIL-TUNNEL-----
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BEGIN = "-----BEGIN EMAIL-TUNNEL-----"
END = "-----END EMAIL-TUNNEL-----"
HKDF_SALT = b"email-tunnel-v1"
HKDF_INFO = b"aes-256-gcm"
NONCE_LEN = 12
AES_KEY_LEN = 32


class CryptoError(Exception):
    pass


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except Exception as exc:
        raise CryptoError(f"Некорректный base64: {exc}") from exc


def derive_key(secret: str) -> bytes:
    if not secret or not secret.strip():
        raise CryptoError("TUNNEL_SECRET пуст")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=AES_KEY_LEN,
        salt=HKDF_SALT,
        info=HKDF_INFO,
    ).derive(secret.encode("utf-8"))


def _aes_encrypt(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce, ct


def _aes_decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise CryptoError("Не удалось расшифровать данные (неверный ключ или повреждённый блок)") from exc


def encrypt_bytes_secret(plaintext: bytes, secret: str) -> str:
    nonce, ct = _aes_encrypt(derive_key(secret), plaintext)
    return _pack({"v": "1", "mode": "secret", "alg": "AES-256-GCM", "nonce": _b64e(nonce), "data": _b64e(ct)})


def decrypt_bytes_secret(armored: str, secret: str) -> bytes:
    fields = _unpack(armored)
    if fields.get("mode") not in (None, "secret"):
        raise CryptoError(f"Ожидался mode=secret, получен {fields.get('mode')}")
    return _aes_decrypt(derive_key(secret), _b64d(fields["nonce"]), _b64d(fields["data"]))


def generate_rsa_keypair(bits: int = 3072) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def load_private_key(path: str | Path):
    data = Path(path).read_bytes()
    return serialization.load_pem_private_key(data, password=None)


def load_public_key(path: str | Path):
    data = Path(path).read_bytes()
    return serialization.load_pem_public_key(data)


def encrypt_bytes_rsa(plaintext: bytes, public_key) -> str:
    aes_key = os.urandom(AES_KEY_LEN)
    wrapped = public_key.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    nonce, ct = _aes_encrypt(aes_key, plaintext)
    return _pack({
        "v": "1",
        "mode": "rsa",
        "alg": "RSA-OAEP-SHA256+AES-256-GCM",
        "key": _b64e(wrapped),
        "nonce": _b64e(nonce),
        "data": _b64e(ct),
    })


def decrypt_bytes_rsa(armored: str, private_key) -> bytes:
    fields = _unpack(armored)
    if fields.get("mode") != "rsa":
        raise CryptoError(f"Ожидался mode=rsa, получен {fields.get('mode')}")
    if "key" not in fields:
        raise CryptoError("В блоке нет обёрнутого AES-ключа")
    try:
        aes_key = private_key.decrypt(
            _b64d(fields["key"]),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
    except Exception as exc:
        raise CryptoError("Не удалось развернуть AES-ключ (неверный RSA-ключ)") from exc
    return _aes_decrypt(aes_key, _b64d(fields["nonce"]), _b64d(fields["data"]))


def _pack(fields: dict[str, str]) -> str:
    lines = [BEGIN]
    for k, v in fields.items():
        lines.append(f"{k}={v}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def _unpack(armored: str) -> dict[str, str]:
    block = extract_block(armored)
    if block is None:
        raise CryptoError("В тексте нет блока EMAIL-TUNNEL")
    fields: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("-----"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        fields[k.strip()] = v.strip()
    if "nonce" not in fields or "data" not in fields:
        raise CryptoError("В блоке нет полей nonce/data")
    return fields


_BLOCK_RE = re.compile(
    r"-----BEGIN EMAIL-TUNNEL-----\s*(.*?)\s*-----END EMAIL-TUNNEL-----",
    re.DOTALL,
)


def extract_block(text: str) -> Optional[str]:
    if not text:
        return None
    m = _BLOCK_RE.search(text)
    return m.group(0) if m else None


def is_encrypted(text: str) -> bool:
    return extract_block(text) is not None


def encrypt_payload(payload: dict[str, Any], *, secret: Optional[str] = None, public_key=None) -> str:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if public_key is not None:
        return encrypt_bytes_rsa(raw, public_key)
    if secret:
        return encrypt_bytes_secret(raw, secret)
    raise CryptoError("Не задан ни TUNNEL_SECRET, ни публичный ключ")


def decrypt_payload(
    text: str,
    *,
    secret: Optional[str] = None,
    private_key=None,
) -> dict[str, Any]:
    fields_mode = None
    try:
        fields_mode = _unpack(text).get("mode", "secret")
    except CryptoError:
        raise

    raw: Optional[bytes] = None
    errors: list[str] = []

    if fields_mode == "rsa":
        if private_key is None:
            raise CryptoError("Письмо зашифровано RSA, но приватный ключ сервера не загружен")
        raw = decrypt_bytes_rsa(text, private_key)
    else:
        if not secret:
            raise CryptoError("Письмо зашифровано общим секретом, но TUNNEL_SECRET не задан")
        raw = decrypt_bytes_secret(text, secret)

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CryptoError(f"Расшифрованный payload не JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CryptoError("Расшифрованный payload должен быть объектом JSON")
    return data


def sender_public_key_path(keys_dir: str | Path, email: str) -> Path:
    safe = email.strip().lower().replace("/", "_")
    return Path(keys_dir) / "senders" / f"{safe}.pem"


def try_load_sender_public_key(keys_dir: str | Path, email: str):
    path = sender_public_key_path(keys_dir, email)
    if path.is_file():
        return load_public_key(path)
    return None
