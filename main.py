import time
import logging

from settings import MAIL_USER, POLL_INTERVAL_SECONDS
from gemini import ask_gemini_with_image, ask_gemini_with_images
from mail import fetch_and_purge_self_sent_with_images, append_reply_to_inbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def process_incoming_requests():
    """
    Забирает новые письма от себя, отправляет каждое (текст + фото, если есть)
    в Gemini, возвращает список результатов.
    """
    letters = fetch_and_purge_self_sent_with_images()

    if not letters:
        return []

    results = []
    for letter in letters:
        text = letter["text"]
        images = letter["images"]

        if not text.strip() and not images:
            continue

        try:
            if images:
                # Все фото из письма отправляются в одном запросе
                answer = ask_gemini_with_images(text=text, images=images)
                request_summary = f"{text} [+ {len(images)} фото]"
            else:
                answer = ask_gemini_with_image(text=text)
                request_summary = text
        except Exception as e:
            logger.exception("Ошибка при обращении к Gemini")
            answer = f"Не удалось получить ответ от нейросети: {e}"
            request_summary = text

        results.append({"request": request_summary, "response": answer})

    return results


def run_once():
    results = process_incoming_requests()

    if not results:
        logger.info("Новых запросов нет.")
        return

    for item in results:
        logger.info("Запрос: %s", item["request"])
        logger.info("Ответ: %s", item["response"])

        try:
            append_reply_to_inbox(
                subject="Ответ от нейросети",
                body_text=item["response"]
            )
            logger.info("Письмо с ответом добавлено во входящие (%s)", MAIL_USER)
        except Exception as e:
            logger.exception("Ошибка при отправке письма: %s", e)


def run_forever():
    """Бесконечный цикл опроса почты с заданным интервалом."""
    logger.info("Email-tunnel запущен. Опрос каждые %d сек. Ctrl+C для остановки.", POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Ошибка в цикле обработки")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()