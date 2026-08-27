import time
from google import genai
from google.genai import types
from settings import GEMINI_API_KEY

client = genai.Client(api_key=GEMINI_API_KEY)


def _generate_with_retry(model: str, contents: list, max_retries: int = 3, delay: int = 5) -> str:
    """Отправляет запрос в Gemini с повторными попытками при временной недоступности."""
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents
            )
            return response.text.strip()
        except Exception as e:
            last_error = e
            error_text = str(e)

            # Повторяем только при временных ошибках (503, перегрузка)
            if "503" in error_text or "UNAVAILABLE" in error_text:
                print(f"Gemini перегружен, попытка {attempt}/{max_retries}, жду {delay} сек...")
                time.sleep(delay)
                continue
            else:
                # Другие ошибки — не повторяем, сразу возвращаем
                break

    return f"Ошибка при обращении к Gemini после {max_retries} попыток: {last_error}"


def ask_gemini_with_image(text: str, image_bytes: bytes = None, mime_type: str = "image/jpeg",
                           model: str = "gemini-3.5-flash-lite") -> str:
    contents = []
    if image_bytes:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
    contents.append(text if text else "Опиши, что на изображении.")

    return _generate_with_retry(model, contents)


def ask_gemini_with_images(text: str, images: list, model: str = "gemini-3.5-flash-lite") -> str:
    contents = [text if text else "Опиши, что на изображениях."]
    for img in images:
        contents.append(types.Part.from_bytes(data=img["data"], mime_type=img["mime_type"]))

    return _generate_with_retry(model, contents)