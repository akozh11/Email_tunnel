# Скопируйте этот файл в settings.py и заполните своими данными.
# settings.py не должен попадать в git.

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

# --- Шифрование полезной нагрузки ---
# ENCRYPTION_ENABLED: шифровать ответы и пытаться расшифровать входящие.
# ENCRYPTION_REQUIRED: игнорировать незашифрованные письма.
ENCRYPTION_ENABLED = True
ENCRYPTION_REQUIRED = False

# Общий секрет для режима AES (одна фраза у сервера и у друзей).
# Придумайте длинную случайную строку, не используйте пароль от почты.
TUNNEL_SECRET = ""

# Каталог ключей RSA (создаётся скриптом generate_keys.py)
KEYS_DIR = "keys"
RSA_PRIVATE_KEY_PATH = "keys/server_private.pem"
RSA_PUBLIC_KEY_PATH = "keys/server_public.pem"
