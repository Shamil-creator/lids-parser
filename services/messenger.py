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
    def __init__(self, client: Client, session_name: str):
        self.client = client
        self.session_name = session_name
        self.template = db.get_active_template()
        self.follow_up_timers = {}  # Таймеры дожимающих сообщений

    def refresh_template(self):
        """Обновить шаблон из БД"""
        self.template = db.get_active_template()

    async def send_first_message(self, user_id: int, username: str = "", channel_source: str = "", original_post_text: str = "", force_repeat: bool = False) -> bool:
        """Отправить первое сообщение пользователю"""
        try:
            # Проверка на дубликаты (если не принудительный повтор)
            if not force_repeat and db.is_user_processed(user_id):
                return False

            # Отправка сообщения
            await self.client.send_message(user_id, self.template)

            # Пометка как обработанного (обновляет timestamp при повторной отправке)
            db.mark_user_processed(user_id, username, channel_source, original_post_text)

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
            await asyncio.sleep(config.FOLLOW_UP_DELAY_HOURS * 3600)
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
        await self.forward_message_to_managers(message, source_channel, original_post_text)
        print(f"[{self.session_name}] ✅ Сообщение переслано в канал менеджеров")

        # Проверка на телефон
        has_phone = self.has_phone_or_digits(text)
        print(f"[{self.session_name}] Проверка на телефон: {has_phone}")
        
        if has_phone:
            phone = self.extract_phone(text) or "Не указан"
            print(f"[{self.session_name}] 📱 Найден телефон: {phone}")

            # Сохранение лида
            db.add_lead(user_id, username, phone, source_channel, original_post_text)
            print(f"[{self.session_name}] ✅ Лид сохранен в БД")

            # Отмена дожимающего сообщения
            if user_id in self.follow_up_timers:
                self.follow_up_timers[user_id].cancel()
                del self.follow_up_timers[user_id]
                print(f"[{self.session_name}] ⏹️ Дожимающее сообщение отменено")
        
        print(f"[{self.session_name}] ✅ process_incoming_message: обработка завершена")

    async def forward_message_to_managers(self, message: Message, source_channel: str = "", original_post_text: str = ""):
        """Переслать сообщение от пользователя в канал менеджеров"""
        # Сначала пробуем получить из БД, если нет - из config
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

