# Критические исправления Coordinator

## ✅ Исправление №1: Застрявшие JOINING группы

### Проблема
`join_tasks` хранились только в памяти. После рестарта группы в состоянии `JOINING` застревали навсегда.

### Решение
```python
# Новый метод в БД
def get_private_groups_stuck_in_joining(max_minutes=10):
    """Получить группы застрявшие в JOINING дольше N минут"""
    
# В reconcile добавлен шаг 0
await self._recover_stuck_joining_groups()

# Логика
if last_join_attempt_at + 10 minutes < now:
    JOINING → JOIN_QUEUED (with retry_count++)
```

**Теперь:**
- Группы в `JOINING` автоматически возвращаются в `JOIN_QUEUED` через 10 минут
- Работает даже после рестарта
- Используется exponential backoff

---

## ✅ Исправление №2: get_chat(invite_link) — ОШИБКА

### Проблема
```python
# ❌ НЕПРАВИЛЬНО
chat = await client.get_chat(group['invite_link'])
```

`get_chat()` принимает только `chat_id`, `username` или `peer`, **но не invite-ссылку**.

### Решение
```python
# ✅ ПРАВИЛЬНО
chat_id = group.get('chat_id')
if chat_id:
    chat = await client.get_chat(chat_id)
else:
    # Если chat_id неизвестен — переводим в JOINED без проверки
    # (проверим позже через reconcile)
```

**Теперь:**
- Используем только `chat_id` для проверки доступа
- Если `chat_id` неизвестен при `UserAlreadyParticipant` — переводим в `JOINED` и логируем
- Админ может вручную указать `chat_id` в БД

---

## ✅ Исправление №3: retry_count из snapshot

### Проблема
```python
# ❌ НЕПРАВИЛЬНО
'retry_count': group['retry_count'] + 1  # group — устаревший snapshot
```

Между чтением `group` и обновлением могло пройти время, за которое другой reconcile изменил `retry_count`.

### Решение
```python
# ✅ ПРАВИЛЬНО
# Всегда читаем актуальное состояние перед обновлением
fresh_group = db.get_private_group_by_id(group_id)
retry_count = fresh_group.get('retry_count', 0) + 1

db.transition_private_group_state(
    group_id, 'JOINING', 'JOIN_QUEUED',
    {'retry_count': retry_count, ...}
)
```

**Теперь:**
- Всегда читаем свежее состояние перед инкрементом
- `retry_count` не прыгает и не сбрасывается
- Backoff работает корректно

---

## ✅ Исправление №4: ASSIGNED → JOIN_QUEUED без лимитов

### Проблема
```python
# ❌ НЕПРАВИЛЬНО
groups = db.get_private_groups_by_state('ASSIGNED')
for group in groups:
    # Переводим ВСЕ сразу → flood
```

Массовый перевод в `JOIN_QUEUED` → все группы одновременно пытаются join → Telegram FloodWait.

### Решение
```python
# ✅ ПРАВИЛЬНО
# 1. Контроль одновременных join
PRIVATE_GROUP_MAX_CONCURRENT_JOINS = 3  # config

self.active_join_tasks: Set[int] = set()

def _can_start_new_join():
    return len(self.active_join_tasks) < MAX_CONCURRENT_JOINS

# 2. Join только если есть слот
if not self._can_start_new_join():
    return

# 3. Регистрируем активный join
self.active_join_tasks.add(group_id)
try:
    await self._perform_join(...)
finally:
    self.active_join_tasks.discard(group_id)
```

**Теперь:**
- Максимум 3 одновременных join операции
- Остальные ждут своей очереди
- Снижает риск FloodWait

---

## ✅ Исправление №5: ACTIVE → LOST_ACCESS слишком агрессивный

### Проблема
```python
# ❌ НЕПРАВИЛЬНО
except Exception as e:
    # Любая ошибка → LOST_ACCESS
```

Временные ошибки (network timeout, reconnect) приводили к деактивации.

### Решение
```python
# ✅ ПРАВИЛЬНО
# Фильтруем ошибки
try:
    await client.get_chat(chat_id)
    
except (ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UsernameNotOccupied) as e:
    # Критические ошибки доступа → increment error counter
    error_count = db.increment_private_group_error(group_id, str(e))
    if error_count >= max_consecutive_errors:
        ACTIVE → LOST_ACCESS
        
except FloodWait as e:
    # FloodWait — игнорируем, попробуем позже
    print(f"FloodWait {e.value}s, skipping")
    
except Exception as e:
    # Временные ошибки (network, timeout) — игнорируем
    print(f"Temporary error: {e}")
```

**Теперь:**
- **Критические ошибки** (ChatAdminRequired, ChannelPrivate, etc) → счётчик ошибок
- **FloodWait** → игнорируем, retry позже
- **Временные ошибки** (NetworkError, Timeout) → игнорируем
- Деактивация только после **N последовательных критических ошибок**

---

## ✅ Исправление №6: LOST_ACCESS → DISABLED навсегда

