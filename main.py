from mail import fetch_and_purge_allowed_with_images, send_reply
from gemini import ask_gemini_with_image, ask_gemini_with_images
from settings import POLL_INTERVAL_SECONDS
import time

# log_request пишет каждый обработанный запрос в карточку адреса,
# которую показывает Telegram-бот (bot.py). Если aiogram не установлен
# или бот ещё не подключён — почтовый цикл всё равно продолжит работать,
# просто без ведения карточек.
try:
    from bot import log_request
except ImportError:
    log_request = None


def process_incoming_requests():
    letters = fetch_and_purge_allowed_with_images()
    if not letters:
        return []

    results = []
    for letter in letters:
        text = letter["text"]
        images = letter["images"]
        sender = letter["from"]
        subject = letter.get("subject") or "Запрос"

        if not text.strip() and not images:
            continue

        if images:
            answer = ask_gemini_with_images(text=text, images=images)
            request_summary = f"{text} [+ {len(images)} фото]"
        else:
            answer = ask_gemini_with_image(text=text)
            request_summary = text

        if log_request is not None:
            try:
                log_request(
                    email=sender,
                    subject=subject,
                    request_text=request_summary,
                    response_text=answer,
                )
            except Exception as e:
                print(f"DEBUG: не удалось записать запрос в карточку адреса: {e}")

        results.append({
            "from": sender,
            "subject": subject,
            "request": request_summary,
            "response": answer,
        })
    return results


def run_once():
    results = process_incoming_requests()
    if not results:
        return

    for item in results:
        print(f"\n=== От: {item['from']} ===")
        print(f"=== Запрос ===\n{item['request']}")
        print(f"=== Ответ ===\n{item['response']}")
        print("-" * 50)

        to_addr = item["from"]
        print(f"DEBUG: пытаюсь отправить письмо на {to_addr}...")
        try:
            send_reply(
                to_addr=to_addr,
                subject="Ответ от нейросети",
                body_text=item["response"],
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
