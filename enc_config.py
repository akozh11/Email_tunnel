"""Безопасное чтение настроек шифрования из settings.py."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

try:
    import settings
except ImportError as exc:
    raise SystemExit(
        "Не найден settings.py. Скопируйте settings.example.py в settings.py и заполните поля."
    ) from exc


def _get(name: str, default=None):
    return getattr(settings, name, default)


ENCRYPTION_ENABLED = bool(_get("ENCRYPTION_ENABLED", False))
ENCRYPTION_REQUIRED = bool(_get("ENCRYPTION_REQUIRED", False))
TUNNEL_SECRET = (_get("TUNNEL_SECRET") or "").strip() or None
KEYS_DIR = Path(_get("KEYS_DIR") or "keys")
RSA_PRIVATE_KEY_PATH = Path(_get("RSA_PRIVATE_KEY_PATH") or KEYS_DIR / "server_private.pem")
RSA_PUBLIC_KEY_PATH = Path(_get("RSA_PUBLIC_KEY_PATH") or KEYS_DIR / "server_public.pem")

_private_key = None
_private_key_tried = False


def server_private_key():
    global _private_key, _private_key_tried
    if _private_key_tried:
        return _private_key
    _private_key_tried = True
    if RSA_PRIVATE_KEY_PATH.is_file():
        from crypto import load_private_key
        _private_key = load_private_key(RSA_PRIVATE_KEY_PATH)
    return _private_key


def encryption_ready() -> bool:
    return bool(TUNNEL_SECRET) or server_private_key() is not None
