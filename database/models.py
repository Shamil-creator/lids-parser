"""
Модели базы данных SQLite
"""
import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple
import os

DATABASE_PATH = "bot_database.db"


class Database:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self.init_database()

    def get_connection(self):
        """Получить соединение с БД"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_database(self):
        """Инициализация таблиц БД"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Таблица админов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица аккаунтов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT UNIQUE NOT NULL,
                phone TEXT NOT NULL,
                api_id TEXT,
                api_hash TEXT,
                status TEXT DEFAULT 'Active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица каналов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT UNIQUE NOT NULL,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица приватных групп
        # Переходы состояний: NEW → ASSIGNED → JOIN_QUEUED → JOINING → JOINED → ACTIVE → LOST_ACCESS → DISABLED
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS private_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                invite_link TEXT NOT NULL,
                chat_id INTEGER UNIQUE,
                title TEXT,
                assigned_session_name TEXT,
                state TEXT DEFAULT 'NEW',
                is_active INTEGER DEFAULT 1,
                last_message_id INTEGER DEFAULT 0,
                
                -- Отслеживание повторов
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 5,
                next_retry_at TIMESTAMP,
                last_join_attempt_at TIMESTAMP,
                
                -- Отслеживание ошибок
                consecutive_errors INTEGER DEFAULT 0,
                max_consecutive_errors INTEGER DEFAULT 3,
                last_error TEXT,
                last_checked_at TIMESTAMP,
                
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (assigned_session_name) REFERENCES accounts(session_name) ON DELETE SET NULL,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
                UNIQUE(category_id, invite_link)
            )
        """)
        
        # Миграция: добавляем category_id если его нет
        try:
            cursor.execute("ALTER TABLE private_groups ADD COLUMN category_id INTEGER")
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_category_invite 
                ON private_groups(category_id, invite_link)
            """)
        except sqlite3.OperationalError:
            # Колонка уже существует или индекс уже создан
            pass

        # Таблица ключевых слов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT UNIQUE NOT NULL
            )
        """)

        # Таблица стоп-слов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stopwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT UNIQUE NOT NULL
            )
        """)

        # Таблица статистики лидов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                channel_id INTEGER,
                count INTEGER DEFAULT 0,
                FOREIGN KEY (channel_id) REFERENCES channels(id)
            )
        """)

        # Таблица обработанных пользователей (чтобы не писать дважды)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                channel_source TEXT,
                original_post_text TEXT
            )
        """)

        # Таблица шаблонов сообщений
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_text TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица глобальных настроек API (для упрощенного добавления аккаунтов)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_api_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_id TEXT,
                api_hash TEXT,
                is_active INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица настроек канала менеджеров
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS managers_channel_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER UNIQUE NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица категорий
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                managers_channel_id INTEGER,
                message_text TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Миграция: добавляем message_text если его нет
        try:
            cursor.execute("ALTER TABLE categories ADD COLUMN message_text TEXT")
        except sqlite3.OperationalError:
            # Колонка уже существует
            pass
        
        # Миграция: добавляем follow_up_message если его нет
        try:
            cursor.execute("ALTER TABLE categories ADD COLUMN follow_up_message TEXT")
        except sqlite3.OperationalError:
            # Колонка уже существует
            pass
        
        # Миграция: удаляем assigned_session_name если он есть (старая структура)
        try:
            cursor.execute("ALTER TABLE categories DROP COLUMN assigned_session_name")
        except sqlite3.OperationalError:
            # Колонка уже удалена или не существует
            pass

        # Таблица связи категорий и каналов (многие-ко-многим)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS category_channels (
                category_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (category_id, channel_id),
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
                FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )
        """)

        # Таблица связи категорий и ключевых слов (многие-ко-многим)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS category_keywords (
                category_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                PRIMARY KEY (category_id, keyword_id),
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
            )
        """)

        # Таблица связи категорий и стоп-слов (многие-ко-многим)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS category_stopwords (
                category_id INTEGER NOT NULL,
                stopword_id INTEGER NOT NULL,
                PRIMARY KEY (category_id, stopword_id),
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
                FOREIGN KEY (stopword_id) REFERENCES stopwords(id) ON DELETE CASCADE
            )
        """)

        # Таблица связи категорий и userbot'ов (многие-ко-многим)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS category_userbots (
                category_id INTEGER NOT NULL,
                session_name TEXT NOT NULL,
                PRIMARY KEY (category_id, session_name),
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
                FOREIGN KEY (session_name) REFERENCES accounts(session_name) ON DELETE CASCADE
            )
        """)

        # Таблица активной категории (только одна может быть активна)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_category (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                category_id INTEGER,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
            )
        """)

        # Таблица лидов (для детальной статистики)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                phone TEXT,
                source_channel TEXT,
                original_post_text TEXT,
                category_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
            )
        """)
        
        # Миграция: добавляем category_id в leads если его нет
        try:
            cursor.execute("ALTER TABLE leads ADD COLUMN category_id INTEGER")
        except sqlite3.OperationalError:
            # Колонка уже существует
            pass

        # Таблица менеджеров (связь user_id с category_id)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS managers (
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, category_id),
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )
        """)

        # Создаем дефолтный шаблон если его нет
        cursor.execute("SELECT COUNT(*) FROM message_templates")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO message_templates (template_text) 
                VALUES ('Привет! Заинтересовало твое сообщение. Давай обсудим детали?')
            """)

        # Создаем запись для глобальных настроек API если её нет
        cursor.execute("SELECT COUNT(*) FROM global_api_settings")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO global_api_settings (api_id, api_hash, is_active) 
                VALUES ('', '', 0)
            """)

        conn.commit()
        conn.close()

    # ========== АДМИНЫ ==========
    def add_admin(self, user_id: int) -> bool:
        """Добавить админа"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding admin: {e}")
            return False

    def is_admin(self, user_id: int) -> bool:
        """Проверить является ли пользователь админом"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        result = cursor.fetchone() is not None
        conn.close()
        return result

    # ========== АККАУНТЫ ==========
    def add_account(self, session_name: str, phone: str, api_id: str = "", api_hash: str = "", status: str = "Active") -> bool:
        """Добавить аккаунт"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO accounts (session_name, phone, api_id, api_hash, status, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (session_name, phone, api_id, api_hash, status))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding account: {e}")
            return False

    def get_account(self, session_name: str) -> Optional[dict]:
        """Получить аккаунт по session_name"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM accounts WHERE session_name = ?", (session_name,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_all_accounts(self) -> List[dict]:
        """Получить все аккаунты"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM accounts")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def update_account_status(self, session_name: str, status: str):
        """Обновить статус аккаунта"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE accounts SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE session_name = ?
        """, (status, session_name))
        conn.commit()
        conn.close()

    def delete_account(self, session_name: str) -> bool:
        """Удалить аккаунт"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM accounts WHERE session_name = ?", (session_name,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error deleting account: {e}")
            return False

    # ========== КАНАЛЫ ==========
    def add_channel(self, link: str, title: str = "") -> bool:
        """Добавить канал"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO channels (link, title) VALUES (?, ?)", (link, title))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding channel: {e}")
            return False

    def get_all_channels(self) -> List[dict]:
        """Получить все каналы"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def delete_channels(self, channel_ids: List[int]) -> bool:
        """Удалить каналы"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(channel_ids))
            cursor.execute(f"DELETE FROM channels WHERE id IN ({placeholders})", channel_ids)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error deleting channels: {e}")
            return False

    # ========== ПРИВАТНЫЕ ГРУППЫ (STATE MACHINE) ==========
    # Разрешенные переходы состояний:
    # NEW → ASSIGNED (назначение аккаунта)
    # ASSIGNED → JOIN_QUEUED (готовность к join)
    # JOIN_QUEUED → JOINING (начало join)
    # JOINING → JOINED (успешный join)
    # JOINED → ACTIVE (первое сообщение обработано)
    # ACTIVE → LOST_ACCESS (ошибка get_chat)
    # LOST_ACCESS → DISABLED (превышен лимит ошибок)
    # DISABLED → NEW (ручная реактивация)

    def add_private_group(self, invite_link: str, category_id: Optional[int] = None) -> Optional[int]:
        """Добавить приватную группу (state=NEW)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO private_groups (invite_link, category_id, state) VALUES (?, ?, 'NEW')",
                (invite_link, category_id),
            )
            group_id = cursor.lastrowid
            
            # Если уже была (INSERT OR IGNORE), получаем её ID
            if group_id == 0:
                cursor.execute("SELECT id FROM private_groups WHERE invite_link = ? AND category_id = ?", (invite_link, category_id))
                row = cursor.fetchone()
                group_id = row[0] if row else None
            
            conn.commit()
            conn.close()
            return group_id
        except Exception as e:
            print(f"Error adding private group: {e}")
            return None

    def get_private_group_by_id(self, group_id: int) -> Optional[dict]:
        """Получить приватную группу по ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM private_groups WHERE id = ?", (group_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_private_group_by_chat_id(self, chat_id: int) -> Optional[dict]:
        """Получить приватную группу по chat_id"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM private_groups WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_all_private_groups(self, category_id: Optional[int] = None) -> List[dict]:
        """Получить все приватные группы (опционально по категории)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        if category_id:
            cursor.execute("SELECT * FROM private_groups WHERE category_id = ? ORDER BY id DESC", (category_id,))
        else:
            cursor.execute("SELECT * FROM private_groups ORDER BY id DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_private_groups_by_category(self, category_id: int) -> List[dict]:
        """Получить приватные группы по категории"""
        return self.get_all_private_groups(category_id=category_id)

    def get_private_groups_by_state(self, state: str) -> List[dict]:
        """Получить группы в определённом состоянии"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM private_groups WHERE state = ? AND is_active = 1 ORDER BY created_at ASC",
            (state,),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_private_groups_ready_for_join(self) -> List[dict]:
        """Получить группы готовые к join (state=JOIN_QUEUED и next_retry_at <= now или NULL)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM private_groups
            WHERE state = 'JOIN_QUEUED' 
            AND is_active = 1
            AND (next_retry_at IS NULL OR next_retry_at <= datetime('now'))
            ORDER BY created_at ASC
            """
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_private_groups_stuck_in_joining(self, max_minutes: int = 10) -> List[dict]:
        """Получить группы застрявшие в JOINING (дольше max_minutes)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM private_groups
            WHERE state = 'JOINING'
            AND is_active = 1
            AND (
                last_join_attempt_at IS NULL 
                OR datetime(last_join_attempt_at, '+' || ? || ' minutes') < datetime('now')
            )
            """,
            (max_minutes,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_private_groups_by_session(self, session_name: str, states: Optional[List[str]] = None) -> List[dict]:
        """Получить приватные группы по сессии и опционально по состояниям"""
        conn = self.get_connection()
        cursor = conn.cursor()
        if states:
            placeholders = ','.join('?' * len(states))
            cursor.execute(
                f"SELECT * FROM private_groups WHERE assigned_session_name = ? AND state IN ({placeholders}) AND is_active = 1",
                (session_name, *states),
            )
        else:
            cursor.execute(
                "SELECT * FROM private_groups WHERE assigned_session_name = ? AND is_active = 1",
                (session_name,),
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def count_private_groups_by_session(self, session_name: str, states: Optional[List[str]] = None) -> int:
        """Посчитать группы на аккаунте (опционально по состояниям)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        if states:
            placeholders = ','.join('?' * len(states))
            cursor.execute(
                f"SELECT COUNT(*) FROM private_groups WHERE assigned_session_name = ? AND state IN ({placeholders}) AND is_active = 1",
                (session_name, *states),
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM private_groups WHERE assigned_session_name = ? AND is_active = 1",
                (session_name,),
            )
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def transition_private_group_state(
        self,
        group_id: int,
        from_state: str,
        to_state: str,
        updates: Optional[dict] = None
    ) -> bool:
        """Атомарный переход состояния (защита от race conditions)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Сначала проверяем текущее состояние
            cursor.execute("SELECT state FROM private_groups WHERE id = ?", (group_id,))
            row = cursor.fetchone()
            if not row or row[0] != from_state:
                conn.close()
                return False
            
            # Формируем UPDATE
            set_clause = "state = ?, updated_at = CURRENT_TIMESTAMP"
            params = [to_state]
            
            if updates:
                for key, value in updates.items():
                    set_clause += f", {key} = ?"
                    params.append(value)
            
            params.append(group_id)
            params.append(from_state)
            
            cursor.execute(
                f"""
                UPDATE private_groups
                SET {set_clause}
                WHERE id = ? AND state = ?
                """,
                params,
            )
            
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            print(f"Error transitioning private group state: {e}")
            return False

    def update_private_group(self, group_id: int, updates: dict) -> bool:
        """Обновить поля группы без смены state"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            set_clause = "updated_at = CURRENT_TIMESTAMP"
            params = []
            
            for key, value in updates.items():
                set_clause += f", {key} = ?"
                params.append(value)
            
            params.append(group_id)
            
            cursor.execute(
                f"UPDATE private_groups SET {set_clause} WHERE id = ?",
                params,
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error updating private group: {e}")
            return False

    def increment_private_group_error(self, group_id: int, error_msg: str) -> int:
        """Увеличить счётчик ошибок и вернуть новое значение"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE private_groups
                SET consecutive_errors = consecutive_errors + 1,
                    last_error = ?,
                    last_checked_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error_msg, group_id),
            )
            
            cursor.execute("SELECT consecutive_errors FROM private_groups WHERE id = ?", (group_id,))
            row = cursor.fetchone()
            count = row[0] if row else 0
            
            conn.commit()
            conn.close()
            return count
        except Exception as e:
            print(f"Error incrementing error count: {e}")
            return 0

    def reset_private_group_errors(self, group_id: int) -> bool:
        """Сбросить счётчик ошибок"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE private_groups
                SET consecutive_errors = 0,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (group_id,),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error resetting errors: {e}")
            return False

    def reactivate_private_group(self, group_id: int) -> bool:
        """Реактивировать группу (DISABLED → NEW для повторного прохода цикла)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE private_groups
                SET state = 'NEW',
                    is_active = 1,
                    assigned_session_name = NULL,
                    chat_id = NULL,
                    retry_count = 0,
                    consecutive_errors = 0,
                    next_retry_at = NULL,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (group_id,),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error reactivating private group: {e}")
            return False

    def delete_private_group(self, group_id: int) -> bool:
        """Удалить приватную группу из БД по ID"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM private_groups WHERE id = ?", (group_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            print(f"Error deleting private group: {e}")
            return False

    # ========== КЛЮЧЕВЫЕ СЛОВА ==========
    def add_keywords(self, words: List[str]) -> int:
        """Добавить ключевые слова"""
        conn = self.get_connection()
        cursor = conn.cursor()
        count = 0
        for word in words:
            word = word.strip().lower()
            if word:
                try:
                    cursor.execute("INSERT OR IGNORE INTO keywords (word) VALUES (?)", (word,))
                    count += 1
                except:
                    pass
        conn.commit()
        conn.close()
        return count

    def get_all_keywords(self) -> List[str]:
        """Получить все ключевые слова"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM keywords")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def delete_keywords(self, keyword_ids: List[int]) -> int:
        """Удалить ключевые слова. Возвращает количество удалённых строк."""
        if not keyword_ids:
            return 0
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(keyword_ids))
            cursor.execute(f"DELETE FROM keywords WHERE id IN ({placeholders})", keyword_ids)
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted_count
        except Exception as e:
            print(f"Error deleting keywords: {e}")
            return 0

    def get_all_keywords_with_ids(self) -> List[dict]:
        """Получить все ключевые слова с ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, word FROM keywords")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # ========== СТОП-СЛОВА ==========
    def add_stopwords(self, words: List[str]) -> int:
        """Добавить стоп-слова"""
        conn = self.get_connection()
        cursor = conn.cursor()
        count = 0
        for word in words:
            word = word.strip().lower()
            if word:
                try:
                    cursor.execute("INSERT OR IGNORE INTO stopwords (word) VALUES (?)", (word,))
                    count += 1
                except:
                    pass
        conn.commit()
        conn.close()
        return count

    def get_all_stopwords(self) -> List[str]:
        """Получить все стоп-слова"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM stopwords")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def delete_stopwords(self, stopword_ids: List[int]) -> int:
        """Удалить стоп-слова. Возвращает количество удалённых строк."""
        if not stopword_ids:
            return 0
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(stopword_ids))
            cursor.execute(f"DELETE FROM stopwords WHERE id IN ({placeholders})", stopword_ids)
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted_count
        except Exception as e:
            print(f"Error deleting stopwords: {e}")
            return 0

    def get_all_stopwords_with_ids(self) -> List[dict]:
        """Получить все стоп-слова с ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, word FROM stopwords")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # ========== ШАБЛОНЫ СООБЩЕНИЙ ==========
    def get_active_template(self) -> str:
        """Получить активный шаблон сообщения"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT template_text FROM message_templates WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else "Привет! Заинтересовало твое сообщение. Давай обсудим детали?"

    def update_template(self, template_text: str) -> bool:
        """Обновить шаблон сообщения"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Деактивируем старые
            cursor.execute("UPDATE message_templates SET is_active = 0")
            # Добавляем новый
            cursor.execute("INSERT INTO message_templates (template_text, is_active) VALUES (?, 1)", (template_text,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error updating template: {e}")
            return False

    # ========== ОБРАБОТАННЫЕ ПОЛЬЗОВАТЕЛИ ==========
    def is_user_processed(self, user_id: int) -> bool:
        """Проверить обработан ли пользователь"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM processed_users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone() is not None
        conn.close()
        return result

    def can_repeat_message_to_user(self, user_id: int, minutes: int = 10) -> bool:
        """Проверить можно ли снова писать пользователю (если прошло больше minutes минут)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        # Проверяем что прошло больше указанного количества минут
        cursor.execute("""
            SELECT 1 FROM processed_users 
            WHERE user_id = ? 
            AND datetime(timestamp, '+' || ? || ' minutes') <= datetime('now')
        """, (user_id, minutes))
        result = cursor.fetchone() is not None
        conn.close()
        return result

    def mark_user_processed(self, user_id: int, username: str = "", channel_source: str = "", original_post_text: str = ""):
        """Пометить пользователя как обработанного"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO processed_users (user_id, username, channel_source, original_post_text, timestamp)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (user_id, username, channel_source, original_post_text))
        conn.commit()
        conn.close()

    def get_user_info(self, user_id: int) -> Optional[dict]:
        """Получить информацию о пользователе"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM processed_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    # ========== ЛИДЫ ==========
    def add_lead(self, user_id: int, username: str, phone: str, source_channel: str, original_post_text: str):
        """Добавить лид"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leads (user_id, username, phone, source_channel, original_post_text)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, phone, source_channel, original_post_text))
        conn.commit()
        conn.close()

    def get_leads_count(self, days: Optional[int] = None) -> int:
        """Получить количество лидов за период"""
        conn = self.get_connection()
        cursor = conn.cursor()
        if days:
            cursor.execute("""
                SELECT COUNT(*) FROM leads 
                WHERE created_at >= datetime('now', '-' || ? || ' days')
            """, (days,))
        else:
            cursor.execute("SELECT COUNT(*) FROM leads")
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_leads_by_channel(self, channel_id: int) -> int:
        """Получить количество лидов по каналу"""
        conn = self.get_connection()
        cursor = conn.cursor()
        channel = self.get_channel_by_id(channel_id)
        if not channel:
            return 0
        cursor.execute("""
            SELECT COUNT(*) FROM leads WHERE source_channel = ?
        """, (channel['link'],))
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_channel_by_id(self, channel_id: int) -> Optional[dict]:
        """Получить канал по ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    # ========== ГЛОБАЛЬНЫЕ НАСТРОЙКИ API ==========
    def get_global_api_settings(self) -> Optional[dict]:
        """Получить глобальные настройки API"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM global_api_settings WHERE is_active = 1 LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def set_global_api_settings(self, api_id: str, api_hash: str) -> bool:
        """Установить глобальные настройки API"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Деактивируем старые настройки
            cursor.execute("UPDATE global_api_settings SET is_active = 0")
            # Добавляем новые
            cursor.execute("""
                INSERT INTO global_api_settings (api_id, api_hash, is_active, updated_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            """, (api_id, api_hash))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error setting global API settings: {e}")
            return False

    def clear_global_api_settings(self) -> bool:
        """Очистить глобальные настройки API"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE global_api_settings SET is_active = 0")
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error clearing global API settings: {e}")
            return False

    # ========== КАНАЛ МЕНЕДЖЕРОВ ==========
    def get_managers_channel_id(self) -> Optional[int]:
        """Получить ID канала менеджеров из БД"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id FROM managers_channel_settings ORDER BY updated_at DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        return int(row['channel_id']) if row else None

    def set_managers_channel_id(self, channel_id: int) -> bool:
        """Установить ID канала менеджеров"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Удаляем старые записи и добавляем новую
            cursor.execute("DELETE FROM managers_channel_settings")
            cursor.execute("""
                INSERT INTO managers_channel_settings (channel_id, updated_at)
                VALUES (?, CURRENT_TIMESTAMP)
            """, (channel_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error setting managers channel ID: {e}")
            return False

    def clear_managers_channel_id(self) -> bool:
        """Очистить настройки канала менеджеров"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM managers_channel_settings")
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error clearing managers channel ID: {e}")
            return False

    # ========== КАТЕГОРИИ ==========
    def add_category(self, name: str, managers_channel_id: Optional[int] = None) -> Optional[int]:
        """Добавить категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Проверяем есть ли поле assigned_session_name (старая структура)
            try:
                cursor.execute("""
                    INSERT INTO categories (name, managers_channel_id, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """, (name, managers_channel_id))
            except sqlite3.OperationalError:
                # Старая структура с assigned_session_name
                cursor.execute("""
                    INSERT INTO categories (name, assigned_session_name, managers_channel_id, updated_at)
                    VALUES (?, NULL, ?, CURRENT_TIMESTAMP)
                """, (name, managers_channel_id))
            category_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return category_id
        except Exception as e:
            print(f"Error adding category: {e}")
            return None

    def get_category(self, category_id: int) -> Optional[dict]:
        """Получить категорию по ID"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def get_category_message_text(self, category_id: int) -> Optional[str]:
        """Получить текст сообщения категории (или None если используется глобальный шаблон)"""
        category = self.get_category(category_id)
        if category and category.get('message_text'):
            return category['message_text']
        return None

    def get_category_follow_up_message(self, category_id: int) -> Optional[str]:
        """Получить текст повторного сообщения категории (или None если используется глобальный шаблон)"""
        category = self.get_category(category_id)
        if category and category.get('follow_up_message'):
            return category['follow_up_message']
        return None

    def get_all_categories(self) -> List[dict]:
        """Получить все категории"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM categories ORDER BY name")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def update_category(self, category_id: int, updates: dict) -> bool:
        """Обновить категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            set_clause = "updated_at = CURRENT_TIMESTAMP"
            params = []
            
            for key, value in updates.items():
                set_clause += f", {key} = ?"
                params.append(value)
            
            params.append(category_id)
            
            cursor.execute(
                f"UPDATE categories SET {set_clause} WHERE id = ?",
                params,
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error updating category: {e}")
            return False

    def delete_category(self, category_id: int) -> bool:
        """Удалить категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM categories WHERE id = ?", (category_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error deleting category: {e}")
            return False

    def set_active_category(self, category_id: Optional[int]) -> bool:
        """Установить активную категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Удаляем старую запись
            cursor.execute("DELETE FROM active_category")
            # Добавляем новую
            if category_id:
                cursor.execute("INSERT INTO active_category (id, category_id) VALUES (1, ?)", (category_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error setting active category: {e}")
            return False

    def get_active_category(self) -> Optional[dict]:
        """Получить активную категорию"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT category_id FROM active_category WHERE id = 1")
        row = cursor.fetchone()
        if not row or not row[0]:
            conn.close()
            return None
        
        category_id = row[0]
        cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
        category_row = cursor.fetchone()
        conn.close()
        return dict(category_row) if category_row else None

    def get_category_channels(self, category_id: int) -> List[dict]:
        """Получить каналы категории"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.* FROM channels c
            INNER JOIN category_channels cc ON c.id = cc.channel_id
            WHERE cc.category_id = ?
        """, (category_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def add_category_channel(self, category_id: int, channel_id: int) -> bool:
        """Добавить канал в категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO category_channels (category_id, channel_id)
                VALUES (?, ?)
            """, (category_id, channel_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding channel to category: {e}")
            return False

    def remove_category_channel(self, category_id: int, channel_id: int) -> bool:
        """Удалить канал из категории"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM category_channels
                WHERE category_id = ? AND channel_id = ?
            """, (category_id, channel_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error removing channel from category: {e}")
            return False

    def get_channel_categories(self, channel_id: int) -> List[int]:
        """Получить список категорий канала"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT category_id FROM category_channels
            WHERE channel_id = ?
        """, (channel_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def get_channel_categories_by_link(self, channel_link: str) -> List[int]:
        """Получить список категорий канала по ссылке"""
        conn = self.get_connection()
        cursor = conn.cursor()
        # Находим канал по ссылке
        cursor.execute("SELECT id FROM channels WHERE link = ?", (channel_link,))
        channel_row = cursor.fetchone()
        if not channel_row:
            conn.close()
            return []
        
        channel_id = channel_row[0]
        # Получаем категории канала
        cursor.execute("""
            SELECT category_id FROM category_channels
            WHERE channel_id = ?
        """, (channel_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def get_category_keywords(self, category_id: int) -> List[str]:
        """Получить ключевые слова категории"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT k.word FROM keywords k
            INNER JOIN category_keywords ck ON k.id = ck.keyword_id
            WHERE ck.category_id = ?
        """, (category_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def add_category_keyword(self, category_id: int, keyword_id: int) -> bool:
        """Добавить ключевое слово в категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO category_keywords (category_id, keyword_id)
                VALUES (?, ?)
            """, (category_id, keyword_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding keyword to category: {e}")
            return False

    def remove_category_keyword(self, category_id: int, keyword_id: int) -> int:
        """Удалить ключевое слово из категории. Возвращает количество удалённых строк."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM category_keywords
                WHERE category_id = ? AND keyword_id = ?
            """, (category_id, keyword_id))
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted_count
        except Exception as e:
            print(f"Error removing keyword from category: {e}")
            return 0

    def get_category_stopwords(self, category_id: int) -> List[str]:
        """Получить стоп-слова категории"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.word FROM stopwords s
            INNER JOIN category_stopwords cs ON s.id = cs.stopword_id
            WHERE cs.category_id = ?
        """, (category_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def add_category_stopword(self, category_id: int, stopword_id: int) -> bool:
        """Добавить стоп-слово в категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO category_stopwords (category_id, stopword_id)
                VALUES (?, ?)
            """, (category_id, stopword_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding stopword to category: {e}")
            return False

    def remove_category_stopword(self, category_id: int, stopword_id: int) -> int:
        """Удалить стоп-слово из категории. Возвращает количество удалённых строк."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM category_stopwords
                WHERE category_id = ? AND stopword_id = ?
            """, (category_id, stopword_id))
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted_count
        except Exception as e:
            print(f"Error removing stopword from category: {e}")
            return 0

    # ========== USERBOT'Ы КАТЕГОРИЙ (многие-ко-многим) ==========
    def get_category_userbots(self, category_id: int) -> List[str]:
        """Получить список userbot'ов категории"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT session_name FROM category_userbots
            WHERE category_id = ?
        """, (category_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def add_category_userbot(self, category_id: int, session_name: str) -> bool:
        """Добавить userbot в категорию"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO category_userbots (category_id, session_name)
                VALUES (?, ?)
            """, (category_id, session_name))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding userbot to category: {e}")
            return False

    def remove_category_userbot(self, category_id: int, session_name: str) -> bool:
        """Удалить userbot из категории"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM category_userbots
                WHERE category_id = ? AND session_name = ?
            """, (category_id, session_name))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error removing userbot from category: {e}")
            return False

    def get_userbot_categories(self, session_name: str) -> List[int]:
        """Получить список категорий userbot'а"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT category_id FROM category_userbots
            WHERE session_name = ?
        """, (session_name,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def get_all_category_userbots(self) -> List[dict]:
        """Получить все связи категорий и userbot'ов"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT category_id, session_name FROM category_userbots
        """)
        rows = cursor.fetchall()
        conn.close()
        return [{"category_id": row[0], "session_name": row[1]} for row in rows]

    # ========== МЕНЕДЖЕРЫ ==========
    def add_manager(self, user_id: int, category_id: int) -> bool:
        """Добавить менеджера к категории"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO managers (user_id, category_id)
                VALUES (?, ?)
            """, (user_id, category_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding manager: {e}")
            return False

    def remove_manager(self, user_id: int, category_id: int) -> bool:
        """Удалить менеджера из категории"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM managers
                WHERE user_id = ? AND category_id = ?
            """, (user_id, category_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error removing manager: {e}")
            return False

    def get_manager_category(self, user_id: int) -> Optional[int]:
        """Получить category_id для менеджера (возвращает первый, если несколько)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT category_id FROM managers
            WHERE user_id = ?
            LIMIT 1
        """, (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def get_manager_categories(self, user_id: int) -> List[int]:
        """Получить все category_id для менеджера"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT category_id FROM managers
            WHERE user_id = ?
        """, (user_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def is_manager(self, user_id: int) -> bool:
        """Проверить является ли пользователь менеджером"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM managers WHERE user_id = ?", (user_id,))
        result = cursor.fetchone() is not None
        conn.close()
        return result

    def get_category_managers(self, category_id: int) -> List[int]:
        """Получить список user_id менеджеров категории"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id FROM managers
            WHERE category_id = ?
        """, (category_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def can_access_category(self, user_id: int, category_id: int) -> bool:
        """Проверить может ли пользователь получить доступ к категории (админ или менеджер этой категории)"""
        if self.is_admin(user_id):
            return True
        manager_categories = self.get_manager_categories(user_id)
        return category_id in manager_categories

    def get_category_command(self, category_id: int) -> Optional[str]:
        """Получить команду для категории (на основе названия)"""
        category = self.get_category(category_id)
        if not category:
            return None
        # Преобразуем название в команду: убираем пробелы, делаем нижний регистр
        name = category['name'].lower().strip()
        # Убираем спецсимволы, оставляем только буквы и цифры
        import re
        command = re.sub(r'[^а-яёa-z0-9]', '', name)
        return command if command else None

    def get_category_by_command(self, command: str) -> Optional[dict]:
        """Получить категорию по команде"""
        categories = self.get_all_categories()
        for cat in categories:
            cat_command = self.get_category_command(cat['id'])
            if cat_command and cat_command == command.lower():
                return cat
        return None

    # ========== СТАТИСТИКА КАТЕГОРИЙ ==========
    def get_category_leads_count(self, category_id: int, days: Optional[int] = None) -> int:
        """Получить количество лидов категории за период"""
        conn = self.get_connection()
        cursor = conn.cursor()
        if days:
            cursor.execute("""
                SELECT COUNT(*) FROM leads 
                WHERE category_id = ? AND created_at >= datetime('now', '-' || ? || ' days')
            """, (category_id, days))
        else:
            cursor.execute("SELECT COUNT(*) FROM leads WHERE category_id = ?", (category_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_category_stats(self, category_id: int) -> dict:
        """Получить статистику категории"""
        groups = self.get_private_groups_by_category(category_id)
        active_groups = [g for g in groups if g.get('is_active') and g.get('state') == 'ACTIVE']
        keywords = self.get_category_keywords(category_id)
        stopwords = self.get_category_stopwords(category_id)
        userbots = self.get_category_userbots(category_id)
        total_leads = self.get_category_leads_count(category_id)
        today_leads = self.get_category_leads_count(category_id, days=1)
        week_leads = self.get_category_leads_count(category_id, days=7)
        month_leads = self.get_category_leads_count(category_id, days=30)
        
        return {
            'total_groups': len(groups),
            'active_groups': len(active_groups),
            'keywords_count': len(keywords),
            'stopwords_count': len(stopwords),
            'userbots_count': len(userbots),
            'total_leads': total_leads,
            'today_leads': today_leads,
            'week_leads': week_leads,
            'month_leads': month_leads,
        }

    def get_all_categories_stats(self) -> dict:
        """Получить статистику по всем категориям"""
        categories = self.get_all_categories()
        total_leads = self.get_leads_count()
        today_leads = self.get_leads_count(days=1)
        week_leads = self.get_leads_count(days=7)
        month_leads = self.get_leads_count(days=30)
        
        categories_stats = []
        for cat in categories:
            cat_stats = self.get_category_stats(cat['id'])
            categories_stats.append({
                'category': cat,
                'stats': cat_stats
            })
        
        return {
            'total_categories': len(categories),
            'total_leads': total_leads,
            'today_leads': today_leads,
            'week_leads': week_leads,
            'month_leads': month_leads,
            'categories': categories_stats,
        }

    def get_category_full_info(self, category_id: int) -> Optional[dict]:
        """Получить полную информацию о категории одним запросом (оптимизация)"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Получаем категорию
            cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
            category_row = cursor.fetchone()
            if not category_row:
                return None
            
            category = dict(category_row)
            
            # Получаем userbot'ы
            cursor.execute("""
                SELECT session_name FROM category_userbots
                WHERE category_id = ?
            """, (category_id,))
            category['userbots'] = [row[0] for row in cursor.fetchall()]
            
            # Получаем количество групп
            cursor.execute("""
                SELECT COUNT(*) FROM private_groups
                WHERE category_id = ?
            """, (category_id,))
            category['groups_count'] = cursor.fetchone()[0]
            
            # Получаем количество ключевых слов
            cursor.execute("""
                SELECT COUNT(*) FROM category_keywords
                WHERE category_id = ?
            """, (category_id,))
            category['keywords_count'] = cursor.fetchone()[0]
            
            # Получаем количество стоп-слов
            cursor.execute("""
                SELECT COUNT(*) FROM category_stopwords
                WHERE category_id = ?
            """, (category_id,))
            category['stopwords_count'] = cursor.fetchone()[0]
            
            return category
        except Exception as e:
            print(f"Error getting category full info: {e}")
            return None
        finally:
            if conn:
                conn.close()
