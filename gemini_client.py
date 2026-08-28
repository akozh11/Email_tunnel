"""
Обёртка над Gemini API.

По сравнению с исходной версией:
  - убран мёртвый параметр image_bytes у ask_gemini_with_image (никогда не
    передавался вызывающим кодом) — вместо двух похожих функций осталась
    одна универсальная ask_gemini(text, images, model);
  - модель теперь передаётся снаружи (из конфигурации конкретного аккаунта),
    а не захардкожена.
"""

import time
from typing import List, Optional

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-3.5-flash-lite"

_client: Optional[genai.Client] = None


def init_client(api_key: str) -> None:
    global _client
    _client = genai.Client(api_key=api_key)


def _get_client() -> genai.Client:
    if _client is None:
        raise RuntimeError("gemini_client.init_client(api_key) не был вызван перед использованием.")
    return _client


def _generate_with_retry(model: str, contents: list, max_retries: int = 3, delay: int = 5) -> str:
    """Отправляет запрос в Gemini с повторными попытками при временной недоступности."""
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            response = _get_client().models.generate_content(
                model=model,
                contents=contents,
            )
            text = (response.text or "").strip()
            return text or "Модель вернула пустой ответ."
        except Exception as e:  # noqa: BLE001 - хотим перехватывать любые ошибки клиента Gemini
            last_error = e
            error_text = str(e)

            if "503" in error_text or "UNAVAILABLE" in error_text:
                print(f"Gemini перегружен, попытка {attempt}/{max_retries}, жду {delay} сек...")
                time.sleep(delay)
                continue
            break

    raise RuntimeError(f"Ошибка при обращении к Gemini после {max_retries} попыток: {last_error}") from last_error


def ask_gemini(text: str, images: Optional[List[dict]] = None, model: str = DEFAULT_MODEL) -> str:
    """
    Единая точка входа: отправляет текст и (опционально) список изображений
    вида {"data": bytes, "mime_type": str} в Gemini и возвращает текст ответа.
    """
    images = images or []

    if not text.strip() and not images:
        return "Пустой запрос — нечего обрабатывать."

    default_prompt = "Опиши, что на изображениях." if images else "Ответь на запрос."
    contents: list = [text.strip() or default_prompt]

    for img in images:
        contents.append(types.Part.from_bytes(data=img["data"], mime_type=img["mime_type"]))

    return _generate_with_retry(model, contents)