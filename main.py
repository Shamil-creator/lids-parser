"""
Главный файл запуска системы лидгенерации
"""
import asyncio
import logging
import json
import time
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from handlers import admin_panel, category_handlers
from services.userbot_manager import UserbotManager
import config

# Логирование для отладки
_DEBUG_LOG_PATH = "/Users/shamilsadykov/Desktop/lids parser/.cursor/debug.log"

def _dbg(hypothesis_id: str, location: str, message: str, data: dict):
    try:
        payload = {
            "sessionId": "debug-session",
            "runId": "run1",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        os.makedirs(os.path.dirname(_DEBUG_LOG_PATH), exist_ok=True)
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    """Главная функция"""
    _dbg("H1", "main.py:main:entry", "Entered main()", {"bot_token_set": bool(config.BOT_TOKEN), "environment": config.ENVIRONMENT})
    # Проверяем токен бота
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен! Создайте .env файл с BOT_TOKEN=...")
        _dbg("H1", "main.py:main:token_check", "BOT_TOKEN missing; aborting", {})
        return

    # Инициализация бота и диспетчера
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрируем роутеры
    # category_handlers должен быть первым, чтобы его обработчики имели приоритет
    dp.include_router(category_handlers.router)
    dp.include_router(admin_panel.router)

    # Создаем менеджер userbot'ов
    userbot_manager = UserbotManager()
    admin_panel.set_userbot_manager(userbot_manager)
    category_handlers.set_userbot_manager(userbot_manager)

    # Запускаем userbot manager в фоне
    asyncio.create_task(userbot_manager.start())
    _dbg("H2", "main.py:main:userbot_start", "Scheduled userbot_manager.start()", {})

    logger.info("Система лидгенерации запущена!")

    try:
        # Запускаем polling
        _dbg("H2", "main.py:main:polling", "Starting aiogram polling", {"skip_updates": True})
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await bot.session.close()
        await userbot_manager.stop()
        _dbg("H2", "main.py:main:shutdown", "Shutdown complete", {})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка системы...")


