"""
PrivateGroupCoordinator — единственный источник истины для управления приватными группами.

Работает через reconcile-паттерн: периодически проверяет состояние всех групп в БД
и выполняет необходимые действия (назначение аккаунта, join, проверка доступа).

State machine:
NEW → ASSIGNED → JOIN_QUEUED → JOINING → JOINED → ACTIVE → LOST_ACCESS → DISABLED
"""
import asyncio
import re
from urllib.parse import urlparse
from datetime import datetime, timedelta
import json
import time
import os
from typing import Dict, Optional, Set
from pyrogram import Client
from pyrogram.errors import FloodWait

try:
    from pyrogram.errors import (
        InviteHashInvalid, InviteHashExpired, UserAlreadyParticipant,
        ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UsernameNotOccupied
    )
except ImportError:
    InviteHashInvalid = InviteHashExpired = UserAlreadyParticipant = Exception
    ChatAdminRequired = ChannelPrivate = PeerIdInvalid = UsernameNotOccupied = Exception

from database.models import Database
import config

db = Database()

# Правила для username Telegram: 5-32 символа, буквы/цифры/подчеркивание
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

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


class PrivateGroupCoordinator:
    """Coordinator приватных групп — единственный кто управляет их состоянием"""
    
    def __init__(self, clients_dict: Dict[str, Client]):
        self.clients = clients_dict  # Ссылка на клиенты UserbotManager
        self.running = False
        self.reconcile_task: Optional[asyncio.Task] = None
        self.active_join_tasks: Set[int] = set()  # Контроль одновременных join
        self.lost_access_retry_counts: Dict[int, int] = {}  # Счетчики повторов

    async def start(self):
        """Запустить coordinator"""
        if self.running:
            return
        
        self.running = True
        _dbg("H3", "private_group_coordinator.py:start", "Coordinator starting", {"clients": len(self.clients)})
        self.reconcile_task = asyncio.create_task(self._reconcile_loop())
        print("[PrivateGroupCoordinator] Started")
        _dbg("H3", "private_group_coordinator.py:start", "Coordinator started", {"reconcile_task": True})

    async def stop(self):
        """Остановить coordinator"""
        self.running = False
        
        if self.reconcile_task:
            self.reconcile_task.cancel()
        
        self.active_join_tasks.clear()
        self.lost_access_retry_counts.clear()
        print("[PrivateGroupCoordinator] Stopped")

    async def _reconcile_loop(self):
        """Основной цикл: проверка состояния групп и выполнение действий"""
        while self.running:
            try:
                await self._reconcile_once()
                await asyncio.sleep(config.PRIVATE_GROUP_RECONCILE_INTERVAL)
            except Exception as e:
                print(f"[PrivateGroupCoordinator] Error in reconcile loop: {e}")
                _dbg("H4", "private_group_coordinator.py:_reconcile_loop", "Reconcile loop exception", {"error": str(e)[:200]})
                await asyncio.sleep(10)

    async def _reconcile_once(self):
        """Один проход: обработка всех групп по состоянию"""
        # JOINING → JOIN_QUEUED (застрявшие)
        await self._recover_stuck_joining_groups()
        
        # NEW → ASSIGNED (назначение аккаунта)
        await self._process_new_groups()
        
        # ASSIGNED → JOIN_QUEUED (подготовка к join)
        await self._process_assigned_groups()
        
        # JOIN_QUEUED → JOINING (выполнение join)
        await self._process_join_queued_groups()
        
        # JOINED → ACTIVE (проверка доступности)
        await self._process_joined_groups()
        
        # ACTIVE → LOST_ACCESS (проверка доступа)
        await self._process_active_groups()
        
        # LOST_ACCESS → DISABLED или восстановление
        await self._process_lost_access_groups()

    async def _recover_stuck_joining_groups(self):
        """JOINING → JOIN_QUEUED: вернуть застрявшие группы в очередь"""
        groups = db.get_private_groups_stuck_in_joining(
            max_minutes=config.PRIVATE_GROUP_JOINING_TIMEOUT_MINUTES
        )
        
        for group in groups:
            # Читаем актуальное состояние
            fresh_group = db.get_private_group_by_id(group['id'])
            if not fresh_group or fresh_group['state'] != 'JOINING':
                continue
            
            retry_count = fresh_group.get('retry_count', 0) + 1
            
            # Экспоненциальная задержка
            delay_minutes = min(2 ** retry_count, 60)
            next_retry = datetime.now() + timedelta(minutes=delay_minutes)
            
            success = db.transition_private_group_state(
                group['id'],
                'JOINING',
                'JOIN_QUEUED',
                {
                    'retry_count': retry_count,
                    'next_retry_at': next_retry.isoformat(),
                    'last_error': 'Join timeout - requeued'
                }
            )
            
            if success:
                print(f"[Coordinator] Recovered stuck group {group['id']} from JOINING → JOIN_QUEUED (retry #{retry_count})")
            
            # Убираем из активных (если был)
            self.active_join_tasks.discard(group['id'])

    async def _process_new_groups(self):
        """NEW → ASSIGNED: назначить аккаунт группам без аккаунта"""
        groups = db.get_private_groups_by_state('NEW')
        if not groups:
            return
        
        # Получаем активные аккаунты
        accounts = db.get_all_accounts()
        active_accounts = [acc for acc in accounts if acc['status'] == 'Active']
        
        if not active_accounts:
            return
        
        for group in groups:
            # Выбираем аккаунт с наименьшим количеством групп
            account = self._pick_least_loaded_account(active_accounts)
            if not account:
                continue
            
            # Проверяем лимит
            current_count = db.count_private_groups_by_session(
                account['session_name'],
                states=['ASSIGNED', 'JOIN_QUEUED', 'JOINING', 'JOINED', 'ACTIVE']
            )
            
            if current_count >= config.MAX_PRIVATE_GROUPS_PER_ACCOUNT:
                continue
            
            # Назначаем
            success = db.transition_private_group_state(
                group['id'],
                'NEW',
                'ASSIGNED',
                {'assigned_session_name': account['session_name']}
            )
            
            if success:
                print(f"[Coordinator] Group {group['id']} assigned to {account['session_name']}")

    async def _process_assigned_groups(self):
        """ASSIGNED → JOIN_QUEUED: подготовить к join"""
        groups = db.get_private_groups_by_state('ASSIGNED')
        
        for group in groups:
            # Просто переводим в JOIN_QUEUED (ready to join)
            success = db.transition_private_group_state(
                group['id'],
                'ASSIGNED',
                'JOIN_QUEUED'
            )
            
            if success:
                print(f"[Coordinator] Group {group['id']} queued for join")

    def _can_start_new_join(self) -> bool:
        """Проверить можно ли запустить новый join (rate limit)"""
        return len(self.active_join_tasks) < config.PRIVATE_GROUP_MAX_CONCURRENT_JOINS

    async def _process_join_queued_groups(self):
        """JOIN_QUEUED → JOINING: выполнить join"""
        if not self._can_start_new_join():
            return
        
        groups = db.get_private_groups_ready_for_join()
        
        for group in groups:
            if not self._can_start_new_join():
                break
            
            # Проверяем что join task ещё не запущена
            if group['id'] in self.active_join_tasks:
                continue

            # Не считаем память источником истины: перепроверяем state в БД прямо перед стартом
            fresh = db.get_private_group_by_id(group["id"])
            if not fresh or fresh.get("state") != "JOIN_QUEUED" or not fresh.get("is_active"):
                continue
            
            # Проверяем что клиент доступен
            client = self.clients.get(group['assigned_session_name'])
            if not client:
                continue
            
            # Запускаем join task
            self.active_join_tasks.add(group['id'])
            asyncio.create_task(self._perform_join(group, client))

    @staticmethod
    def _normalize_join_target(raw: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Нормализация ссылки для join.

        Returns:
            (join_target, kind, error)
            kind: "private" | "public"
        """
        if not raw:
            return None, None, "empty link"

        s = raw.strip()
        if not s:
            return None, None, "empty link"

        # normalize scheme for telegram short links
        if s.startswith("t.me/") or s.startswith("telegram.me/"):
            s = "https://" + s

        # +HASH as private invite (no URL)
        if s.startswith("+") and len(s) > 1:
            h = s[1:].split("?", 1)[0].strip()
            if not h:
                return None, None, "invalid invite hash"
            return f"https://t.me/+{h}", "private", None

        # @username
        if s.startswith("@") and len(s) > 1:
            username = s[1:].strip()
            if _USERNAME_RE.fullmatch(username):
                return username, "public", None
            return None, None, "invalid @username"

        # Full URL forms
        if s.startswith("http://") or s.startswith("https://"):
            try:
                parsed = urlparse(s)
            except Exception:
                return None, None, "invalid url"

            host = (parsed.netloc or "").lower()
            if not (host.endswith("t.me") or host.endswith("telegram.me")):
                return None, None, "unsupported host"

            path = (parsed.path or "").strip("/")
            if not path:
                return None, None, "empty telegram path"

            # Приватные инвайты
            if path.startswith("+") or path.startswith("joinchat/"):
                first = path.split("/", 2)
                if first[0].startswith("+"):
                    canonical = f"https://t.me/{first[0]}"
                else:
                    canonical = f"https://t.me/{first[0]}/{first[1]}" if len(first) > 1 else f"https://t.me/{path}"
                return canonical, "private", None

            # Заблокированные пути
            if path.startswith("s/") or path.startswith("c/"):
                return None, None, "service link, not a chat"

            # Публичный username
            username = path.split("/", 1)[0]
            if _USERNAME_RE.fullmatch(username):
                return username, "public", None
            return None, None, "invalid username in url"

        # Обычный username
        if _USERNAME_RE.fullmatch(s):
            return s, "public", None

        return None, None, "unrecognized link format"


    async def _perform_join(self, group: dict, client: Client):
        """Выполнить join в группу/канал (приватная через invite hash, публичная по username/link)."""
        group_id = group['id']
        
        try:
            _dbg("H3", "private_group_coordinator.py:_perform_join:entry", "Starting join attempt", {"group_id": group_id, "state_from": "JOIN_QUEUED"})
            # Переводим в JOINING
            success = db.transition_private_group_state(
                group_id,
                'JOIN_QUEUED',
                'JOINING',
                {'last_join_attempt_at': datetime.now().isoformat()}
            )
            
            if not success:
                # Кто-то уже начал join
                return
            
            print(f"[Coordinator] Joining group {group_id} ({group['invite_link']})")
            
            # Выполнение join
            join_target, join_kind, parse_error = self._normalize_join_target(group.get("invite_link") or "")
            _dbg(
                "H3",
                "private_group_coordinator.py:_perform_join:parsed",
                "Parsed join target",
                {
                    "group_id": group_id,
                    "kind": join_kind,
                    "parse_error": bool(parse_error),
                    "join_target_len": len(join_target) if join_target else 0,
                    "join_target_preview": (join_target[:4] + "...") if join_target else "",
                },
            )
            if not join_target or parse_error:
                # Некорректная ссылка — DISABLED
                db.transition_private_group_state(
                    group_id,
                    'JOINING',
                    'DISABLED',
                    {'is_active': 0, 'last_error': f'Invalid link: {parse_error}'}
                )
                return

            # Join через Pyrogram
            if join_kind == "private":
                try:
                    chat = await client.join_chat(join_target)
                except UsernameNotOccupied:
                    # Резерв: извлечение hash из URL
                    path = urlparse(join_target).path.strip("/")
                    if path.startswith("+"):
                        invite_hash = path[1:]
                    elif path.startswith("joinchat/"):
                        invite_hash = path.split("joinchat/", 1)[1]
                    else:
                        raise
                    chat = await client.join_chat(invite_hash)
            else:
                chat = await client.join_chat(join_target)
            _dbg("H3", "private_group_coordinator.py:_perform_join:joined", "join_chat succeeded", {"group_id": group_id, "chat_id": int(getattr(chat, 'id', 0) or 0)})
            
            # Успех: JOINING → JOINED
            db.transition_private_group_state(
                group_id,
                'JOINING',
                'JOINED',
                {
                    'chat_id': chat.id,
                    'title': chat.title or '',
                    'retry_count': 0,
                    'next_retry_at': None
                }
            )
            
            db.reset_private_group_errors(group_id)
            print(f"[Coordinator] Successfully joined group {group_id}: {chat.title} (chat_id={chat.id})")
            
        except UserAlreadyParticipant:
            # Уже участник — это success-case. Пытаемся получить chat_id корректно:
            # - если chat_id уже сохранен: get_chat(chat_id)
            # - если это публичный username: get_chat(username)
            print(f"[Coordinator] Already participant in group {group_id}, resolving chat info")
            
            # Читаем актуальное состояние (может быть обновлено)
            fresh_group = db.get_private_group_by_id(group_id)
            if not fresh_group:
                return
            
            chat_id = fresh_group.get('chat_id')
            
            try:
                if chat_id:
                    # Используем сохранённый chat_id
                    chat = await client.get_chat(chat_id)
                else:
                    # Если это публичная ссылка/username — можем получить chat по username
                    join_target, join_kind, parse_error = self._normalize_join_target(fresh_group.get("invite_link") or "")
                    if join_target and join_kind == "public":
                        chat = await client.get_chat(join_target)
                    else:
                        # Для приватных инвайтов без сохранённого chat_id получить чат без join невозможно.
                        # Оставляем JOINED без chat_id (админ может заполнить chat_id вручную).
                        print(f"[Coordinator] Group {group_id}: chat_id unknown for private invite; marking JOINED")
                        chat = None
                
                if chat:
                    db.transition_private_group_state(
                        group_id,
                        'JOINING',
                        'JOINED',
                        {
                            'chat_id': chat.id,
                            'title': chat.title or '',
                            'retry_count': 0,
                            'next_retry_at': None
                        }
                    )
                else:
                    # Переводим в JOINED без chat_id (проверим позже)
                    db.transition_private_group_state(
                        group_id,
                        'JOINING',
                        'JOINED',
                        {
                            'retry_count': 0,
                            'next_retry_at': None,
                            'last_error': 'Already participant but chat_id is unknown'
                        }
                    )
                    
            except Exception as e:
                print(f"[Coordinator] Error getting chat info for group {group_id}: {e}")
                # Всё равно переводим в JOINED, проверим позже
                db.transition_private_group_state(
                    group_id,
                    'JOINING',
                    'JOINED',
                    {
                        'retry_count': 0,
                        'last_error': f'Already participant, get_chat failed: {e}'
                    }
                )
                
        except FloodWait as e:
            # FloodWait: повтор через указанное время
            print(f"[Coordinator] FloodWait {e.value}s for group {group_id}")
            
            # Чтение актуального состояния
            fresh_group = db.get_private_group_by_id(group_id)
            if not fresh_group:
                return
            
            retry_count = fresh_group.get('retry_count', 0) + 1
            next_retry = datetime.now() + timedelta(seconds=e.value + 10)
            
            db.transition_private_group_state(
                group_id,
                'JOINING',
                'JOIN_QUEUED',
                {
                    'retry_count': retry_count,
                    'next_retry_at': next_retry.isoformat(),
                    'last_error': f'FloodWait {e.value}s'
                }
            )
            
        except (InviteHashInvalid, InviteHashExpired) as e:
            # Невалидный/истекший инвайт — DISABLED
            print(f"[Coordinator] Invalid invite for group {group_id}: {e}")
            db.transition_private_group_state(
                group_id,
                'JOINING',
                'DISABLED',
                {
                    'is_active': 0,
                    'last_error': f'Invalid/expired invite: {e}'
                }
            )
        except UsernameNotOccupied as e:
            # Retry через backoff (username мог измениться)
            print(f"[Coordinator] Username not occupied for group {group_id}: {e}")
            self._handle_join_error(group_id, f"Username not occupied: {e}", retry=True)
        except PeerIdInvalid as e:
            # Невалидный target — DISABLED
            print(f"[Coordinator] Invalid peer for group {group_id}: {e}")
            db.transition_private_group_state(
                group_id,
                'JOINING',
                'DISABLED',
                {'is_active': 0, 'last_error': f'Invalid peer: {e}'}
            )
            
        except Exception as e:
            # Другая ошибка: retry с backoff
            print(f"[Coordinator] Error joining group {group_id}: {e}")
            self._handle_join_error(group_id, str(e), retry=True)
            
        finally:
            # Удаление из активных
            self.active_join_tasks.discard(group_id)

    def _handle_join_error(self, group_id: int, error_msg: str, retry: bool = True):
        """Обработка ошибки join"""
        # Чтение актуального состояния
        group = db.get_private_group_by_id(group_id)
        if not group:
            return
        
        retry_count = group.get('retry_count', 0) + 1
        
        if retry and retry_count < group.get('max_retries', 5):
            # Retry с экспоненциальной задержкой
            delay_minutes = min(2 ** retry_count, 60)
            next_retry = datetime.now() + timedelta(minutes=delay_minutes)
            
            db.transition_private_group_state(
                group_id,
                'JOINING',
                'JOIN_QUEUED',
                {
                    'retry_count': retry_count,
                    'next_retry_at': next_retry.isoformat(),
                    'last_error': error_msg
                }
            )
        else:
            # Превышен лимит повторов — DISABLED
            db.transition_private_group_state(
                group_id,
                'JOINING',
                'DISABLED',
                {
                    'is_active': 0,
                    'last_error': f'Max retries exceeded: {error_msg}'
                }
            )

    async def _process_joined_groups(self):
        """JOINED → ACTIVE: проверить что группа действительно доступна"""
        groups = db.get_private_groups_by_state('JOINED')
        
        for group in groups:
            # Проверяем доступ к чату
            client = self.clients.get(group['assigned_session_name'])
            if not client:
                continue
            
            chat_id = group.get('chat_id')
            if not chat_id:
                # JOINED без chat_id — ошибка
                error_count = db.increment_private_group_error(group['id'], "JOINED without chat_id")
                print(f"[Coordinator] Group {group['id']}: JOINED without chat_id (errors={error_count})")
                if error_count >= 3:
                    db.transition_private_group_state(
                        group['id'],
                        'JOINED',
                        'DISABLED',
                        {'is_active': 0, 'last_error': 'chat_id unresolved'}
                    )
                continue
            
            try:
                chat = await client.get_chat(chat_id)
                
                # Успех: переводим в ACTIVE
                db.transition_private_group_state(
                    group['id'],
                    'JOINED',
                    'ACTIVE',
                    {
                        'title': chat.title or group.get('title', ''),
                        'last_checked_at': datetime.now().isoformat()
                    }
                )
                
                db.reset_private_group_errors(group['id'])
                print(f"[Coordinator] Group {group['id']} is now ACTIVE")
                
            except (ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UsernameNotOccupied) as e:
                # Критические ошибки доступа — LOST_ACCESS
                print(f"[Coordinator] Access error for group {group['id']}: {e}")
                error_count = db.increment_private_group_error(group['id'], str(e))
                
                if error_count >= group.get('max_consecutive_errors', 3):
                    db.transition_private_group_state(
                        group['id'],
                        'JOINED',
                        'LOST_ACCESS',
                        {'last_checked_at': datetime.now().isoformat()}
                    )
                    
            except FloodWait as e:
                # FloodWait — игнорируем
                print(f"[Coordinator] FloodWait {e.value}s when checking group {group['id']}")
                
            except Exception as e:
                # Временные ошибки — игнорируем
                print(f"[Coordinator] Temporary error checking group {group['id']}: {e}")

    async def _process_active_groups(self):
        """Проверка доступа к ACTIVE группам"""
        groups = db.get_private_groups_by_state('ACTIVE')
        
        for group in groups:
            # Проверка раз в N минут
            last_checked = group.get('last_checked_at')
            if last_checked:
                try:
                    last_checked_dt = datetime.fromisoformat(last_checked)
                    if datetime.now() - last_checked_dt < timedelta(minutes=config.PRIVATE_GROUP_CHECK_INTERVAL_MINUTES):
                        continue
                except:
                    pass
            
            # Проверка доступа
            client = self.clients.get(group['assigned_session_name'])
            if not client:
                continue
            
            chat_id = group.get('chat_id')
            if not chat_id:
                continue
            
            try:
                await client.get_chat(chat_id)
                
                # Успех: обновление last_checked_at
                db.update_private_group(group['id'], {'last_checked_at': datetime.now().isoformat()})
                db.reset_private_group_errors(group['id'])
                
            except (ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UsernameNotOccupied) as e:
                # Критические ошибки доступа
                print(f"[Coordinator] Lost access to group {group['id']}: {e}")
                error_count = db.increment_private_group_error(group['id'], str(e))
                
                # Несколько ошибок подряд — LOST_ACCESS
                if error_count >= group.get('max_consecutive_errors', 3):
                    db.transition_private_group_state(
                        group['id'],
                        'ACTIVE',
                        'LOST_ACCESS',
                        {'last_checked_at': datetime.now().isoformat()}
                    )
                    self.lost_access_retry_counts[group['id']] = 0
                    
            except FloodWait as e:
                # FloodWait — игнорируем
                print(f"[Coordinator] FloodWait {e.value}s when checking group {group['id']}")
                
            except Exception as e:
                # Временные ошибки — игнорируем
                print(f"[Coordinator] Temporary error checking group {group['id']}: {e}")

    async def _process_lost_access_groups(self):
        """Восстановление доступа или DISABLED"""
        groups = db.get_private_groups_by_state('LOST_ACCESS')
        
        for group in groups:
            group_id = group['id']
            
            # Счетчик повторов
            retry_count = self.lost_access_retry_counts.get(group_id, 0)
            
            # Проверка лимита попыток
            if retry_count >= config.PRIVATE_GROUP_LOST_ACCESS_MAX_RETRIES:
                # Превышен лимит — DISABLED
                db.transition_private_group_state(
                    group_id,
                    'LOST_ACCESS',
                    'DISABLED',
                    {
                        'is_active': 0,
                        'last_error': f'Access permanently lost after {retry_count} retries'
                    }
                )
                self.lost_access_retry_counts.pop(group_id, None)
                print(f"[Coordinator] Group {group_id} disabled after {retry_count} failed recovery attempts")
                continue
            
            # Попытка восстановления доступа
            client = self.clients.get(group['assigned_session_name'])
            if not client:
                # Нет клиента — увеличиваем счетчик
                self.lost_access_retry_counts[group_id] = retry_count + 1
                continue
            
            chat_id = group.get('chat_id')
            if not chat_id:
                # Нет chat_id — DISABLED
                db.transition_private_group_state(
                    group_id,
                    'LOST_ACCESS',
                    'DISABLED',
                    {
                        'is_active': 0,
                        'last_error': 'No chat_id available'
                    }
                )
                self.lost_access_retry_counts.pop(group_id, None)
                continue
            
            try:
                await client.get_chat(chat_id)
                
                # Доступ восстановлен — ACTIVE
                db.transition_private_group_state(
                    group_id,
                    'LOST_ACCESS',
                    'ACTIVE',
                    {'last_checked_at': datetime.now().isoformat()}
                )
                
                db.reset_private_group_errors(group_id)
                self.lost_access_retry_counts.pop(group_id, None)
                print(f"[Coordinator] Access restored to group {group_id}")
                
            except Exception as e:
                # Доступ все еще потерян — увеличиваем счетчик
                self.lost_access_retry_counts[group_id] = retry_count + 1
                print(f"[Coordinator] Failed to recover group {group_id} (attempt {retry_count + 1}/{config.PRIVATE_GROUP_LOST_ACCESS_MAX_RETRIES}): {e}")

    def _pick_least_loaded_account(self, accounts: list) -> Optional[dict]:
        """Выбрать аккаунт с наименьшей нагрузкой"""
        if not accounts:
            return None
        
        # Считаем нагрузку для каждого аккаунта
        load = []
        for acc in accounts:
            count = db.count_private_groups_by_session(
                acc['session_name'],
                states=['ASSIGNED', 'JOIN_QUEUED', 'JOINING', 'JOINED', 'ACTIVE']
            )
            load.append((count, acc))
        
        # Сортируем по нагрузке
        load.sort(key=lambda x: x[0])
        return load[0][1] if load else None
