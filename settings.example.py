# Скопируйте этот файл в settings.py и заполните своими данными.
# settings.py не должен попадать в git.
TG_BOT_TOKEN = "токен от @BotFather"

OWNER_ID = 123456789   # ваш Telegram ID (узнать можно, например, у @userinfobot)

MAIL_USER = ""          # почтовый адрес туннеля
MAIL_PASS = ""          # пароль приложения (App Password)

IMAP_SERVER = "imap.mail.ru"
IMAP_PORT = 993
SMTP_SERVER = "smtp.mail.ru"
SMTP_PORT = 465

GEMINI_API_KEY = ""     # https://aistudio.google.com/api-keys

# Адреса, с которых принимаются запросы
ALLOWED_SENDERS = [
    # "friend@example.com",
]

POLL_INTERVAL_SECONDS = 10