### Проблема
```python
# ❌ НЕПРАВИЛЬНО
except Exception:
    LOST_ACCESS → DISABLED  # Сразу и навсегда
```

Любой временный сбой приводил к окончательной деактивации.

### Решение
```python
# ✅ ПРАВИЛЬНО
# Счётчик попыток восстановления
PRIVATE_GROUP_LOST_ACCESS_MAX_RETRIES = 5  # config

self.lost_access_retry_counts: Dict[int, int] = {}

async def _process_lost_access_groups():
    for group in lost_access_groups:
        retry_count = self.lost_access_retry_counts.get(group_id, 0)
        
        # Проверяем лимит
        if retry_count >= MAX_RETRIES:
            LOST_ACCESS → DISABLED
            return
        
        # Пробуем восстановить
        try:
            await client.get_chat(chat_id)
            # Успех!
            LOST_ACCESS → ACTIVE
            self.lost_access_retry_counts.pop(group_id)
            
        except:
            # Не удалось
            self.lost_access_retry_counts[group_id] = retry_count + 1
```

**Теперь:**
- В `LOST_ACCESS` делается **5 попыток восстановления** (по одной на каждый reconcile loop)
- Если доступ вернулся → `ACTIVE`
- Если после 5 попыток не восстановилось → `DISABLED`
- Мягкая обработка временных сбоев

---

## 📊 Новые настройки config.py

```python
# Таймаут для застрявших JOINING
PRIVATE_GROUP_JOINING_TIMEOUT_MINUTES = 10

# Максимум одновременных join операций (anti-flood)
PRIVATE_GROUP_MAX_CONCURRENT_JOINS = 3

# Попытки восстановления LOST_ACCESS перед DISABLED
PRIVATE_GROUP_LOST_ACCESS_MAX_RETRIES = 5
```

---

## 📝 Новые методы БД

```python
# database/models.py

def get_private_groups_stuck_in_joining(max_minutes: int) -> List[dict]:
    """Получить группы застрявшие в JOINING дольше max_minutes"""
```

---

## 🔄 Обновлённый reconcile flow

```python
async def _reconcile_once():
    # 0. JOINING → JOIN_QUEUED (застрявшие) ✅ НОВОЕ
    await self._recover_stuck_joining_groups()
    
    # 1. NEW → ASSIGNED
    await self._process_new_groups()
    
    # 2. ASSIGNED → JOIN_QUEUED
    await self._process_assigned_groups()
    
    # 3. JOIN_QUEUED → JOINING (с rate limit) ✅ ИСПРАВЛЕНО
    await self._process_join_queued_groups()
    
    # 4. JOINED → ACTIVE (с правильным get_chat) ✅ ИСПРАВЛЕНО
    await self._process_joined_groups()
    
    # 5. ACTIVE → LOST_ACCESS (с фильтрацией ошибок) ✅ ИСПРАВЛЕНО
    await self._process_active_groups()
    
    # 6. LOST_ACCESS → DISABLED (с retry count) ✅ ИСПРАВЛЕНО
    await self._process_lost_access_groups()
```

---

## ✨ Итоговые улучшения

| Проблема | Было | Стало |
|----------|------|-------|
| **Застрявшие JOINING** | Навсегда | Авто-восстановление через 10 мин |
| **get_chat(invite_link)** | Ошибка | Используем chat_id |
| **retry_count snapshot** | Прыгает | Читаем актуальное |
| **Массовый join** | Flood | Лимит 3 одновременно |
| **Любая ошибка → LOST_ACCESS** | Агрессивно | Фильтр критических |
| **LOST_ACCESS → DISABLED** | Сразу | 5 попыток восстановления |

---

## 🚀 Тестирование

### Сценарий 1: Рестарт во время join
```
1. Группа в JOINING
2. Рестарт процесса
3. Coordinator обнаруживает застрявшую группу (>10 мин)
4. Автоматически JOINING → JOIN_QUEUED
5. Повторный join
```

### Сценарий 2: FloodWait
```
1. join вызывает FloodWait 120s
2. Группа переводится JOIN_QUEUED с next_retry_at = now + 120s
3. Через 120s+ join выполняется снова
```

### Сценарий 3: Временный network error
```
1. get_chat выбрасывает NetworkError
2. Ошибка игнорируется (не критическая)
3. Следующий reconcile loop попробует снова
4. Группа остаётся ACTIVE
```

### Сценарий 4: Потеря доступа
```
1. get_chat → ChannelPrivate (3 раза подряд)
2. ACTIVE → LOST_ACCESS
3. 5 попыток восстановления (по одной на reconcile)
4. Если не восстановилось → DISABLED
```

---

## 📚 См. также

- [PRIVATE_GROUPS_ARCHITECTURE.md](./PRIVATE_GROUPS_ARCHITECTURE.md) — Общая архитектура
- [PRIVATE_GROUPS_QUICK_START.md](./PRIVATE_GROUPS_QUICK_START.md) — Быстрый старт
- [CHANGELOG_PRIVATE_GROUPS.md](../CHANGELOG_PRIVATE_GROUPS.md) — История изменений
