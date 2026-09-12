"""
bot.py — Telegram-бот для управления доверенными почтовыми адресами Email Tunnel.

Возможности:
  • добавление и удаление адресов, которым разрешено присылать запросы на обработку ИИ;
  • карточка по каждому адресу: сколько запросов отправлено и история последних
    запросов/ответов.

Данные хранятся в двух JSON-файлах в каталоге data/:
  • allowed_senders.json — список доверенных адресов;
  • requests_history.json — история запросов по каждому адресу.

При каждом добавлении/удалении адреса бот также правит список
ALLOWED_SENDERS прямо в settings.py (только сам список — остальной
файл, включая пароли и ключи, не трогается), чтобы он всегда совпадал
с тем, что видно в боте.

Как подключить к остальному проекту — см. комментарий в самом низу файла.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from settings import TG_BOT_TOKEN, OWNER_ID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("senders_bot")


# ──────────────────────────── Хранилище ────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

SENDERS_FILE = DATA_DIR / "allowed_senders.json"
HISTORY_FILE = DATA_DIR / "requests_history.json"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SETTINGS_FILE = BASE_DIR / "settings.py"
ALLOWED_SENDERS_RE = re.compile(r"ALLOWED_SENDERS\s*=\s*\[[^\]]*\]", re.DOTALL)

SENDERS_PAGE_SIZE = 8
HISTORY_PAGE_SIZE = 5
PREVIEW_LEN = 160
MAX_HISTORY_PER_SENDER = 300


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Не удалось прочитать %s: %s", path, e)
        return default


def _save_json(path: Path, data) -> None:
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def _sender_id(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


def load_senders() -> dict:
    """{sender_id: {"email": str, "note": str, "added_at": str, "added_by": int}}"""
    return _load_json(SENDERS_FILE, {})


def save_senders(data: dict) -> None:
    _save_json(SENDERS_FILE, data)


def load_history() -> dict:
    """{email: [ {"timestamp": str, "subject": str, "request": str, "response": str}, ... ]}"""
    return _load_json(HISTORY_FILE, {})


def save_history(data: dict) -> None:
    _save_json(HISTORY_FILE, data)


def get_allowed_senders_list() -> list[str]:
    """
    Используется в main.py вместо статического settings.ALLOWED_SENDERS —
    возвращает актуальный список доверенных адресов.
    """
    return [item["email"] for item in load_senders().values()]


def _format_allowed_senders_block(emails: list[str]) -> str:
    if not emails:
        return "ALLOWED_SENDERS = [\n]"
    body = ",\n".join(f'    "{e}"' for e in emails)
    return f"ALLOWED_SENDERS = [\n{body},\n]"


def _sync_settings_allowed_senders() -> None:
    """
    Зеркалирует текущий список доверенных адресов (data/allowed_senders.json)
    в переменную ALLOWED_SENDERS внутри settings.py. Меняется только сам
    список — остальной файл (пароли, ключи, комментарии) не трогается.
    Вызывается автоматически из add_sender()/remove_sender().

    Это best-effort операция: если settings.py не найден, повреждён или
    недоступен для записи — ошибка только логируется, работа бота не
    прерывается, т.к. источником истины всё равно остаётся
    data/allowed_senders.json.
    """
    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Не удалось прочитать settings.py для синхронизации: %s", e)
        return

    if not ALLOWED_SENDERS_RE.search(text):
        logger.warning("В settings.py не найден блок ALLOWED_SENDERS — синхронизация пропущена")
        return

    emails = sorted(get_allowed_senders_list())
    new_block = _format_allowed_senders_block(emails)
    new_text = ALLOWED_SENDERS_RE.sub(lambda _m: new_block, text, count=1)

    if new_text == text:
        return

    try:
        tmp_path = SETTINGS_FILE.with_suffix(".py.tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(SETTINGS_FILE)
    except OSError as e:
        logger.warning("Не удалось записать settings.py: %s", e)
        return

    logger.info("settings.py: ALLOWED_SENDERS синхронизирован (%d адрес(ов))", len(emails))


def add_sender(email: str, note: str, added_by: int) -> bool:
    """Возвращает False, если такой адрес уже есть в списке."""
    email = email.lower().strip()
    senders = load_senders()
    sid = _sender_id(email)
    if sid in senders:
        return False
    senders[sid] = {
        "email": email,
        "note": note or "",
        "added_at": datetime.now().isoformat(timespec="seconds"),
        "added_by": added_by,
    }
    save_senders(senders)
    _sync_settings_allowed_senders()
    return True


def remove_sender(sender_id: str) -> str | None:
    """Удаляет адрес из доверенных, возвращает удалённый e-mail (или None)."""
    senders = load_senders()
    item = senders.pop(sender_id, None)
    if item is None:
        return None
    save_senders(senders)
    _sync_settings_allowed_senders()
    return item["email"]


def log_request(email: str, subject: str, request_text: str, response_text: str) -> None:
    """
    Используется в main.py после обработки письма — добавляет запись
    в карточку адреса. Хранит не больше MAX_HISTORY_PER_SENDER записей
    на адрес, чтобы файл не разрастался бесконечно.
    """
    email = email.lower().strip()
    history = load_history()
    entries = history.setdefault(email, [])
    entries.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "subject": (subject or "")[:200],
        "request": (request_text or "")[:2000],
        "response": (response_text or "")[:2000],
    })
    if len(entries) > MAX_HISTORY_PER_SENDER:
        del entries[: len(entries) - MAX_HISTORY_PER_SENDER]
    save_history(history)


def _shorten(text: str, limit: int = PREVIEW_LEN) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


# ──────────────────────────── FSM ────────────────────────────

class AddSender(StatesGroup):
    waiting_email = State()
    waiting_note = State()


# ──────────────────────────── Клавиатуры и рендер экранов ────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список адресов", callback_data="list:0")],
        [InlineKeyboardButton(text="➕ Добавить адрес", callback_data="add")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ])


def kb_delete_confirm(sender_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_yes:{sender_id}"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data=f"card:{sender_id}:0"),
        ],
    ])


def render_senders_list(page: int) -> tuple[str, InlineKeyboardMarkup]:
    senders = load_senders()
    items = sorted(senders.items(), key=lambda kv: kv[1]["email"])

    if not items:
        text = "Список доверенных адресов пуст."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить адрес", callback_data="add")],
        ])
        return text, kb

    total_pages = max(1, (len(items) + SENDERS_PAGE_SIZE - 1) // SENDERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * SENDERS_PAGE_SIZE: (page + 1) * SENDERS_PAGE_SIZE]

    history = load_history()
    rows: list[list[InlineKeyboardButton]] = []
    for sid, item in chunk:
        count = len(history.get(item["email"], []))
        label = item["note"] or item["email"]
        rows.append([InlineKeyboardButton(
            text=f"{label} ({count})",
            callback_data=f"card:{sid}:0",
        )])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"list:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"list:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="➕ Добавить адрес", callback_data="add")])

    text = f"<b>Доверенные адреса ({len(items)}):</b>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_sender_card(sender_id: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    senders = load_senders()
    item = senders.get(sender_id)
    if item is None:
        text = "Этот адрес больше не в списке доверенных."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К списку", callback_data="list:0")],
        ])
        return text, kb

    email = item["email"]
    history = list(reversed(load_history().get(email, [])))  # новые сверху
    total = len(history)

    lines = ["<b>📇 Карточка адреса</b>", f"✉️ {html.escape(email)}"]
    if item.get("note"):
        lines.append(f"🏷 {html.escape(item['note'])}")
    lines.append(f"📅 Добавлен: {item['added_at']}")
    lines.append(f"📊 Всего запросов: {total}")

    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    if total == 0:
        lines.append("\nЗапросов пока не было.")
    else:
        chunk = history[page * HISTORY_PAGE_SIZE: (page + 1) * HISTORY_PAGE_SIZE]
        lines.append(f"\n<b>Последние запросы (стр. {page + 1}/{total_pages}):</b>")
        for entry in chunk:
            ts = entry.get("timestamp", "—")
            subject = html.escape(entry.get("subject") or "(без темы)")
            preview = html.escape(_shorten(entry.get("request", "")))
            lines.append(f"\n🕒 {ts}\n<b>{subject}</b>\n{preview}")

    text = "\n".join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    if total > HISTORY_PAGE_SIZE:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"card:{sender_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"card:{sender_id}:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🗑 Удалить адрес", callback_data=f"del:{sender_id}")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="list:0")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ──────────────────────────── Роутеры ────────────────────────────

router = Router()
router.message.filter(F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)

fallback_router = Router()


async def _edit(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


async def _try_add_sender(message: Message, state: FSMContext, email: str) -> None:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        await message.answer(
            "⚠️ Это не похоже на e-mail. Пришлите адрес ещё раз или нажмите «Отмена».",
            reply_markup=kb_cancel(),
        )
        return
    await state.update_data(pending_email=email)
    await state.set_state(AddSender.waiting_note)
    await message.answer(
        f"Адрес: {html.escape(email)}\n\n"
        f"Пришлите короткую подпись (например, имя друга), чтобы было проще узнать "
        f"адрес в списке, или отправьте «-», чтобы пропустить.",
        reply_markup=kb_cancel(),
    )


# ─── команды ───

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Управление доверенными почтовыми адресами Email Tunnel.",
        reply_markup=kb_main_menu(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb_main_menu())


@router.message(Command("list"))
async def cmd_list(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = render_senders_list(0)
    await message.answer(text, reply_markup=kb)


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
    email = (command.args or "").strip()
    if not email:
        await state.set_state(AddSender.waiting_email)
        await message.answer("Пришлите e-mail адрес, который нужно добавить:", reply_markup=kb_cancel())
        return
    await _try_add_sender(message, state, email)


# ─── FSM: добавление адреса ───

@router.message(AddSender.waiting_email)
async def process_email(message: Message, state: FSMContext) -> None:
    await _try_add_sender(message, state, message.text or "")


@router.message(AddSender.waiting_note)
async def process_note(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    email = data.get("pending_email")
    note = (message.text or "").strip()
    if note == "-":
        note = ""
    await state.clear()

    if not email:
        await message.answer("Что-то пошло не так, попробуйте снова.", reply_markup=kb_main_menu())
        return

    added = add_sender(email, note, added_by=message.from_user.id)
    if added:
        logger.info("Добавлен доверенный адрес: %s", email)
        await message.answer(
            f"✅ Адрес {html.escape(email)} добавлен в доверенные "
            f"(data/allowed_senders.json и settings.py).",
            reply_markup=kb_main_menu(),
        )
    else:
        await message.answer(
            f"ℹ️ Адрес {html.escape(email)} уже есть в списке.",
            reply_markup=kb_main_menu(),
        )


# ─── callback-кнопки ───

@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit(callback, "👋 Управление доверенными почтовыми адресами Email Tunnel.", kb_main_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _edit(callback, "Отменено.", kb_main_menu())


@router.callback_query(F.data == "add")
async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSender.waiting_email)
    await _edit(callback, "Пришлите e-mail адрес, который нужно добавить:", kb_cancel())


@router.callback_query(F.data.startswith("list:"))
async def cb_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    page = int(callback.data.split(":")[1])
    text, kb = render_senders_list(page)
    await _edit(callback, text, kb)


@router.callback_query(F.data.startswith("card:"))
async def cb_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, sender_id, page = callback.data.split(":")
    text, kb = render_sender_card(sender_id, int(page))
    await _edit(callback, text, kb)


@router.callback_query(F.data.startswith("del_yes:"))
async def cb_delete_confirmed(callback: CallbackQuery) -> None:
    sender_id = callback.data.split(":", 1)[1]
    email = remove_sender(sender_id)
    if email:
        logger.info("Удалён доверенный адрес: %s", email)
        await callback.answer("Адрес удалён из data/allowed_senders.json и settings.py")
    else:
        await callback.answer("Адрес уже был удалён")
    text, kb = render_senders_list(0)
    await _edit(callback, text, kb)


@router.callback_query(F.data.startswith("del:"))
async def cb_delete_ask(callback: CallbackQuery) -> None:
    sender_id = callback.data.split(":", 1)[1]
    item = load_senders().get(sender_id)
    if item is None:
        await callback.answer("Адрес уже удалён", show_alert=True)
        text, kb = render_senders_list(0)
        await _edit(callback, text, kb)
        return
    await _edit(
        callback,
        f"Удалить {html.escape(item['email'])} из доверенных адресов?",
        kb_delete_confirm(sender_id),
    )


# ─── «заглушки», которые должны стоять последними в router ───

@router.message()
async def fallback_owner_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Не понял команду. Открываю меню:", reply_markup=kb_main_menu())


@router.callback_query()
async def fallback_owner_callback(callback: CallbackQuery) -> None:
    await callback.answer("Неизвестное действие.")


# ─── доступ для всех остальных пользователей ───

@fallback_router.message()
async def deny_message(message: Message) -> None:
    await message.answer("⛔ Доступ к этому боту ограничен.")


@fallback_router.callback_query()
async def deny_callback(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Доступ ограничен", show_alert=True)


# ──────────────────────────── Точка входа ────────────────────────────

async def main() -> None:
    bot = Bot(token=TG_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.include_router(fallback_router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


# ═══════════════════════ Интеграция с Email Tunnel ═══════════════════════
#
# 1) В settings.py добавьте:
#      TG_BOT_TOKEN = "токен от @BotFather"
#      OWNER_ID = 123456789   # ваш Telegram ID (узнать можно, например, у @userinfobot)
#
# 2) В Requirements добавьте:
#      aiogram>=3.4.0
#
# 3) В mail.py вместо статического settings.ALLOWED_SENDERS используйте
#    динамический список из этого файла, чтобы бот реально управлял тем,
#    кому разрешено писать:
#
#      from bot import get_allowed_senders_list
#      letters = fetch_and_purge_allowed_with_images(
#          allowed_senders=get_allowed_senders_list()
#      )
#
# 4) В main.py, в process_incoming_requests(), после формирования словаря
#    results.append({...}) добавьте вызов log_request(), чтобы запрос
#    попал в карточку адреса:
#
#      from bot import log_request
#      ...
#      log_request(
#          email=sender,
#          subject=letter.get("subject") or "",
#          request_text=request_summary,
#          response_text=answer,
#      )
#
# 5) Бот (bot.py) и почтовый цикл (main.py) — два независимых процесса,
#    запускать нужно отдельно, например через systemd/screen/supervisor:
#      python bot.py
#      python main.py