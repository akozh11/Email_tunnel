"""
Персистентное состояние опроса для каждого почтового аккаунта.

Хранит:
  - last_uid: последний обработанный/пропущенный IMAP UID (инкрементальный опрос)
  - uidvalidity: значение UIDVALIDITY на момент последнего сохранения
                 (если оно изменилось — сервер пересоздал mailbox, все старые
                 UID недействительны, нужно сбросить last_uid)
  - processed_message_ids: небольшой буфер последних Message-ID для дедупликации
    на случай, если один и тот же UID случайно попадётся дважды подряд
"""

import json
import threading
from collections import deque
from pathlib import Path
from typing import Optional

from config import STATE_DIR

_MAX_PROCESSED_IDS = 500


class StateManager:
    def __init__(self, account_name: str):
        self._path: Path = STATE_DIR / f"{account_name}.json"
        self._lock = threading.Lock()
        self.last_uid: int = 0
        self.uidvalidity: Optional[str] = None
        self.processed_message_ids: deque = deque(maxlen=_MAX_PROCESSED_IDS)
        # UID-ы, которые уже полностью разрешены (ответ отправлен/письмо
        # осознанно пропущено), но ещё не "примыкают" к last_uid из-за того,
        # что более ранний UID в этом же цикле ещё обрабатывается или упал
        # с ошибкой. Нужны, чтобы не терять и не пропускать письма при
        # обработке "не по порядку" или частичном сбое цикла.
        self._resolved_uids: set = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.last_uid = int(data.get("last_uid", 0))
        self.uidvalidity = data.get("uidvalidity")
        ids = data.get("processed_message_ids", [])
        self.processed_message_ids = deque(ids, maxlen=_MAX_PROCESSED_IDS)
        self._resolved_uids = set(data.get("resolved_uids", []))
        self._compact()

    def save(self) -> None:
        with self._lock:
            payload = {
                "last_uid": self.last_uid,
                "uidvalidity": self.uidvalidity,
                "processed_message_ids": list(self.processed_message_ids),
                "resolved_uids": sorted(self._resolved_uids),
            }
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)

    def reset_for_new_mailbox(self, uidvalidity: str) -> None:
        """UIDVALIDITY изменился — старые UID больше не действительны."""
        self.last_uid = 0
        self.uidvalidity = uidvalidity
        self.processed_message_ids.clear()
        self._resolved_uids.clear()
        self.save()

    def has_processed(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self.processed_message_ids

    def mark_processed(self, message_id: str) -> None:
        if message_id:
            self.processed_message_ids.append(message_id)

    def _compact(self) -> None:
        """Продвигает last_uid вперёд, поглощая непрерывную цепочку уже
        разрешённых UID-ов начиная с last_uid + 1."""
        while (self.last_uid + 1) in self._resolved_uids:
            self._resolved_uids.discard(self.last_uid + 1)
            self.last_uid += 1

    def fast_forward(self, uid: int) -> None:
        """Безусловно устанавливает last_uid вперёд. Использовать только там,
        где заведомо нет ещё не разрешённых писем ниже этого UID (например,
        при бутстрапе first-run, когда история сознательно не обрабатывается)."""
        if uid > self.last_uid:
            self.last_uid = uid
            self._resolved_uids = {u for u in self._resolved_uids if u > uid}

    def mark_uid_resolved(self, uid: int) -> None:
        """Помечает UID как полностью разрешённый (ответ отправлен и письмо
        удалено/пропущено осознанно). Продвигает last_uid только если это
        не создаёт "дыр" из ещё не разрешённых более ранних UID."""
        if uid <= self.last_uid:
            return
        self._resolved_uids.add(uid)
        self._compact()

    @property
    def has_stuck_gap(self) -> bool:
        """True, если есть неразрешённые UID-ы, застрявшие позади разрешённых
        (например, письмо, на котором постоянно падает Gemini/SMTP)."""
        return len(self._resolved_uids) > 0