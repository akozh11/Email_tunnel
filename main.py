from mail import fetch_and_purge_allowed_with_images, send_reply
from gemini import ask_gemini_with_image, ask_gemini_with_images
from settings import POLL_INTERVAL_SECONDS
from enc_config import ENCRYPTION_ENABLED, ENCRYPTION_REQUIRED, encryption_ready
import time


def process_incoming_requests():
    letters = fetch_and_purge_allowed_with_images()
    if not letters:
        return []

    results = []
    for letter in letters:
        text = letter["text"]
        images = letter["images"]
        sender = letter["from"]

        if not text.strip() and not images:
            continue

        if images:
            answer = ask_gemini_with_images(text=text, images=images)
            request_summary = f"{text} [+ {len(images)} фото]"
        else:
            answer = ask_gemini_with_image(text=text)
            request_summary = text

        results.append({
            "from": sender,
            "subject": letter.get("subject") or "Ответ от нейросети",
            "request": request_summary,
            "response": answer,
            "encrypted": letter.get("encrypted", False),
        })
    return results


def run_once():
    results = process_incoming_requests()
    if not results:
        print("Новых запросов нет.")
        return

    for item in results:
        flag = " [шифр]" if item.get("encrypted") else ""
        print(f"\n=== От: {item['from']}{flag} ===")
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
    enc_state = "выкл"
    if ENCRYPTION_ENABLED and encryption_ready():
        enc_state = "вкл" + (" (только шифр)" if ENCRYPTION_REQUIRED else "")
    elif ENCRYPTION_ENABLED:
        enc_state = "вкл, но нет ключа/секрета — письма идут открытым текстом"
    print(
        f"Email-tunnel запущен. Шифрование: {enc_state}. "
        f"Опрос каждые {POLL_INTERVAL_SECONDS} сек. Ctrl+C для остановки."
    )
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Ошибка в цикле обработки: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
