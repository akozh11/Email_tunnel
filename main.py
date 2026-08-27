import time
from settings import MAIL_USER
from gemini import ask_gemini_with_image, ask_gemini_with_images

from mail import fetch_and_purge_self_sent_with_images, append_reply_to_inbox
POLL_INTERVAL_SECONDS = 10


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

        if images:
            # Все фото из письма отправляются в одном запросе
            answer = ask_gemini_with_images(text=text, images=images)
            request_summary = f"{text} [+ {len(images)} фото]"
        else:
            answer = ask_gemini_with_image(text=text)
            request_summary = text

        results.append({"request": request_summary, "response": answer})

    return results


def run_once():
    results = process_incoming_requests()

    if not results:
        print("Новых запросов нет.")
        return

    for item in results:
        print(f"\n=== Запрос ===\n{item['request']}")
        print(f"\n=== Ответ ===\n{item['response']}")
        print("-" * 50)

        print(f"DEBUG: пытаюсь отправить письмо на {MAIL_USER}...")
        try:
            append_reply_to_inbox(
                subject="Ответ от нейросети",
                body_text=item["response"]
            )
            print("DEBUG: письмо отправлено успешно")
        except Exception as e:
            print(f"DEBUG: ОШИБКА при отправке письма: {e}")


def run_forever():
    """Бесконечный цикл опроса почты с заданным интервалом."""
    print(f"Email-tunnel запущен. Опрос каждые {POLL_INTERVAL_SECONDS} сек. Ctrl+C для остановки.")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Ошибка в цикле обработки: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()