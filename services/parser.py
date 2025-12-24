"""
ChannelParser — парсер сообщений из каналов/групп с фильтрацией по ключевым словам.
FloodWait/anti-flood логики здесь нет (ошибки ловим общим except).
"""
import re
from typing import List, Optional, Union
from urllib.parse import urlparse
from pyrogram import Client
from pyrogram.types import Message
from database.models import Database

db = Database()

_WORD_RE_CACHE: dict[str, re.Pattern] = {}

class ChannelParser:
    def __init__(self, client: Client, category_id: Optional[int] = None, category_ids: Optional[List[int]] = None):
        self.client = client
        self.category_id = category_id  # Для обратной совместимости
        self.category_ids = category_ids or ([category_id] if category_id else [])  # Список всех категорий
        self.keywords: List[str] = []
        self.stopwords: List[str] = []
        self.refresh_filters()

    def refresh_filters(self):
        """Обновить ключевые слова и стоп-слова из БД (из категорий или глобально)"""
        if self.category_ids:
            # Объединяем ключевые слова и стоп-слова всех категорий
            all_keywords = set()
            all_stopwords = set()
            for cat_id in self.category_ids:
                keywords = db.get_category_keywords(cat_id) or []
                stopwords = db.get_category_stopwords(cat_id) or []
                all_keywords.update(str(w).lower().strip() for w in keywords if str(w).strip())
                all_stopwords.update(str(w).lower().strip() for w in stopwords if str(w).strip())
            self.keywords = list(all_keywords)
            self.stopwords = list(all_stopwords)
        elif self.category_id:
            # Используем ключевые слова и стоп-слова одной категории (старый способ)
            self.keywords = [str(w).lower().strip() for w in (db.get_category_keywords(self.category_id) or []) if str(w).strip()]
            self.stopwords = [str(w).lower().strip() for w in (db.get_category_stopwords(self.category_id) or []) if str(w).strip()]
        else:
            # Используем глобальные ключевые слова и стоп-слова (для обратной совместимости)
            self.keywords = [str(w).lower().strip() for w in (db.get_all_keywords() or []) if str(w).strip()]
            self.stopwords = [str(w).lower().strip() for w in (db.get_all_stopwords() or []) if str(w).strip()]

    @staticmethod
    def _compile_word_regex(word: str) -> re.Pattern:
        """
        Компилируем regex для поиска слова как отдельного токена.
        Важно: это "word boundary" поиск, а не подстрока.
        """
        # Кэш для regex
        if word not in _WORD_RE_CACHE:
            # Границы по \w для поддержки "#tag", "c++"
            _WORD_RE_CACHE[word] = re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)
        return _WORD_RE_CACHE[word]

    def _contains_any_word(self, text: str, words: List[str]) -> bool:
        for word in words:
            if self._compile_word_regex(word).search(text):
                return True
        return False

    def should_process_message(self, message: Message) -> bool:
        """Определить нужно ли обрабатывать сообщение"""
        if not message:
            print("[ChannelParser] ❌ should_process_message: message is None")
            return False

        text = message.text or message.caption
        if not text:
            print("[ChannelParser] ❌ should_process_message: нет текста (ни text, ни caption)")
            return False

        text_lower = text.lower()
        print(f"[ChannelParser] Проверяем текст: '{text_lower[:100]}'")

        # Ключевые слова
        if self.keywords:
            contains_keyword = self._contains_any_word(text_lower, self.keywords)
            print(f"[ChannelParser] Ключевые слова ({len(self.keywords)}): найдено={contains_keyword}")
            if not contains_keyword:
                print(f"[ChannelParser] ❌ Нет ключевых слов в тексте")
                return False
        else:
            print("[ChannelParser] ⚠️ Ключевые слова не настроены (пропускаем все сообщения)")

        # Стоп-слова
        if self.stopwords:
            contains_stopword = self._contains_any_word(text_lower, self.stopwords)
            print(f"[ChannelParser] Стоп-слова ({len(self.stopwords)}): найдено={contains_stopword}")
            if contains_stopword:
                print(f"[ChannelParser] ❌ Найдено стоп-слово в тексте")
                return False

        print("[ChannelParser] ✅ Сообщение прошло все фильтры")
        return True

    def detect_category_by_keywords(self, text: str) -> Optional[int]:
        """Определить категорию сообщения по ключевым словам"""
        if not self.category_ids or not text:
            return None
        
        text_lower = text.lower()
        category_keyword_matches = {}
        
        # Проверяем ключевые слова каждой категории
        for cat_id in self.category_ids:
            keywords = db.get_category_keywords(cat_id) or []
            stopwords = db.get_category_stopwords(cat_id) or []
            
            # Проверяем стоп-слова - если есть стоп-слово, категория не подходит
            has_stopword = False
            for stopword in stopwords:
                if self._contains_any_word(text_lower, [str(stopword).lower().strip()]):
                    has_stopword = True
                    break
            
            if has_stopword:
                continue
            
            # Считаем совпадения ключевых слов
            matches = 0
            for keyword in keywords:
                if self._contains_any_word(text_lower, [str(keyword).lower().strip()]):
                    matches += 1
            
            if matches > 0:
                category_keyword_matches[cat_id] = matches
        
        # Возвращаем категорию с наибольшим количеством совпадений
        if category_keyword_matches:
            return max(category_keyword_matches.items(), key=lambda x: x[1])[0]
        
        return None

    def normalize_chat_target(self, chat: Union[int, str]) -> Optional[Union[int, str]]:
        """
        Приводит вход к виду:
        - chat_id (int)
        - username (str, без @)
        """
        if isinstance(chat, int):
            return chat

        s = str(chat).strip()
        if not s:
            return None

        if s.startswith("@"):
            return s[1:]

        # Нормализация коротких ссылок
        if s.startswith("t.me/") or s.startswith("telegram.me/"):
            s = "https://" + s

        # Обработка t.me ссылок
        if s.startswith("http://") or s.startswith("https://"):
            try:
                parsed = urlparse(s)
            except Exception:
                return None

            host = (parsed.netloc or "").lower()
            if host.endswith("t.me") or host.endswith("telegram.me"):
                path = (parsed.path or "").strip("/")
                if not path:
                    return None

                parts = path.split("/")

                # Приватные инвайты не поддерживаются
                if parts[0].startswith("+") or parts[0] == "joinchat":
                    return None

                # Служебная ссылка /c/ не поддерживается
                if parts[0] == "c":
                    return None

                # Публичная ссылка /s/<username>
                if parts[0] == "s":
                    if len(parts) < 2:
                        return None
                    return parts[1]

                # Обычная ссылка /<username>
                return parts[0]

        return s

    def _normalize_chat_target_with_reason(self, chat: Union[int, str]) -> tuple[Optional[Union[int, str]], Optional[str]]:
        """То же, что normalize_chat_target(), но возвращает причину ошибки"""
        if isinstance(chat, int):
            return chat, None

        s = str(chat).strip()
        if not s:
            return None, "empty input"

        if s.startswith("@"):
            if len(s) == 1:
                return None, "empty @username"
            return s[1:], None

        if s.startswith("t.me/") or s.startswith("telegram.me/"):
            s = "https://" + s

        if s.startswith("http://") or s.startswith("https://"):
            try:
                parsed = urlparse(s)
            except Exception:
                return None, "invalid url"

            host = (parsed.netloc or "").lower()
            if not (host.endswith("t.me") or host.endswith("telegram.me")):
                return None, "unsupported host"

            path = (parsed.path or "").strip("/")
            if not path:
                return None, "empty telegram path"

            parts = path.split("/")

            if parts[0].startswith("+") or parts[0] == "joinchat":
                return None, "приватный инвайт (не поддерживается)"

            if parts[0] == "c":
                return None, "служебная ссылка /c/ (не поддерживается)"

            if parts[0] == "s":
                if len(parts) < 2 or not parts[1]:
                    return None, "ссылка /s/ без username"
                return parts[1], None

            return parts[0], None

        # Обычный идентификатор: username / phone / me / self
        return s, None

    async def parse_chat(self, chat: Union[int, str], limit: int = 100) -> List[Message]:
        """
        Парсит сообщения из канала / группы
        chat: chat_id | @username | https://t.me/...
        """
        target, reason = self._normalize_chat_target_with_reason(chat)
        messages: List[Message] = []

        if target is None:
            print(f"[ChannelParser] Skipping chat={chat!r}: {reason or 'unrecognized format'}")
            return messages

        try:
            async for message in self.client.get_chat_history(target, limit=limit):
                if self.should_process_message(message):
                    messages.append(message)
        except Exception as e:
            print(f"[ChannelParser] Error parsing chat={chat!r} (target={target!r}): {e}")

        return messages

    # Совместимость со старым API
    async def parse_channel(self, channel_link: str, limit: int = 100) -> List[Message]:
        return await self.parse_chat(channel_link, limit=limit)

    def get_message_author(self, message: Message) -> Optional[dict]:
        """Получить автора сообщения"""
        if not message or not message.from_user:
            return None

        user = message.from_user
        return {
            "id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
        }

    # Совместимость со старым API
    async def get_message_author_async(self, message: Message) -> Optional[dict]:
        return self.get_message_author(message)


