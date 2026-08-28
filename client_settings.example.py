# Скопируйте в client_settings.py и заполните.
# Это настройки ОТПРАВИТЕЛЯ (друга), не сервера туннеля.

# Куда слать запросы
TUNNEL_EMAIL = ""          # адрес ящика туннеля

# Ваша почта, с которой уходит письмо.
# Этот адрес должен быть в ALLOWED_SENDERS на сервере.
CLIENT_EMAIL = ""
CLIENT_PASS = ""           # пароль приложения

SMTP_SERVER = ""
SMTP_PORT = 465            # 465 = SSL, 587 = STARTTLS

# Шифрование: укажите секрет и/или путь к публичному ключу сервера
TUNNEL_SECRET = ""
SERVER_PUBLIC_KEY = "keys/server_public.pem"

DEFAULT_SUBJECT = "Email Tunnel"
