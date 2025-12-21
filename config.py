"""
Конфигурация системы лидгенерации
"""
import os
from dotenv import load_dotenv

# Определяем окружение (development, production, test)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Загружаем переменные окружения в порядке приоритета:
# 1. .env - базовые настройки
# 2. .env.local - локальные переопределения (для разработки)
# 3. .env.production - для продакшена (если ENVIRONMENT=production)
# 4. Системные переменные окружения (имеют наивысший приоритет)

# Сначала загружаем базовый .env
load_dotenv(".env")

# Затем переопределяем локальными настройками (если есть)
if os.path.exists(".env.local"):
    load_dotenv(".env.local", override=True)

# Для продакшена загружаем .env.production (если есть)
if ENVIRONMENT == "production" and os.path.exists(".env.production"):
    load_dotenv(".env.production", override=True)

# Telegram Bot Token для админ-бота
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Канал менеджеров (куда пересылаются лиды)
MANAGERS_CHANNEL_ID = int(os.getenv("MANAGERS_CHANNEL_ID", "0"))

# Пароль для доступа к админ-панели
ADMIN_PASSWORD = "admin251219750"

# Задержки (в секундах)
MIN_DELAY_BETWEEN_MESSAGES = 2
MAX_DELAY_BETWEEN_MESSAGES = 5
FOLLOW_UP_DELAY_HOURS = 4  # Задержка перед дожимающим сообщением
REPEAT_MESSAGE_MINUTES = int(os.getenv("REPEAT_MESSAGE_MINUTES", "10"))  # Через сколько минут можно писать снова (для групп)

# Приватные группы: лимиты и задержки
PRIVATE_GROUP_RECONCILE_INTERVAL = int(os.getenv("PRIVATE_GROUP_RECONCILE_INTERVAL", "30"))  # Интервал цикла проверки (сек)
PRIVATE_GROUP_JOIN_MIN_DELAY = int(os.getenv("PRIVATE_GROUP_JOIN_MIN_DELAY", "120"))  # Мин задержка перед join (сек)
PRIVATE_GROUP_JOIN_MAX_DELAY = int(os.getenv("PRIVATE_GROUP_JOIN_MAX_DELAY", "300"))  # Макс задержка перед join (сек)
PRIVATE_GROUP_CHECK_INTERVAL_MINUTES = int(os.getenv("PRIVATE_GROUP_CHECK_INTERVAL_MINUTES", "30"))  # Интервал проверки доступа (мин)
PRIVATE_GROUP_JOINING_TIMEOUT_MINUTES = int(os.getenv("PRIVATE_GROUP_JOINING_TIMEOUT_MINUTES", "1"))  # Таймаут для JOINING (мин)
PRIVATE_GROUP_MAX_CONCURRENT_JOINS = int(os.getenv("PRIVATE_GROUP_MAX_CONCURRENT_JOINS", "3"))  # Макс одновременных join
PRIVATE_GROUP_LOST_ACCESS_MAX_RETRIES = int(os.getenv("PRIVATE_GROUP_LOST_ACCESS_MAX_RETRIES", "5"))  # Повторы перед DISABLED
MAX_PRIVATE_GROUPS_PER_ACCOUNT = int(os.getenv("MAX_PRIVATE_GROUPS_PER_ACCOUNT", "10"))  # Макс групп на аккаунт

# Текст дожимающего сообщения
FOLLOW_UP_MESSAGE = "Привет! Ты еще не ответил на мое сообщение. Готов обсудить детали?"

# База данных
DATABASE_PATH = "bot_database.db"

# Папка для сессий
SESSIONS_DIR = "sessions"

# Создаем папку для сессий если её нет
os.makedirs(SESSIONS_DIR, exist_ok=True)


