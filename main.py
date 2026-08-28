import logging
import threading

from config import ConfigError, load_config
from gemini_client import init_client
from worker import run_account_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("email_tunnel.main")


def main() -> None:
    try:
        app_config = load_config()
    except ConfigError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(1) from exc

    init_client(app_config.gemini_api_key)

    if not app_config.accounts:
        logger.error("В config.json не задано ни одного аккаунта.")
        raise SystemExit(1)

    stop_event = threading.Event()
    threads = []

    for account in app_config.accounts:
        thread = threading.Thread(
            target=run_account_loop,
            args=(account, stop_event),
            name=f"account-{account.name}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    logger.info("Email-tunnel запущен: %d аккаунт(ов). Ctrl+C для остановки.", len(threads))

    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        logger.info("Получен сигнал остановки, завершаю потоки...")
        stop_event.set()
        for t in threads:
            t.join(timeout=10.0)
        logger.info("Остановлено.")


if __name__ == "__main__":
    main()