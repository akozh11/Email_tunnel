"""
Загрузка конфигурации проекта.

Секреты (пароли, ключи API) хранятся в .env и НЕ попадают в config.json —
в config.json вместо них используются плейсхолдеры вида ${VAR_NAME},
которые подставляются из переменных окружения при загрузке.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Подхватываем .env в os.environ (если файла нет — просто ничего не делает)
load_dotenv()

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_DIR = BASE_DIR / "state"

VALID_REPLY_MODES = {"append_inbox", "smtp"}


class ConfigError(RuntimeError):
    """Ошибка конфигурации: отсутствует обязательное поле или переменная окружения."""


def _expand_env_vars(raw_text: str) -> str:
    def _replace(match: "re.Match[str]") -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ConfigError(
                f"Переменная окружения '{var_name}', указанная в config.json, "
                f"не найдена ни в .env, ни в окружении процесса."
            )
        return value

    return _ENV_VAR_RE.sub(_replace, raw_text)


@dataclass
class Account:
    name: str
    mail_user: str
    mail_pass: str
    imap_server: str
    imap_port: int = 993
    smtp_server: str = ""
    smtp_port: int = 465
    smtp_use_ssl: bool = True

    # True  -> обрабатываем только письма, отправленные ящиком самому себе
    #          (личный "командный" инбокс, как в исходной версии проекта)
    # False -> обрабатываем письма от любых внешних отправителей
    self_only: bool = True

    # "append_inbox" -> ответ дописывается в тот же INBOX помеченным письмом
    # "smtp"         -> ответ реально отправляется по SMTP на адрес отправителя
    reply_mode: str = "append_inbox"

    poll_interval_seconds: int = 15
    gemini_model: str = "gemini-3.5-flash-lite"
    max_replies_per_sender_per_hour: int = 10
    max_messages_per_cycle: int = 20

    # Если False — при первом запуске бот не будет обрабатывать существующие
    # в ящике письма, а начнёт отслеживать только новые (защита от массовой
    # рассылки ответов на всю историю письма при первом подключении внешнего ящика).
    process_backlog_on_first_run: bool = False


@dataclass
class AppConfig:
    gemini_api_key: str
    accounts: List[Account] = field(default_factory=list)


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Файл конфигурации не найден: {path}")

    raw_text = path.read_text(encoding="utf-8")
    expanded_text = _expand_env_vars(raw_text)

    try:
        raw = json.loads(expanded_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json содержит некорректный JSON: {exc}") from exc

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ConfigError("Переменная окружения GEMINI_API_KEY не задана (проверьте .env).")

    defaults = {
        "poll_interval_seconds": raw.get("poll_interval_seconds", 15),
        "gemini_model": raw.get("gemini_model", "gemini-3.5-flash-lite"),
        "max_replies_per_sender_per_hour": raw.get("max_replies_per_sender_per_hour", 10),
        "max_messages_per_cycle": raw.get("max_messages_per_cycle", 20),
    }

    raw_accounts = raw.get("accounts")
    if not raw_accounts or not isinstance(raw_accounts, list):
        raise ConfigError("config.json должен содержать непустой список 'accounts'.")

    accounts: List[Account] = []
    seen_names = set()

    for idx, item in enumerate(raw_accounts):
        name = item.get("name") or f"account_{idx + 1}"
        if name in seen_names:
            raise ConfigError(f"Повторяющееся имя аккаунта в config.json: '{name}'")
        seen_names.add(name)

        for required in ("mail_user", "mail_pass", "imap_server"):
            if not item.get(required):
                raise ConfigError(f"Аккаунт '{name}': отсутствует обязательное поле '{required}'.")

        reply_mode = item.get("reply_mode", "append_inbox")
        if reply_mode not in VALID_REPLY_MODES:
            raise ConfigError(
                f"Аккаунт '{name}': недопустимый reply_mode='{reply_mode}', "
                f"ожидается одно из {VALID_REPLY_MODES}."
            )
        if reply_mode == "smtp" and not item.get("smtp_server"):
            raise ConfigError(f"Аккаунт '{name}': reply_mode='smtp' требует поля 'smtp_server'.")

        account = Account(
            name=name,
            mail_user=item["mail_user"],
            mail_pass=item["mail_pass"],
            imap_server=item["imap_server"],
            imap_port=int(item.get("imap_port", 993)),
            smtp_server=item.get("smtp_server", ""),
            smtp_port=int(item.get("smtp_port", 465)),
            smtp_use_ssl=bool(item.get("smtp_use_ssl", True)),
            self_only=bool(item.get("self_only", True)),
            reply_mode=reply_mode,
            poll_interval_seconds=int(item.get("poll_interval_seconds", defaults["poll_interval_seconds"])),
            gemini_model=item.get("gemini_model", defaults["gemini_model"]),
            max_replies_per_sender_per_hour=int(
                item.get("max_replies_per_sender_per_hour", defaults["max_replies_per_sender_per_hour"])
            ),
            max_messages_per_cycle=int(item.get("max_messages_per_cycle", defaults["max_messages_per_cycle"])),
            process_backlog_on_first_run=bool(item.get("process_backlog_on_first_run", False)),
        )
        accounts.append(account)

    STATE_DIR.mkdir(exist_ok=True)

    return AppConfig(gemini_api_key=gemini_api_key, accounts=accounts)