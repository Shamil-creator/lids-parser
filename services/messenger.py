"""
Сервис для отправки сообщений и обработки ответов
"""
import asyncio
import re
from typing import Optional
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import UserPrivacyRestricted, FloodWait
try:
    # В Pyrogram 2.x это PeerFlood
    from pyrogram.errors import PeerFlood
except Exception:
    PeerFlood = Exception
from database.models import Database
import config

db = Database()


class Messenger:
    def __init__(self, client: Client, session_name: str, category_id: Optional[int] = None, parser=None):
        self.client = client
        self.session_name = session_name
        self.category_id = category_id
        self.parser = parser  # Парсер для определения категории по ключевым словам
        self.template = self._get_template()
        self.follow_up_timers = {}  # Таймеры дожимающих сообщений

    def _get_template(self) -> str:
        """Получить шаблон сообщения (приоритет: категория > глобальный)"""
        if self.category_id:
            category_text = db.get_category_message_text(self.category_id)
            if category_text:
                return category_text
        return db.get_active_template()

    def refresh_template(self):
        """Обновить шаблон из БД"""
        self.template = self._get_template()

    async def send_first_message(self, user_id: int, username: str = "", channel_source: str = "", original_post_text: str = "", force_repeat: bool = False) -> bool:
        """Отправить первое сообщение пользователю"""
        try:
            # Проверка на дубликаты (если не принудительный повтор)
            # Проверяем, не запущен ли уже таймер дожима (значит сообщение уже отправлялось)
            if not force_repeat and user_id in self.follow_up_timers:
                return False
            
            # Проверяем, не ответил ли пользователь уже (помечен как обработанный)
            if not force_repeat and db.is_user_processed(user_id):
                return False

            # Отправка сообщения
            await self.client.send_message(user_id, self.template)

            # НЕ помечаем пользователя как обработанного при отправке первого сообщения
            # Пользователь будет помечен как обработанный только при ответе
            # Это нужно для того, чтобы через 4 часа отправить повторное сообщение, если пользователь не ответил

            # Запуск таймера дожима
            await self.schedule_follow_up(user_id)

            return True
        except PeerFlood:
            print(f"[{self.session_name}] PeerFlood for user {user_id}")
            db.update_account_status(self.session_name, "Flood")
            return False
        except UserPrivacyRestricted:
            print(f"[{self.session_name}] UserPrivacyRestricted for user {user_id}")
            return False
        except FloodWait as e:
            print(f"[{self.session_name}] FloodWait {e.value} seconds")
            await asyncio.sleep(e.value)
            return await self.send_first_message(user_id)
        except Exception as e:
            print(f"[{self.session_name}] Error sending message to {user_id}: {e}")
            return False

    async def schedule_follow_up(self, user_id: int):
        """Запланировать дожимающее сообщение"""
        async def follow_up_task():
            # Используем задержку в минутах из конфига
            await asyncio.sleep(config.FOLLOW_UP_DELAY_MINUTES * 60)
            # Проверка ответа пользователя
            if not db.is_user_processed(user_id):
                try:
                    await self.client.send_message(user_id, config.FOLLOW_UP_MESSAGE)
                except Exception as e:
                    print(f"[{self.session_name}] Error sending follow-up to {user_id}: {e}")

        task = asyncio.create_task(follow_up_task())
        self.follow_up_timers[user_id] = task

    def extract_phone(self, text: str) -> Optional[str]:
        """Извлечь номер телефона из текста"""
        # Паттерны для телефонов
        patterns = [
            r'\+?\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,4}[-.\s]?\d{1,9}',
            r'\+7\s?\(?\d{3}\)?\s?\d{3}[-.\s]?\d{2}[-.\s]?\d{2}',
            r'\d{10,15}',
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                phone = re.sub(r'[^\d+]', '', match.group())
                if len(phone) >= 10:
                    return phone

        # Поиск длинных последовательностей цифр
        digits = re.findall(r'\d+', text)
        if digits and any(len(d) >= 10 for d in digits):
            return ''.join(digits)[:15]

        return None

    def has_phone_or_digits(self, text: str) -> bool:
        """Проверить наличие телефона или цифр"""
        phone = self.extract_phone(text)
        if phone:
            return True

        # Проверка количества цифр
        digits_count = len(re.findall(r'\d', text))
        return digits_count >= 7

    async def process_incoming_message(self, message: Message, source_channel: str = "", original_post_text: str = ""):
        """Обработать входящее сообщение от пользователя"""
        print(f"[{self.session_name}] 📥 process_incoming_message: начало обработки")
        
        if not message.text:
            print(f"[{self.session_name}] ⏭️ Нет текста в сообщении, пропускаем")
            return

        user_id = message.from_user.id
        username = message.from_user.username or ""
        text = message.text
        print(f"[{self.session_name}] Текст сообщения: {text[:100]}")

        # Пересылка всех сообщений в канал менеджеров
        print(f"[{self.session_name}] 📤 Пересылаем сообщение в канал менеджеров...")
        # Парсер передается для определения категории по ключевым словам
        await self.forward_message_to_managers(message, source_channel, original_post_text, parser=self.parser)
        print(f"[{self.session_name}] ✅ Сообщение переслано в канал менеджеров")

        # Помечаем пользователя как обработанного (он ответил)
        db.mark_user_processed(user_id, username, source_channel, original_post_text)
        print(f"[{self.session_name}] ✅ Пользователь помечен как обработанный (ответил)")
        
        # Отмена дожимающего сообщения (пользователь ответил)
        if user_id in self.follow_up_timers:
            self.follow_up_timers[user_id].cancel()
            del self.follow_up_timers[user_id]
            print(f"[{self.session_name}] ⏹️ Дожимающее сообщение отменено (пользователь ответил)")
        
        # Проверка на телефон
        has_phone = self.has_phone_or_digits(text)
        print(f"[{self.session_name}] Проверка на телефон: {has_phone}")
        
        if has_phone:
            phone = self.extract_phone(text) or "Не указан"
            print(f"[{self.session_name}] 📱 Найден телефон: {phone}")

            # Сохранение лида
            db.add_lead(user_id, username, phone, source_channel, original_post_text)
            print(f"[{self.session_name}] ✅ Лид сохранен в БД")
        
        print(f"[{self.session_name}] ✅ process_incoming_message: обработка завершена")

    async def forward_message_to_managers(self, message: Message, source_channel: str = "", original_post_text: str = "", parser=None):
        """Переслать сообщение от пользователя в канал менеджеров"""
        # Определяем категорию по каналу-источнику и ключевым словам
        channel_id = None
        source_category_id = None
        
        message_text = message.text or message.caption or ""
        
        # Пытаемся определить категорию по каналу-источнику
        if source_channel:
            source_categories = db.get_channel_categories_by_link(source_channel)
            if source_categories:
                # Если канал принадлежит нескольким категориям, определяем по ключевым словам
                if len(source_categories) > 1 and parser:
                    detected_category = parser.detect_category_by_keywords(message_text)
                    if detected_category and detected_category in source_categories:
                        source_category_id = detected_category
                    else:
                        # Используем первую категорию канала
                        source_category_id = source_categories[0]
                else:
                    # Используем первую категорию канала
                    source_category_id = source_categories[0]
                
                category = db.get_category(source_category_id)
                if category and category.get('managers_channel_id'):
                    channel_id = category['managers_channel_id']
        
        # Если не удалось определить по каналу-источнику, используем category_id из messenger'а
        if not channel_id and self.category_id:
            category = db.get_category(self.category_id)
            if category and category.get('managers_channel_id'):
                channel_id = category['managers_channel_id']
        
        # Если канал категории не настроен, используем глобальный
        if not channel_id:
            channel_id = db.get_managers_channel_id() or config.MANAGERS_CHANNEL_ID
        
        if not channel_id:
            print("MANAGERS_CHANNEL_ID not configured!")
            return

        try:
            user_id = message.from_user.id
            username = message.from_user.username or ""
            text = message.text or ""
            
            # Формирование сообщения для менеджеров
            report_text = f"""💬 Сообщение от пользователя

👤 Имя: @{username or 'Не указано'}
🆔 User ID: <code>{user_id}</code>
📢 Источник: {source_channel or 'Не указан'}
📝 Исходный пост:
{original_post_text[:300] if original_post_text else 'Не указан'}

💬 Сообщение:
{text}
"""

            await self.client.send_message(channel_id, report_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"[{self.session_name}] Error forwarding message to managers: {e}")

    async def forward_lead_to_managers(self, user_id: int, username: str, phone: str, 
                                      source_channel: str, original_post_text: str, user_message: str):
        """Переслать лид в канал менеджеров (старый метод)"""
        # Сначала пробуем получить из БД, если нет - из config
        channel_id = db.get_managers_channel_id() or config.MANAGERS_CHANNEL_ID
        
        if not channel_id:
            print("MANAGERS_CHANNEL_ID not configured!")
            return

        try:
            # Формирование сообщения для менеджеров
            report_text = f"""🎯 НОВЫЙ ЛИД

👤 Имя: {username or 'Не указано'}
📱 Телефон: {phone}
🆔 User ID: {user_id}
📢 Источник: {source_channel}
📝 Исходный пост:
{original_post_text[:500]}

💬 Сообщение пользователя:
{user_message}
"""

            await self.client.send_message(channel_id, report_text)
        except Exception as e:
            print(f"[{self.session_name}] Error forwarding lead: {e}")

