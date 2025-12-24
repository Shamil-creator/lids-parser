"""
Менеджер для управления пулом userbot'ов
"""
import asyncio
import os
import json
import time
from typing import Dict, Optional
from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, FloodWait
from pyrogram.types import Message
from pyrogram.enums import ChatType
from database.models import Database
from services.parser import ChannelParser
from services.messenger import Messenger
from services.private_group_coordinator import PrivateGroupCoordinator
import config

db = Database()

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


class UserbotManager:
    def __init__(self):
        self.clients: Dict[str, Client] = {}
        self.parsers: Dict[str, ChannelParser] = {}
        self.messengers: Dict[str, Messenger] = {}
        self.running = False
        self.tasks: Dict[str, asyncio.Task] = {}
        # Координатор приватных групп
        self.private_group_coordinator: Optional[PrivateGroupCoordinator] = None

    async def load_accounts(self):
        """Загрузить все активные аккаунты из БД"""
        accounts = db.get_all_accounts()
        _dbg("H2", "userbot_manager.py:load_accounts", "Loaded accounts from DB", {"total": len(accounts), "active": sum(1 for a in accounts if a.get("status") == "Active")})
        for account in accounts:
            if account['status'] == 'Active':
                await self.add_client(account['session_name'], account['phone'])

    async def add_client(self, session_name: str, phone: str = ""):
        """Добавить клиент Pyrogram"""
        try:
            session_path = os.path.join(config.SESSIONS_DIR, f"{session_name}.session")

            # Проверяем существует ли сессия
            if not os.path.exists(session_path):
                print(f"Session file not found: {session_path}")
                return False

            # Получаем API credentials из БД
            account = db.get_account(session_name)
            if not account:
                print(f"Account not found in DB: {session_name}")
                return False

            api_id = account.get('api_id') or os.getenv(f"API_ID_{session_name}", "")
            api_hash = account.get('api_hash') or os.getenv(f"API_HASH_{session_name}", "")

            # Используем API credentials если есть, иначе пробуем без них
            if api_id and api_hash:
                # Клиент с API credentials
                client = Client(
                    name=session_name,
                    workdir=config.SESSIONS_DIR,
                    api_id=int(api_id) if api_id.isdigit() else api_id,
                    api_hash=api_hash
                )
            else:
                # Подключение без API credentials (если они в сессии)
                print(f"[{session_name}] API credentials не найдены, используем сессию напрямую")
                client = Client(
                    name=session_name,
                    workdir=config.SESSIONS_DIR
                )

            # Проверяем авторизацию
            try:
                await client.start()
                me = await client.get_me()
                print(f"[{session_name}] Client started: @{me.username}")

                self.clients[session_name] = client
                # Получаем все категории для этого userbot'а
                userbot_categories = db.get_userbot_categories(session_name)
                # Передаем все категории в парсер, чтобы он объединял ключевые слова и стоп-слова
                category_id = userbot_categories[0] if userbot_categories else None
                
                parser = ChannelParser(client, category_id=category_id, category_ids=userbot_categories)
                self.parsers[session_name] = parser
                # Передаем parser в messenger для определения категории по ключевым словам
                self.messengers[session_name] = Messenger(client, session_name, category_id=category_id, parser=parser)

                # Запуск воркера
                if self.running:
                    self.tasks[session_name] = asyncio.create_task(self.worker_loop(session_name))

                return True
            except (AuthKeyUnregistered, UserDeactivated) as e:
                print(f"[{session_name}] Account banned/deactivated: {e}")
                db.update_account_status(session_name, "Banned")
                return False
            except Exception as e:
                print(f"[{session_name}] Error starting client: {e}")
                return False

        except Exception as e:
            print(f"Error adding client {session_name}: {e}")
            return False

    async def remove_client(self, session_name: str):
        """Удалить клиент"""
        if session_name in self.tasks:
            self.tasks[session_name].cancel()
            del self.tasks[session_name]

        if session_name in self.messengers:
            del self.messengers[session_name]

        if session_name in self.parsers:
            del self.parsers[session_name]

        if session_name in self.clients:
            try:
                await self.clients[session_name].stop()
            except:
                pass
            del self.clients[session_name]

    async def worker_loop(self, session_name: str):
        """Основной цикл работы воркера"""
        client = self.clients.get(session_name)
        parser = self.parsers.get(session_name)
        messenger = self.messengers.get(session_name)

        if not all([client, parser, messenger]):
            return

        print(f"[{session_name}] Worker started")

        # Обработчик входящих сообщений
        asyncio.create_task(self.message_handler(session_name))

        while self.running:
            try:
                # Обновление фильтров
                parser.refresh_filters()
                messenger.refresh_template()

                # Получаем все категории для этого userbot'а
                userbot_categories = db.get_userbot_categories(session_name)
                
                if userbot_categories:
                    # Парсим каналы всех категорий userbot'а
                    all_channels = []
                    for cat_id in userbot_categories:
                        cat_channels = db.get_category_channels(cat_id)
                        all_channels.extend(cat_channels)
                    # Убираем дубликаты по ID
                    seen_ids = set()
                    channels = []
                    for ch in all_channels:
                        if ch['id'] not in seen_ids:
                            seen_ids.add(ch['id'])
                            channels.append(ch)
                else:
                    # Обратная совместимость: парсим все каналы
                    channels = db.get_all_channels()

                for channel in channels:
                    try:
                        # Определяем категорию канала для правильной пересылки в канал менеджеров
                        channel_categories = db.get_channel_categories(channel['id'])
                        channel_category_id = channel_categories[0] if channel_categories else None
                        
                        # Временно обновляем category_id в messenger'е для этого канала
                        original_category_id = messenger.category_id
                        if channel_category_id:
                            messenger.category_id = channel_category_id
                        
                        # Парсинг канала
                        messages = await parser.parse_channel(channel['link'], limit=50)

                        for message in messages:
                            # Автор сообщения
                            author = parser.get_message_author(message)
                            if not author:
                                continue

                            user_id = author['id']

                            # Проверка на дубликаты
                            if db.is_user_processed(user_id):
                                continue

                            # Отправка первого сообщения
                            message_text = message.text or message.caption or ""
                            success = await messenger.send_first_message(
                                user_id,
                                author.get('username', ''),
                                channel['link'],
                                message_text[:500]
                            )

                            # Задержка между сообщениями
                            await asyncio.sleep(config.MIN_DELAY_BETWEEN_MESSAGES)
                        
                        # Восстанавливаем оригинальный category_id
                        messenger.category_id = original_category_id

                    except Exception as e:
                        print(f"[{session_name}] Error processing channel {channel['link']}: {e}")
                        continue

                # Пауза перед следующим циклом
                await asyncio.sleep(60)

            except Exception as e:
                print(f"[{session_name}] Error in worker loop: {e}")
                await asyncio.sleep(10)

    async def message_handler(self, session_name: str):
        """Обработчик входящих сообщений"""
        client = self.clients.get(session_name)
        messenger = self.messengers.get(session_name)
        parser = self.parsers.get(session_name)

        if not all([client, messenger, parser]):
            return

        @client.on_message()
        async def handle_message(client: Client, message: Message):
            try:
                # Логирование всех входящих сообщений
                chat_type = getattr(message.chat, 'type', 'unknown')
                chat_id = getattr(message.chat, 'id', None)
                message_text = (message.text or message.caption or "")[:100]
                from_user_id = getattr(message.from_user, 'id', None) if message.from_user else None
                from_username = getattr(message.from_user, 'username', None) if message.from_user else None
                
                print(f"[{session_name}] 📨 Входящее сообщение:")
                print(f"  • Тип чата: {chat_type}")
                print(f"  • chat_id: {chat_id}")
                print(f"  • message_id: {message.id}")
                print(f"  • От: user_id={from_user_id}, username=@{from_username}")
                print(f"  • Текст: {message_text}")
                
                # Личные сообщения
                if message.chat.type == ChatType.PRIVATE and message.from_user:
                    print(f"[{session_name}] → Личное сообщение, обрабатываем через messenger")
                    # Информация о пользователе из БД
                    user_info = db.get_user_info(message.from_user.id)
                    source_channel = user_info['channel_source'] if user_info else ""
                    original_post_text = user_info['original_post_text'] if user_info else ""
                    print(f"[{session_name}] Информация о пользователе: source_channel={source_channel}, has_original_post={bool(original_post_text)}")

                    try:
                        await messenger.process_incoming_message(
                            message,
                            source_channel,
                            original_post_text
                        )
                        print(f"[{session_name}] ✅ Обработка личного сообщения завершена")
                    except Exception as e:
                        print(f"[{session_name}] ❌ Ошибка при обработке личного сообщения: {e}")
                        import traceback
                        traceback.print_exc()
                    return
                
                # Сообщения из групп (только ACTIVE)
                if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                    chat_id = int(message.chat.id)
                    print(f"[{session_name}] → Сообщение из группы/супергруппы, chat_id={chat_id}")
                    print(f"[{session_name}] Ищем группу в БД по chat_id={chat_id}...")
                    group = db.get_private_group_by_chat_id(chat_id)
                    print(f"[{session_name}] Результат поиска группы: {group is not None}")
                    if group:
                        print(f"[{session_name}] Группа найдена: ID={group.get('id')}, state={group.get('state')}, is_active={group.get('is_active')}")

                    _dbg(
                        "H5",
                        "userbot_manager.py:handle_message:group_entry",
                        "Incoming group/supergroup message",
                        {
                            "session": session_name,
                            "chat_id": chat_id,
                            "has_from_user": bool(getattr(message, "from_user", None)),
                            "has_sender_chat": bool(getattr(message, "sender_chat", None)),
                            "text_preview": (message.text or message.caption or "")[:30],
                            "group_found": bool(group),
                            "group_state": (group.get("state") if group else None),
                            "group_is_active": (bool(group.get("is_active")) if group else None),
                        },
                    )
                    
                    # Проверка состояния группы
                    if not group:
                        print(f"[{session_name}] ❌ Группа не найдена в БД для chat_id={chat_id}")
                        return
                    
                    group_state = group.get('state', 'UNKNOWN')
                    group_is_active = bool(group.get('is_active'))
                    print(f"[{session_name}] Группа найдена: ID={group.get('id')}, state={group_state}, is_active={group_is_active}")
                    
                    if group_state != 'ACTIVE' or not group_is_active:
                        print(f"[{session_name}] ❌ Группа не ACTIVE (state={group_state}, is_active={group_is_active})")
                        return
                    
                    # Проверка на новое сообщение
                    last_message_id = group.get('last_message_id', 0)
                    if message.id <= last_message_id:
                        print(f"[{session_name}] ⏭️ Сообщение уже обработано (message_id={message.id} <= last_message_id={last_message_id})")
                        return
                    
                    print(f"[{session_name}] ✅ Группа ACTIVE, сообщение новое, проверяем фильтры...")
                    
                    # Обновление last_message_id
                    db.update_private_group(group['id'], {'last_message_id': message.id})
                    
                    # Обновление фильтров
                    parser.refresh_filters()
                    keywords = parser.keywords
                    stopwords = parser.stopwords
                    print(f"[{session_name}] Ключевые слова ({len(keywords)}): {keywords[:5]}")
                    print(f"[{session_name}] Стоп-слова ({len(stopwords)}): {stopwords[:5]}")
                    
                    # Проверка фильтров
                    should_process = parser.should_process_message(message)
                    print(f"[{session_name}] Результат фильтрации: should_process={should_process}")
                    _dbg(
                        "H4",
                        "userbot_manager.py:handle_message:filters",
                        "Filter decision",
                        {
                            "session": session_name,
                            "chat_id": chat_id,
                            "message_id": int(message.id or 0),
                            "should_process": bool(should_process),
                        },
                    )

                    if should_process:
                        print(f"[{session_name}] ✅ Сообщение прошло фильтры, получаем автора...")
                        # Получение автора
                        author = parser.get_message_author(message)
                        if not author:
                            print(f"[{session_name}] ❌ Не удалось получить автора (возможно анонимный админ или пост от канала)")
                            _dbg(
                                "H3",
                                "userbot_manager.py:handle_message:no_author",
                                "No from_user author (likely anonymous/channel post)",
                                {"session": session_name, "chat_id": chat_id, "message_id": int(message.id or 0)},
                            )
                            return
                        
                        user_id = author['id']
                        username = author.get('username', '')
                        print(f"[{session_name}] Автор: user_id={user_id}, username=@{username}")
                        
                        # Проверка на дубликаты (для групп разрешаем повтор через N минут)
                        already = db.is_user_processed(user_id)
                        can_repeat = False
                        if already:
                            can_repeat = db.can_repeat_message_to_user(user_id, config.REPEAT_MESSAGE_MINUTES)
                            print(f"[{session_name}] Пользователь уже обработан, можно повторить: {can_repeat}")
                        
                        print(f"[{session_name}] Пользователь уже обработан: {already}, можно повторить: {can_repeat}")
                        _dbg(
                            "H5",
                            "userbot_manager.py:handle_message:processed_check",
                            "Processed check",
                            {"session": session_name, "chat_id": chat_id, "user_id": int(user_id), "already": bool(already), "can_repeat": bool(can_repeat)},
                        )

                        if already and not can_repeat:
                            print(f"[{session_name}] ⏭️ Пропускаем, пользователю уже писали недавно (менее {config.REPEAT_MESSAGE_MINUTES} минут назад)")
                            return
                        
                        # Отправка первого сообщения (или повторного, если прошло достаточно времени)
                        message_text = message.text or message.caption or ""
                        group_title = group.get('title', 'Private Group')
                        force_repeat = already and can_repeat  # Повторная отправка если прошло достаточно времени
                        print(f"[{session_name}] 📤 Отправляем {'повторное' if force_repeat else 'первое'} сообщение пользователю {user_id}...")
                        
                        ok = await messenger.send_first_message(
                            user_id,
                            username,
                            f"Private Group: {group_title}",
                            message_text[:500],
                            force_repeat=force_repeat
                        )
                        print(f"[{session_name}] {'✅ Сообщение отправлено успешно' if ok else '❌ Ошибка отправки сообщения'}")
                        _dbg(
                            "H6",
                            "userbot_manager.py:handle_message:send_first_message",
                            "send_first_message result",
                            {"session": session_name, "chat_id": chat_id, "user_id": int(user_id), "ok": bool(ok)},
                        )
                    else:
                        print(f"[{session_name}] ❌ Сообщение не прошло фильтры (нет ключевых слов или есть стоп-слова)")
                
            except Exception as e:
                print(f"[{session_name}] Error handling message: {e}")

    async def start(self):
        """Запустить менеджер"""
        if self.running:
            return

        self.running = True
        _dbg("H2", "userbot_manager.py:start", "Starting UserbotManager", {})
        await self.load_accounts()
        _dbg("H2", "userbot_manager.py:start", "Loaded clients", {"clients": len(self.clients)})

        # Запуск воркеров
        for session_name in list(self.clients.keys()):
            self.tasks[session_name] = asyncio.create_task(self.worker_loop(session_name))

        # Запуск координатора групп
        self.private_group_coordinator = PrivateGroupCoordinator(self.clients)
        await self.private_group_coordinator.start()
        _dbg("H3", "userbot_manager.py:start", "PrivateGroupCoordinator started", {"clients": len(self.clients)})

        print("UserbotManager started")

    async def stop(self):
        """Остановить менеджер"""
        self.running = False

        # Остановка координатора
        if self.private_group_coordinator:
            await self.private_group_coordinator.stop()

        # Отмена задач
        for task in self.tasks.values():
            task.cancel()

        # Остановка клиентов
        for client in self.clients.values():
            try:
                await client.stop()
            except:
                pass

        self.clients.clear()
        self.parsers.clear()
        self.messengers.clear()
        self.tasks.clear()

        print("UserbotManager stopped")

    async def reload_account(self, session_name: str):
        """Перезагрузить аккаунт"""
        await self.remove_client(session_name)
        account = next((a for a in db.get_all_accounts() if a['session_name'] == session_name), None)
        if account:
            await self.add_client(session_name, account['phone'])
    
    async def update_category_for_session(self, session_name: str):
        """Обновить category_ids для парсера и messenger'а сессии"""
        if session_name not in self.parsers or session_name not in self.messengers:
            return
        
        # Получаем все категории для этого userbot'а
        userbot_categories = db.get_userbot_categories(session_name)
        # Используем первую категорию для messenger'а (для определения канала менеджеров)
        category_id = userbot_categories[0] if userbot_categories else None
        
        # Обновляем category_ids в парсере
        if hasattr(self.parsers[session_name], 'category_ids'):
            self.parsers[session_name].category_ids = userbot_categories
        self.parsers[session_name].category_id = category_id  # Для обратной совместимости
        self.parsers[session_name].refresh_filters()
        
        # Обновляем category_id в messenger'е
        self.messengers[session_name].category_id = category_id
