"""
Админ-панель для управления системой лидгенерации
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from database.models import Database
from services.userbot_manager import UserbotManager
from pyrogram import Client
from pyrogram.errors import PhoneCodeInvalid, PhoneCodeExpired, SessionPasswordNeeded
import os
import re
from urllib.parse import urlparse
import config

router = Router()
db = Database()
userbot_manager: UserbotManager = None

# ===== Константы UI приватных групп =====
PRIVATE_GROUPS_PAGE_SIZE = 8


async def _safe_callback_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    """Safely answer callback query (ignore expired/invalid query errors)."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        # Игнорируем устаревшие запросы
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            return
        return
    except Exception:
        return


async def _safe_edit_text(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = None):
    """Safely edit message text (ignore 'message is not modified')."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        raise

def _pg_state_emoji(state: str) -> str:
    return {
        'NEW': '🆕',
        'ASSIGNED': '📌',
        'JOIN_QUEUED': '⏳',
        'JOINING': '🔄',
        'JOINED': '✅',
        'ACTIVE': '🟢',
        'LOST_ACCESS': '⚠️',
        'DISABLED': '❌',
    }.get(state or "", "•")

def _pg_filter_groups(groups: list[dict], flt: str) -> list[dict]:
    flt = (flt or "all").lower()
    if flt == "active":
        return [g for g in groups if g.get("is_active") and g.get("state") == "ACTIVE"]
    if flt == "issues":
        return [
            g for g in groups
            if (not g.get("is_active")) or g.get("state") in {"DISABLED", "LOST_ACCESS", "JOINING", "JOIN_QUEUED"}
        ]
    return groups

def _pg_paginate(items: list[dict], page: int, page_size: int) -> tuple[list[dict], int, int]:
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    end = start + page_size
    return items[start:end], page, pages

def _pg_try_parse_list_cb(data: str) -> tuple[int, str] | None:
    # pg_list:<page>:<filter>
    if not data.startswith("pg_list:"):
        return None
    parts = data.split(":")
    if len(parts) < 3:
        return None
    try:
        page = int(parts[1])
    except Exception:
        return None
    flt = parts[2] or "all"
    return page, flt

def _pg_try_parse_view_cb(data: str) -> tuple[int, int, str] | None:
    # pg_view:<id>:<page>:<filter>
    if not data.startswith("pg_view:"):
        return None
    parts = data.split(":")
    if len(parts) < 4:
        return None
    try:
        gid = int(parts[1])
        page = int(parts[2])
    except Exception:
        return None
    flt = parts[3] or "all"
    return gid, page, flt

def _pg_try_parse_del_cb(data: str) -> tuple[str, int, int, str] | None:
    # pg_del_confirm:<id>:<page>:<filter>
    # pg_del:<id>:<page>:<filter>
    for prefix in ("pg_del_confirm:", "pg_del:"):
        if data.startswith(prefix):
            parts = data.split(":")
            if len(parts) < 4:
                return None
            try:
                gid = int(parts[1])
                page = int(parts[2])
            except Exception:
                return None
            flt = parts[3] or "all"
            action = "confirm" if prefix == "pg_del_confirm:" else "delete"
            return action, gid, page, flt
    return None

def _render_private_groups_list(page: int = 0, flt: str = "all", group_type: str = "private") -> tuple[str, InlineKeyboardMarkup]:
    """
    group_type: "private" | "public"
    """
    groups = db.get_all_private_groups()
    
    # Фильтруем по типу группы (приватная/публичная)
    if group_type == "private":
        groups = [g for g in groups if _is_private_invite_link(g.get("invite_link", ""))]
        header = "🔒 <b>Приватные группы</b>"
    else:
        groups = [g for g in groups if not _is_private_invite_link(g.get("invite_link", "")) and _is_public_target(g.get("invite_link", ""))]
        header = "🌐 <b>Публичные группы</b>"
    
    filtered = _pg_filter_groups(groups, flt)
    page_items, page, pages = _pg_paginate(filtered, page, PRIVATE_GROUPS_PAGE_SIZE)

    total = len(groups)
    active_cnt = len([g for g in groups if g.get("is_active") and g.get("state") == "ACTIVE"])
    issues_cnt = len(_pg_filter_groups(groups, "issues"))
    
    text = (
        f"{header}\n\n"
        f"Всего: <b>{total}</b> | ACTIVE: <b>{active_cnt}</b> | Проблемные: <b>{issues_cnt}</b>\n\n"
    )

    if not page_items:
        text += "Нет групп для отображения."
    else:
        for g in page_items:
            gid = g.get("id")
            state = g.get("state", "UNKNOWN")
            emoji = _pg_state_emoji(state)
            title = (g.get("title") or "Без названия").strip()
            assigned = g.get("assigned_session_name") or "—"
            chat_id = g.get("chat_id") or "—"
            is_active = "✅" if g.get("is_active") else "❌"
            text += (
                f"{emoji} {is_active} <b>{title}</b>\n"
                f"  • ID: <code>{gid}</code> | <code>{state}</code>\n"
                f"  • chat_id: <code>{chat_id}</code>\n"
                f"  • account: {assigned}\n"
            )
            if g.get("last_error"):
                text += f"  • err: {str(g.get('last_error'))[:60]}\n"
            text += "\n"

    keyboard: list[list[InlineKeyboardButton]] = []
    # Кнопка добавления в зависимости от типа группы
    if group_type == "private":
        keyboard.append([
            InlineKeyboardButton(text="➕ Добавить", callback_data="private_group_add_private"),
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(text="➕ Добавить", callback_data="private_group_add_public"),
        ])
    
    # Кнопки групп для просмотра деталей и удаления
    # Используем разные префиксы для приватных и публичных групп
    view_prefix = "pg_view:" if group_type == "private" else "pub_view:"
    for g in page_items:
        gid = g.get("id")
        title = g.get("title") or g.get("invite_link") or f"ID={gid}"
        state = g.get("state", "")
        emoji = _pg_state_emoji(state)
        keyboard.append([
            InlineKeyboardButton(
                text=f"{emoji} {title[:30]}",
                callback_data=f"{view_prefix}{gid}:{page}:{flt}"
            )
        ])

    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)

def _render_private_group_details(group_id: int, page: int, flt: str, confirm_delete: bool = False, group_type: str = "private") -> tuple[str, InlineKeyboardMarkup]:
    g = db.get_private_group_by_id(group_id)
    if not g:
        text = "❌ Группа не найдена (возможно удалена)."
        list_prefix = "pg_list:" if group_type == "private" else "pub_list:"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"{list_prefix}{page}:{flt}")]])
        return text, kb

    state = g.get("state", "UNKNOWN")
    emoji = _pg_state_emoji(state)
    is_active = "✅" if g.get("is_active") else "❌"
    title = (g.get("title") or "Без названия").strip()

    text = (
        f"{emoji} {is_active} <b>{title}</b>\n\n"
        f"ID: <code>{g.get('id')}</code>\n"
        f"State: <code>{state}</code>\n"
        f"chat_id: <code>{g.get('chat_id') or '—'}</code>\n"
        f"account: {g.get('assigned_session_name') or '—'}\n\n"
        f"invite_link:\n<code>{(g.get('invite_link') or '—')[:250]}</code>\n\n"
        f"retry: <code>{g.get('retry_count')}/{g.get('max_retries')}</code>\n"
        f"next_retry_at: <code>{g.get('next_retry_at') or '—'}</code>\n"
        f"last_join_attempt_at: <code>{g.get('last_join_attempt_at') or '—'}</code>\n\n"
        f"errors: <code>{g.get('consecutive_errors')}/{g.get('max_consecutive_errors')}</code>\n"
        f"last_error: <code>{(g.get('last_error') or '—')[:250]}</code>\n\n"
        f"last_checked_at: <code>{g.get('last_checked_at') or '—'}</code>\n"
        f"created_at: <code>{g.get('created_at') or '—'}</code>\n"
        f"updated_at: <code>{g.get('updated_at') or '—'}</code>\n"
    )

    kb: list[list[InlineKeyboardButton]] = []
    # Только удалить и назад
    del_prefix = "pg_del_confirm:" if group_type == "private" else "pub_del_confirm:"
    view_prefix = "pg_view:" if group_type == "private" else "pub_view:"
    list_prefix = "pg_list:" if group_type == "private" else "pub_list:"
    kb.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{del_prefix}{group_id}:{page}:{flt}"),
    ])
    kb.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"{list_prefix}{page}:{flt}"),
    ])
    if confirm_delete:
        text = "⚠️ <b>Подтвердите удаление</b>\n\n" + text
        del_exec_prefix = "pg_del:" if group_type == "private" else "pub_del:"
        kb.append([
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"{del_exec_prefix}{group_id}:{page}:{flt}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"{view_prefix}{group_id}:{page}:{flt}"),
        ])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


def set_userbot_manager(manager: UserbotManager):
    """Установить менеджер userbot'ов"""
    global userbot_manager
    userbot_manager = manager


# ========== СОСТОЯНИЯ FSM ==========
class AddAccountStates(StatesGroup):
    waiting_for_api_id = State()
    waiting_for_api_hash = State()
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()
    waiting_for_session_file = State()  # Для загрузки готового .session файла
    waiting_for_session_name = State()  # Имя для сессии
    waiting_for_phone_simple = State()  # Упрощенный способ - только телефон


class GlobalAPISettingsStates(StatesGroup):
    waiting_for_api_id = State()
    waiting_for_api_hash = State()


class AddPrivateGroupStates(StatesGroup):
    waiting_for_private_invite_link = State()
    waiting_for_public_link = State()

class DeletePrivateGroupStates(StatesGroup):
    waiting_for_delete_id = State()


class AddKeywordsStates(StatesGroup):
    waiting_for_keywords = State()


class AddStopwordsStates(StatesGroup):
    waiting_for_stopwords = State()


class UpdateTemplateStates(StatesGroup):
    waiting_for_template = State()

class ManagersChannelStates(StatesGroup):
    waiting_for_channel_id = State()


class CategoryStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_session_name = State()
    waiting_for_managers_channel_id = State()
    waiting_for_channels = State()
    waiting_for_keywords = State()
    waiting_for_stopwords = State()


# ========== ГЛАВНОЕ МЕНЮ ==========
def get_main_menu(user_id: int = None) -> InlineKeyboardMarkup:
    """Главное меню админ-панели - список категорий"""
    is_admin_user = user_id and db.is_admin(user_id)
    
    if is_admin_user:
        # Админ видит все категории
        categories = db.get_all_categories()
    else:
        # Менеджер видит только свою категорию
        if user_id:
            manager_category_id = db.get_manager_category(user_id)
            if manager_category_id:
                category = db.get_category(manager_category_id)
                categories = [category] if category else []
            else:
                categories = []
        else:
            categories = []
    
    keyboard = []
    
    # Кнопка добавления категории только для админа
    if is_admin_user:
        keyboard.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data="category_add")])
    
    # Список категорий
    if categories:
        for cat in categories:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"📁 {cat['name']}",
                    callback_data=f"category_menu_{cat['id']}"
                )
            ])
    
    # Дополнительные разделы только для админа
    if is_admin_user:
        keyboard.append([InlineKeyboardButton(text="👥 Аккаунты", callback_data="admin_accounts")])
        keyboard.append([InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")])
    else:
        # Для менеджера только статистика его категории
        if user_id:
            manager_category_id = db.get_manager_category(user_id)
            if manager_category_id:
                keyboard.append([InlineKeyboardButton(text="📊 Статистика", callback_data=f"category_stats_{manager_category_id}")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_category_menu(category_id: int, user_id: int = None) -> InlineKeyboardMarkup:
    """Меню категории с настройками"""
    category = db.get_category(category_id)
    if not category:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]])
    
    is_admin_user = user_id and db.is_admin(user_id)
    
    # Проверяем настройки категории
    userbots = db.get_category_userbots(category_id)
    has_userbot = len(userbots) > 0
    has_channel = bool(category.get('managers_channel_id'))
    groups = db.get_private_groups_by_category(category_id)
    keywords = db.get_category_keywords(category_id)
    stopwords = db.get_category_stopwords(category_id)
    
    keyboard = [
        [InlineKeyboardButton(text="🔒 Приватные группы", callback_data=f"cat_private_groups_{category_id}")],
        [InlineKeyboardButton(text="🌐 Публичные группы", callback_data=f"cat_public_groups_{category_id}")],
        [InlineKeyboardButton(text=f"{'✅' if has_userbot else '❌'} Userbot'ы ({len(userbots)})", callback_data=f"cat_userbot_{category_id}")],
        [InlineKeyboardButton(text=f"{'✅' if has_channel else '❌'} Канал менеджеров", callback_data=f"cat_managers_channel_{category_id}")],
        [InlineKeyboardButton(text=f"🔑 Ключевые слова ({len(keywords)})", callback_data=f"cat_keywords_{category_id}")],
        [InlineKeyboardButton(text=f"🛑 Стоп-слова ({len(stopwords)})", callback_data=f"cat_stopwords_{category_id}")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"category_stats_{category_id}")],
    ]
    
    # Кнопки редактирования/удаления только для админа
    if is_admin_user:
        keyboard.append([InlineKeyboardButton(text="✏️ Редактировать категорию", callback_data=f"category_edit_{category_id}")])
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить категорию", callback_data=f"category_delete_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.message(Command("admin251219750"))
async def cmd_admin(message: Message):
    """Команда входа в админ-панель"""
    user_id = message.from_user.id

    # Проверяем пароль (только при первом входе)
    if not db.is_admin(user_id):
        # Сохраняем как админа
        db.add_admin(user_id)
        await message.answer("✅ Вы добавлены как администратор!")
    else:
        await message.answer("✅ Добро пожаловать в админ-панель!")

    categories = db.get_all_categories()
    if not categories:
        await message.answer(
            "📁 <b>Категории</b>\n\n"
            "У вас пока нет категорий. Создайте первую категорию для начала работы!",
            reply_markup=get_main_menu(user_id),
            parse_mode="HTML"
        )
    else:
        await message.answer("Выберите категорию для настройки:", reply_markup=get_main_menu(user_id))


@router.message(F.text.startswith("/") & ~F.text.startswith("/admin251219750"))
async def handle_category_command(message: Message):
    """Обработчик динамических команд категорий (например /машины, /материалы)"""
    user_id = message.from_user.id
    command = message.text[1:].strip().lower()  # Убираем "/" и приводим к нижнему регистру
    
    # Ищем категорию по команде
    category = db.get_category_by_command(command)
    if not category:
        # Неизвестная команда - игнорируем
        return
    
    category_id = category['id']
    
    # Оптимизация: проверяем админа один раз
    is_admin_user = db.is_admin(user_id)
    
    # Если пользователь не админ, проверяем и добавляем как менеджера если нужно
    if not is_admin_user:
        manager_categories = db.get_manager_categories(user_id)
        if category_id not in manager_categories:
            db.add_manager(user_id, category_id)
            # Обновляем список после добавления
            manager_categories = db.get_manager_categories(user_id)
        
        # Проверяем права доступа
        if category_id not in manager_categories:
            await message.answer("❌ У вас нет доступа к этой категории.")
            return
    
    # Показываем меню категории (оптимизированный запрос - вся информация одним запросом)
    category_info = db.get_category_full_info(category_id)
    if not category_info:
        await message.answer("❌ Категория не найдена.")
        return
    
    userbots = category_info.get('userbots', [])
    groups_count = category_info.get('groups_count', 0)
    keywords_count = category_info.get('keywords_count', 0)
    stopwords_count = category_info.get('stopwords_count', 0)
    
    text = f"📁 <b>{category_info['name']}</b>\n\n"
    text += f"Userbot'ы: {', '.join(userbots) if userbots else 'Не назначены'}\n"
    text += f"Канал менеджеров: <code>{category_info.get('managers_channel_id') or 'Не настроен'}</code>\n\n"
    text += f"📊 Статистика:\n"
    text += f"• Групп: {groups_count}\n"
    text += f"• Ключевых слов: {keywords_count}\n"
    text += f"• Стоп-слов: {stopwords_count}\n"
    
    await message.answer(text, reply_markup=get_category_menu(category_id, user_id), parse_mode="HTML")


# ========== СТАТИСТИКА ==========
@router.callback_query(F.data == "admin_stats")
async def show_stats(callback: CallbackQuery):
    """Показать общую статистику (только для админа)"""
    user_id = callback.from_user.id
    
    if not db.is_admin(user_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    # Общая статистика по всем категориям
    all_stats = db.get_all_categories_stats()
    
    text = f"""📊 <b>Общая статистика:</b>

Всего категорий: {all_stats['total_categories']}
Всего лидов: {all_stats['total_leads']}
За сегодня: {all_stats['today_leads']}
За 7 дней: {all_stats['week_leads']}
За месяц: {all_stats['month_leads']}

<b>Статистика по категориям:</b>
"""
    
    for item in all_stats['categories']:
        cat = item['category']
        stats = item['stats']
        text += f"\n📁 <b>{cat['name']}</b>\n"
        text += f"  • Лидов: {stats['total_leads']} (сегодня: {stats['today_leads']})\n"
        text += f"  • Групп: {stats['total_groups']} (активных: {stats['active_groups']})\n"
        text += f"  • Userbot'ов: {stats['userbots_count']}\n"
    
    keyboard = [[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]]
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("category_stats_"))
async def show_category_stats(callback: CallbackQuery):
    """Показать статистику категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not db.can_access_category(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    stats = db.get_category_stats(category_id)
    
    text = f"""📊 <b>Статистика: {category['name']}</b>

<b>Лиды:</b>
• Всего: {stats['total_leads']}
• За сегодня: {stats['today_leads']}
• За 7 дней: {stats['week_leads']}
• За месяц: {stats['month_leads']}

<b>Группы:</b>
• Всего: {stats['total_groups']}
• Активных: {stats['active_groups']}

<b>Настройки:</b>
• Userbot'ов: {stats['userbots_count']}
• Ключевых слов: {stats['keywords_count']}
• Стоп-слов: {stats['stopwords_count']}
"""
    
    keyboard = [[InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")]]
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


# ========== ПРИВАТНЫЕ ГРУППЫ (STATE MACHINE) ==========
@router.callback_query(F.data == "admin_private_groups")
async def show_private_groups(callback: CallbackQuery):
    """Показать список приватных групп (страница 1, фильтр all)"""
    text, kb = _render_private_groups_list(page=0, flt="all", group_type="private")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_public_groups")
async def show_public_groups(callback: CallbackQuery):
    """Показать список публичных групп (страница 1, фильтр all)"""
    text, kb = _render_private_groups_list(page=0, flt="all", group_type="public")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pg_list:"))
async def private_groups_list_page(callback: CallbackQuery):
    parsed = _pg_try_parse_list_cb(callback.data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    page, flt = parsed
    text, kb = _render_private_groups_list(page=page, flt=flt, group_type="private")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pub_list:"))
async def public_groups_list_page(callback: CallbackQuery):
    """Обработчик для списка публичных групп"""
    # Используем тот же парсер, что и для приватных
    data = callback.data.replace("pub_list:", "pg_list:")
    parsed = _pg_try_parse_list_cb(data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    page, flt = parsed
    text, kb = _render_private_groups_list(page=page, flt=flt, group_type="public")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pg_view:"))
async def private_group_view(callback: CallbackQuery):
    parsed = _pg_try_parse_view_cb(callback.data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    gid, page, flt = parsed
    text, kb = _render_private_group_details(group_id=gid, page=page, flt=flt, confirm_delete=False, group_type="private")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pub_view:"))
async def public_group_view(callback: CallbackQuery):
    """Обработчик для просмотра публичной группы"""
    data = callback.data.replace("pub_view:", "pg_view:")
    parsed = _pg_try_parse_view_cb(data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    gid, page, flt = parsed
    text, kb = _render_private_group_details(group_id=gid, page=page, flt=flt, confirm_delete=False, group_type="public")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pg_del_confirm:"))
async def private_group_delete_confirm(callback: CallbackQuery):
    parsed = _pg_try_parse_del_cb(callback.data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    action, gid, page, flt = parsed
    if action != "confirm":
        await _safe_callback_answer(callback)
        return
    text, kb = _render_private_group_details(group_id=gid, page=page, flt=flt, confirm_delete=True, group_type="private")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pub_del_confirm:"))
async def public_group_delete_confirm(callback: CallbackQuery):
    """Обработчик подтверждения удаления публичной группы"""
    data = callback.data.replace("pub_del_confirm:", "pg_del_confirm:")
    parsed = _pg_try_parse_del_cb(data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    action, gid, page, flt = parsed
    if action != "confirm":
        await _safe_callback_answer(callback)
        return
    text, kb = _render_private_group_details(group_id=gid, page=page, flt=flt, confirm_delete=True, group_type="public")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pg_del:"))
async def private_group_delete_execute(callback: CallbackQuery):
    parsed = _pg_try_parse_del_cb(callback.data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    action, gid, page, flt = parsed
    if action != "delete":
        await _safe_callback_answer(callback)
        return

    deleted = db.delete_private_group(gid)
    await _safe_callback_answer(callback, "🗑 Удалено" if deleted else "⚠️ Уже удалено", show_alert=True)
    text, kb = _render_private_groups_list(page=page, flt=flt, group_type="private")
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pub_del:"))
async def public_group_delete_execute(callback: CallbackQuery):
    """Обработчик удаления публичной группы"""
    data = callback.data.replace("pub_del:", "pg_del:")
    parsed = _pg_try_parse_del_cb(data)
    if not parsed:
        await _safe_callback_answer(callback, "Некорректная команда", show_alert=True)
        return
    action, gid, page, flt = parsed
    if action != "delete":
        await _safe_callback_answer(callback)
        return

    deleted = db.delete_private_group(gid)
    await _safe_callback_answer(callback, "🗑 Удалено" if deleted else "⚠️ Уже удалено", show_alert=True)
    text, kb = _render_private_groups_list(page=page, flt=flt, group_type="public")
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


def _is_private_invite_link(text: str) -> bool:
    """Грубая валидация приватных инвайтов: t.me/+HASH, t.me/joinchat/HASH, +HASH"""
    s = (text or "").strip()
    if not s:
        return False
    if s.startswith("+") and len(s) > 1:
        return True
    if s.startswith("http://") or s.startswith("https://"):
        try:
            p = urlparse(s)
        except Exception:
            return False
        host = (p.netloc or "").lower()
        path = (p.path or "").strip("/")
        if host.endswith("t.me") or host.endswith("telegram.me"):
            return path.startswith("+") or path.startswith("joinchat/")
    return False


def _is_public_target(text: str) -> bool:
    """Грубая валидация публичных: @username, username, https://t.me/username"""
    s = (text or "").strip()
    if not s:
        return False
    if s.startswith("@"):
        s = s[1:]
    if s.startswith("http://") or s.startswith("https://"):
        try:
            p = urlparse(s)
        except Exception:
            return False
        host = (p.netloc or "").lower()
        path = (p.path or "").strip("/")
        if not (host.endswith("t.me") or host.endswith("telegram.me")):
            return False
        first = path.split("/", 1)[0]
        if first in {"c", "s", "joinchat", "+"}:
            return False
        s = first
    return re.fullmatch(r"[A-Za-z0-9_]{5,32}", s) is not None


def _render_simple_add_groups_screen(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    """
    kind: 'private' | 'public'
    Shows existing groups of that kind and prompts user to send a link/username.
    Keyboard contains only: Delete, Back.
    """
    groups = db.get_all_private_groups()
    if kind == "private":
        shown = [g for g in groups if _is_private_invite_link(g.get("invite_link", ""))]
        header = "➕ <b>Приватные группы (invite)</b>"
        hint = "Отправьте invite-ссылку: <code>https://t.me/+HASH</code> или <code>https://t.me/joinchat/HASH</code> или <code>+HASH</code>"
    else:
        shown = [g for g in groups if not _is_private_invite_link(g.get("invite_link", "")) and _is_public_target(g.get("invite_link", ""))]
        header = "➕ <b>Публичные группы/каналы</b>"
        hint = "Отправьте: <code>@username</code> или <code>username</code> или <code>https://t.me/username</code>"

    text = f"{header}\n\n{hint}\n\n<b>Текущие группы (последние 10):</b>\n"
    if not shown:
        text += "— пока пусто —\n"
    else:
        for g in shown[:10]:
            gid = g.get("id")
            title = (g.get("title") or "Без названия").strip()
            state = g.get("state") or "UNKNOWN"
            emoji = _pg_state_emoji(state)
            text += f"{emoji} <b>{title}</b> — <code>{gid}</code> (<code>{state}</code>)\n"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"simple_delete_start:{kind}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ]
    )
    return text, kb


@router.callback_query(F.data == "menu_add_private_group")
async def menu_add_private_group(callback: CallbackQuery, state: FSMContext):
    """Main menu: add private invite group (shows list + prompt)"""
    await state.set_state(AddPrivateGroupStates.waiting_for_private_invite_link)
    text, kb = _render_simple_add_groups_screen("private")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "menu_add_public_group")
async def menu_add_public_group(callback: CallbackQuery, state: FSMContext):
    """Main menu: add public group/channel (shows list + prompt)"""
    await state.set_state(AddPrivateGroupStates.waiting_for_public_link)
    text, kb = _render_simple_add_groups_screen("public")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("simple_delete_start:"))
async def simple_delete_start(callback: CallbackQuery, state: FSMContext):
    """Ask for group ID to delete (by kind)"""
    kind = callback.data.split(":", 1)[1] if ":" in callback.data else "private"
    if kind not in {"private", "public"}:
        kind = "private"
    await state.set_state(DeletePrivateGroupStates.waiting_for_delete_id)
    await state.update_data(delete_kind=kind)
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "🗑 <b>Удаление группы</b>\n\nОтправьте <b>ID</b> группы, которую нужно удалить.\n"
        "⚠️ Удаление удаляет запись из БД (без возможности восстановить).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"menu_add_{'private' if kind=='private' else 'public'}_group")]]),
        parse_mode="HTML",
    )


@router.message(DeletePrivateGroupStates.waiting_for_delete_id)
async def simple_delete_process(message: Message, state: FSMContext):
    """Delete group by ID and return to add screen"""
    data = await state.get_data()
    kind = data.get("delete_kind", "private")
    raw = (message.text or "").strip()
    try:
        gid = int(raw)
    except Exception:
        await message.answer("❌ Введите числовой ID (например 12).")
        return

    g = db.get_private_group_by_id(gid)
    if not g:
        await message.answer("⚠️ Группа с таким ID не найдена.")
        return

    link = g.get("invite_link", "")
    is_private = _is_private_invite_link(link)
    if kind == "private" and not is_private:
        await message.answer("⚠️ Это не приватная invite-группа. Удаляйте её из раздела публичных.")
        return
    if kind == "public" and is_private:
        await message.answer("⚠️ Это приватная invite-группа. Удаляйте её из раздела приватных.")
        return

    deleted = db.delete_private_group(gid)
    await message.answer("🗑 Удалено" if deleted else "⚠️ Уже удалено")

    # Возврат к экрану добавления
    await state.clear()
    if kind == "private":
        await state.set_state(AddPrivateGroupStates.waiting_for_private_invite_link)
    else:
        await state.set_state(AddPrivateGroupStates.waiting_for_public_link)

    text, kb = _render_simple_add_groups_screen(kind)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "private_group_add_private")
async def add_private_group_private_start(callback: CallbackQuery, state: FSMContext):
    """Добавить приватную группу по invite-ссылке"""
    await state.set_state(AddPrivateGroupStates.waiting_for_private_invite_link)
    await callback.message.edit_text(
        "🔒 <b>Добавление приватной группы (invite)</b>\n\n"
        "Отправьте invite-ссылку:\n"
        "- `https://t.me/+HASH`\n"
        "- `https://t.me/joinchat/HASH`\n"
        "- `+HASH`\n\n"
        "⚠️ Важно: это именно приватное приглашение (без публичного @username).",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "private_group_add_public")
async def add_private_group_public_start(callback: CallbackQuery, state: FSMContext):
    """Добавить публичную группу/канал по username"""
    await state.set_state(AddPrivateGroupStates.waiting_for_public_link)
    await callback.message.edit_text(
        "🌐 <b>Добавление публичной группы/канала</b>\n\n"
        "Отправьте одно из:\n"
        "- `@username`\n"
        "- `username`\n"
        "- `https://t.me/username`\n\n"
        "⚠️ Не отправляйте `t.me/+...` — это приватные инвайты (используйте кнопку «Приватная»).",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddPrivateGroupStates.waiting_for_private_invite_link)
async def add_private_group_private_process(message: Message, state: FSMContext):
    """Сохранить приватный инвайт в БД"""
    invite_link = (message.text or "").strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if not _is_private_invite_link(invite_link):
        await message.answer("❌ Формат не похож на приватный invite. Отправьте `https://t.me/+HASH` или `https://t.me/joinchat/HASH` или `+HASH`.")
        return

    group_id = db.add_private_group(invite_link, category_id=category_id)
    if not group_id:
        await message.answer("❌ Не удалось добавить (возможно ошибка БД).")
        await state.clear()
        user_id = message.from_user.id
        if category_id:
            await message.answer("Выберите раздел:", reply_markup=get_category_menu(category_id, user_id))
        else:
            await message.answer("Выберите раздел:", reply_markup=get_main_menu(user_id))
        return

    await message.answer("✅ Добавлено.")
    
    if category_id:
        # Если это добавление в категорию, возвращаемся в меню категории
        await state.clear()
        user_id = message.from_user.id
        await message.answer("Выберите раздел:", reply_markup=get_category_menu(category_id, user_id))
    else:
        # Старое поведение для обратной совместимости
        await state.set_state(AddPrivateGroupStates.waiting_for_private_invite_link)
        text, kb = _render_simple_add_groups_screen("private")
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(AddPrivateGroupStates.waiting_for_public_link)
async def add_private_group_public_process(message: Message, state: FSMContext):
    """Сохранить публичный username/линк в БД"""
    public_link = (message.text or "").strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if not _is_public_target(public_link):
        await message.answer("❌ Формат не похож на публичный username/ссылку. Пример: `@username` или `https://t.me/username`.")
        return

    group_id = db.add_private_group(public_link, category_id=category_id)
    if not group_id:
        await message.answer("❌ Не удалось добавить (возможно ошибка БД).")
        await state.clear()
        user_id = message.from_user.id
        if category_id:
            await message.answer("Выберите раздел:", reply_markup=get_category_menu(category_id, user_id))
        else:
            await message.answer("Выберите раздел:", reply_markup=get_main_menu(user_id))
        return

    await message.answer("✅ Добавлено.")
    
    if category_id:
        # Если это добавление в категорию, возвращаемся в меню категории
        await state.clear()
        user_id = message.from_user.id
        await message.answer("Выберите раздел:", reply_markup=get_category_menu(category_id, user_id))
    else:
        # Старое поведение для обратной совместимости
        await state.set_state(AddPrivateGroupStates.waiting_for_public_link)
        text, kb = _render_simple_add_groups_screen("public")
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("private_group_reactivate_"))
async def reactivate_private_group(callback: CallbackQuery):
    """Реактивировать приватную группу (DISABLED → NEW)"""
    try:
        group_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    success = db.reactivate_private_group(group_id)
    
    if success:
        await callback.answer("✅ Группа реактивирована (state=NEW). Coordinator запустит процесс заново.", show_alert=True)
    else:
        await callback.answer("❌ Ошибка реактивации", show_alert=True)
    
    await show_private_groups(callback)


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    """Пустой callback (для кнопок-заголовков)"""
    await _safe_callback_answer(callback)


@router.callback_query(F.data.startswith("private_group_delete_"))
async def delete_private_group_legacy(callback: CallbackQuery):
    """Legacy handler: redirect delete to confirm screen."""
    try:
        group_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    # Открытие подтверждения удаления
    text, kb = _render_private_group_details(group_id=group_id, page=0, flt="all", confirm_delete=True, group_type="private")
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=kb, parse_mode="HTML")


# ========== КЛЮЧЕВЫЕ СЛОВА ==========
@router.callback_query(F.data == "admin_keywords")
async def show_keywords(callback: CallbackQuery):
    """Показать меню ключевых слов"""
    keywords = db.get_all_keywords()
    text = f"🔑 Ключевые слова ({len(keywords)}):\n" + "\n".join(f"• {k}" for k in keywords[:10])

    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="keywords_add")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="keywords_delete")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.callback_query(F.data == "keywords_add")
async def add_keywords_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление ключевых слов"""
    await state.set_state(AddKeywordsStates.waiting_for_keywords)
    await callback.message.edit_text("Отправьте ключевые слова через запятую или каждое с новой строки:")
    await callback.answer()


@router.message(AddKeywordsStates.waiting_for_keywords)
async def add_keywords_process(message: Message, state: FSMContext):
    """Обработать добавление ключевых слов"""
    text = message.text.strip()
    words = [w.strip() for w in text.replace('\n', ',').split(',') if w.strip()]
    count = db.add_keywords(words)
    await message.answer(f"✅ Добавлено {count} ключевых слов!")
    await state.clear()
    await message.answer("Выберите раздел:", reply_markup=get_main_menu())


@router.callback_query(F.data == "keywords_delete")
async def delete_keywords_start(callback: CallbackQuery):
    """Начать удаление ключевых слов"""
    keywords = db.get_all_keywords_with_ids()
    if not keywords:
        await callback.answer("Нет ключевых слов", show_alert=True)
        return

    keyboard = []
    for kw in keywords[:20]:  # Ограничиваем 20
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 {kw['word']}",
                callback_data=f"keyword_delete_{kw['id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_keywords")])
    await callback.message.edit_text("Выберите ключевое слово для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.callback_query(F.data.startswith("keyword_delete_"))
async def delete_keyword(callback: CallbackQuery):
    """Удалить ключевое слово"""
    keyword_id = int(callback.data.split("_")[-1])
    db.delete_keywords([keyword_id])
    await callback.answer("Удалено", show_alert=True)
    await show_keywords(callback)


# ========== СТОП-СЛОВА ==========
@router.callback_query(F.data == "admin_stopwords")
async def show_stopwords(callback: CallbackQuery):
    """Показать меню стоп-слов"""
    stopwords = db.get_all_stopwords()
    text = f"🛑 Стоп-слова ({len(stopwords)}):\n" + "\n".join(f"• {s}" for s in stopwords[:10])

    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="stopwords_add")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="stopwords_delete")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.callback_query(F.data == "stopwords_add")
async def add_stopwords_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление стоп-слов"""
    await state.set_state(AddStopwordsStates.waiting_for_stopwords)
    await callback.message.edit_text("Отправьте стоп-слова через запятую или каждое с новой строки:")
    await callback.answer()


@router.message(AddStopwordsStates.waiting_for_stopwords)
async def add_stopwords_process(message: Message, state: FSMContext):
    """Обработать добавление стоп-слов"""
    text = message.text.strip()
    words = [w.strip() for w in text.replace('\n', ',').split(',') if w.strip()]
    count = db.add_stopwords(words)
    await message.answer(f"✅ Добавлено {count} стоп-слов!")
    await state.clear()
    await message.answer("Выберите раздел:", reply_markup=get_main_menu())


@router.callback_query(F.data == "stopwords_delete")
async def delete_stopwords_start(callback: CallbackQuery):
    """Начать удаление стоп-слов"""
    stopwords = db.get_all_stopwords_with_ids()
    if not stopwords:
        await callback.answer("Нет стоп-слов", show_alert=True)
        return

    keyboard = []
    for sw in stopwords[:20]:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 {sw['word']}",
                callback_data=f"stopword_delete_{sw['id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_stopwords")])
    await callback.message.edit_text("Выберите стоп-слово для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.callback_query(F.data.startswith("stopword_delete_"))
async def delete_stopword(callback: CallbackQuery):
    """Удалить стоп-слово"""
    stopword_id = int(callback.data.split("_")[-1])
    db.delete_stopwords([stopword_id])
    await callback.answer("Удалено", show_alert=True)
    await show_stopwords(callback)


# ========== ШАБЛОНЫ ==========
@router.callback_query(F.data == "admin_templates")
async def show_templates(callback: CallbackQuery):
    """Показать меню шаблонов"""
    template = db.get_active_template()
    text = f"💬 <b>Текущий шаблон:</b>\n\n{template}"

    keyboard = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="template_edit")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "template_edit")
async def edit_template_start(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование шаблона"""
    await state.set_state(UpdateTemplateStates.waiting_for_template)
    await callback.message.edit_text("Отправьте новый текст шаблона:")
    await callback.answer()


@router.message(UpdateTemplateStates.waiting_for_template)
async def edit_template_process(message: Message, state: FSMContext):
    """Обработать изменение шаблона"""
    template = message.text
    db.update_template(template)
    await message.answer("✅ Шаблон обновлен!")
    await state.clear()
    await message.answer("Выберите раздел:", reply_markup=get_main_menu())


# ========== АККАУНТЫ ==========
@router.callback_query(F.data == "admin_accounts")
async def show_accounts(callback: CallbackQuery):
    """Показать меню аккаунтов"""
    accounts = db.get_all_accounts()
    text = f"👥 Аккаунты ({len(accounts)}):\n\n"
    for acc in accounts:
        text += f"• {acc['session_name']} ({acc['phone']}) - {acc['status']}\n"

    # Проверяем есть ли глобальные настройки API
    global_api = db.get_global_api_settings()
    has_global_api = global_api and global_api.get('api_id') and global_api.get('api_hash')
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="account_add")],
    ]
    
    if has_global_api:
        keyboard.append([InlineKeyboardButton(text="📱 Добавить по телефону (быстро)", callback_data="account_add_simple")])
    
    keyboard.extend([
        [InlineKeyboardButton(text="📁 Загрузить сессию", callback_data="account_add_session")],
        [InlineKeyboardButton(text="⚙️ Настройки API", callback_data="account_api_settings")],
        [InlineKeyboardButton(text="📋 Список", callback_data="account_list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="account_delete")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.callback_query(F.data == "account_add_simple")
async def add_account_simple_start(callback: CallbackQuery, state: FSMContext):
    """Начать упрощенное добавление аккаунта (только телефон)"""
    global_api = db.get_global_api_settings()
    if not global_api or not global_api.get('api_id') or not global_api.get('api_hash'):
        await callback.answer("❌ Сначала настройте глобальные API credentials в '⚙️ Настройки API'", show_alert=True)
        return
    
    await state.set_state(AddAccountStates.waiting_for_phone_simple)
    await callback.message.edit_text(
        "📱 <b>Упрощенное добавление аккаунта</b>\n\n"
        "Используются глобальные настройки API.\n\n"
        "Отправьте номер телефона (с кодом страны, например: +79991234567):",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddAccountStates.waiting_for_phone_simple)
async def add_account_simple_phone(message: Message, state: FSMContext):
    """Получить телефон для упрощенного добавления"""
    phone = message.text.strip()
    global_api = db.get_global_api_settings()
    
    if not global_api or not global_api.get('api_id') or not global_api.get('api_hash'):
        await message.answer("❌ Глобальные настройки API не найдены. Используйте обычный способ добавления.")
        await state.clear()
        return
    
    api_id = int(global_api['api_id'])
    api_hash = global_api['api_hash']
    
    # Создаем временную сессию для авторизации
    session_name = f"temp_{message.from_user.id}"
    
    try:
        client = Client(
            name=session_name,
            workdir=config.SESSIONS_DIR,
            api_id=api_id,
            api_hash=api_hash
        )
        
        await client.connect()
        sent_code = await client.send_code(phone)
        await state.update_data(
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            session_name=session_name,
            phone_code_hash=sent_code.phone_code_hash
        )
        await state.set_state(AddAccountStates.waiting_for_code)
        await message.answer("✅ Код отправлен!\n\nОтправьте код подтверждения из Telegram:")
        await client.disconnect()
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "account_add")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление аккаунта"""
    # Проверяем есть ли глобальные настройки
    global_api = db.get_global_api_settings()
    has_global_api = global_api and global_api.get('api_id') and global_api.get('api_hash')
    
    if has_global_api:
        await callback.message.edit_text(
            "Выберите способ добавления:\n\n"
            "📱 <b>Быстрый способ:</b> Используйте '📱 Добавить по телефону' - "
            "потребуется только номер телефона и код!\n\n"
            "📝 <b>Полный способ:</b> Продолжить с вводом API_ID/API_HASH для этого аккаунта",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Быстрый (только телефон)", callback_data="account_add_simple")],
                [InlineKeyboardButton(text="📝 Полный (с API_ID/API_HASH)", callback_data="account_add_full")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_accounts")],
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
    else:
        await state.set_state(AddAccountStates.waiting_for_api_id)
        await callback.message.edit_text(
            "Отправьте API_ID:\n\n"
            "💡 <b>Совет:</b> Настройте глобальные API credentials в '⚙️ Настройки API', "
            "чтобы потом добавлять аккаунты только по номеру телефона!",
            parse_mode="HTML"
        )
        await callback.answer()


@router.callback_query(F.data == "account_add_full")
async def add_account_full_start(callback: CallbackQuery, state: FSMContext):
    """Начать полное добавление аккаунта (с API_ID/API_HASH)"""
    await state.set_state(AddAccountStates.waiting_for_api_id)
    await callback.message.edit_text("Отправьте API_ID:")
    await callback.answer()


@router.callback_query(F.data == "account_api_settings")
async def show_api_settings(callback: CallbackQuery):
    """Показать настройки глобальных API credentials"""
    global_api = db.get_global_api_settings()
    
    if global_api and global_api.get('api_id') and global_api.get('api_hash'):
        text = (
            f"⚙️ <b>Глобальные настройки API</b>\n\n"
            f"API_ID: <code>{global_api['api_id']}</code>\n"
            f"API_HASH: <code>{global_api['api_hash'][:20]}...</code>\n\n"
            f"✅ Настроено! Теперь вы можете добавлять аккаунты только по номеру телефона."
        )
        keyboard = [
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="api_settings_edit")],
            [InlineKeyboardButton(text="🗑 Очистить", callback_data="api_settings_clear")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_accounts")],
        ]
    else:
        text = (
            "⚙️ <b>Глобальные настройки API</b>\n\n"
            "Не настроено.\n\n"
            "💡 <b>Зачем это нужно?</b>\n"
            "Если вы укажете API_ID и API_HASH один раз здесь, "
            "то при добавлении аккаунтов вам нужно будет вводить только номер телефона и код подтверждения!"
        )
        keyboard = [
            [InlineKeyboardButton(text="➕ Настроить", callback_data="api_settings_set")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_accounts")],
        ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "api_settings_set")
async def api_settings_set_start(callback: CallbackQuery, state: FSMContext):
    """Начать настройку глобальных API credentials"""
    await state.set_state(GlobalAPISettingsStates.waiting_for_api_id)
    await callback.message.edit_text(
        "⚙️ <b>Настройка глобальных API credentials</b>\n\n"
        "Эти настройки будут использоваться для всех новых аккаунтов.\n\n"
        "Отправьте API_ID:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(GlobalAPISettingsStates.waiting_for_api_id)
async def api_settings_api_id(message: Message, state: FSMContext):
    """Получить API_ID для глобальных настроек"""
    try:
        api_id = int(message.text.strip())
        await state.update_data(api_id=api_id)
        await state.set_state(GlobalAPISettingsStates.waiting_for_api_hash)
        await message.answer("Отправьте API_HASH:")
    except ValueError:
        await message.answer("❌ API_ID должен быть числом. Попробуйте снова:")


@router.message(GlobalAPISettingsStates.waiting_for_api_hash)
async def api_settings_api_hash(message: Message, state: FSMContext):
    """Получить API_HASH для глобальных настроек"""
    api_hash = message.text.strip()
    data = await state.get_data()
    api_id = data['api_id']
    
    db.set_global_api_settings(str(api_id), api_hash)
    
    await message.answer(
        f"✅ Глобальные настройки API сохранены!\n\n"
        f"Теперь при добавлении аккаунтов используйте кнопку "
        f"'📱 Добавить по телефону' - потребуется только номер телефона!"
    )
    await state.clear()
    await message.answer("Выберите раздел:", reply_markup=get_main_menu())


@router.callback_query(F.data == "api_settings_edit")
async def api_settings_edit_start(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование глобальных API credentials"""
    await state.set_state(GlobalAPISettingsStates.waiting_for_api_id)
    await callback.message.edit_text("Отправьте новый API_ID:")
    await callback.answer()


@router.callback_query(F.data == "api_settings_clear")
async def api_settings_clear(callback: CallbackQuery):
    """Очистить глобальные настройки API"""
    db.clear_global_api_settings()
    await callback.answer("✅ Глобальные настройки API очищены", show_alert=True)
    await show_api_settings(callback)


@router.callback_query(F.data == "account_add_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление аккаунта через готовый .session файл"""
    await state.set_state(AddAccountStates.waiting_for_session_name)
    await callback.message.edit_text(
        "📁 <b>Загрузка готовой сессии</b>\n\n"
        "Этот способ позволяет добавить аккаунт БЕЗ API_ID и API_HASH!\n\n"
        "1. Отправьте имя для сессии (например: account_123456789)\n"
        "2. Затем отправьте .session файл\n\n"
        "💡 <b>Где взять .session файл?</b>\n"
        "- Из другой программы на Pyrogram\n"
        "- Из папки sessions/ (если уже есть)\n"
        "- Экспортировать из Telegram Desktop\n\n"
        "Отправьте имя сессии:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddAccountStates.waiting_for_session_name)
async def add_account_session_name(message: Message, state: FSMContext):
    """Получить имя сессии"""
    session_name = message.text.strip()
    
    # Убираем расширение если есть
    if session_name.endswith('.session'):
        session_name = session_name[:-8]
    
    await state.update_data(session_name=session_name)
    await state.set_state(AddAccountStates.waiting_for_session_file)
    await message.answer(
        f"Имя сессии: <b>{session_name}</b>\n\n"
        "Теперь отправьте .session файл.\n\n"
        "💡 <b>Как отправить файл:</b>\n"
        "1. Нажмите на скрепку (📎) в Telegram\n"
        "2. Выберите 'Файл' или 'Документ'\n"
        "3. Выберите ваш .session файл\n"
        "4. Отправьте",
        parse_mode="HTML"
    )


@router.message(AddAccountStates.waiting_for_session_file)
async def add_account_session_file(message: Message, state: FSMContext):
    """Обработать загрузку .session файла"""
    if not message.document:
        await message.answer("❌ Пожалуйста, отправьте файл (не фото). Попробуйте снова:")
        return
    
    file_name = message.document.file_name or ""
    
    # Проверяем что это .session файл
    if not file_name.endswith('.session'):
        await message.answer(
            "❌ Файл должен иметь расширение .session\n\n"
            "Пожалуйста, отправьте правильный файл:"
        )
        return
    
    data = await state.get_data()
    session_name = data.get('session_name', file_name[:-8])  # Убираем .session
    
    try:
        # Скачиваем файл
        session_path = os.path.join(config.SESSIONS_DIR, f"{session_name}.session")
        
        # Создаем папку если нет
        os.makedirs(config.SESSIONS_DIR, exist_ok=True)
        
        # Скачиваем файл (Aiogram 3.x)
        file = await message.bot.get_file(message.document.file_id)
        await message.bot.download_file(file.file_path, destination=session_path)
        
        # Пытаемся подключиться к сессии
        # Для работы Pyrogram нужны API_ID и API_HASH, но если они уже в сессии - можно попробовать без них
        try:
            # Пробуем подключиться без API credentials (если они уже в сессии)
            client = Client(
                name=session_name,
                workdir=config.SESSIONS_DIR
            )
            
            await client.start()
            me = await client.get_me()
            
            # Получаем API credentials из сессии (если возможно)
            # Pyrogram хранит их в сессии, но мы не можем их извлечь напрямую
            # Поэтому сохраняем как пустые, но сессия будет работать
            phone = me.phone_number or f"+{me.id}"  # Используем ID если телефона нет
            
            await client.disconnect()
            
            # Сохраняем в БД (без API credentials, они уже в сессии)
            db.add_account(session_name, phone, "", "", "Active")
            
            # Проверяем есть ли category_id в state (добавление для категории)
            data = await state.get_data()
            category_id = data.get('category_id')
            if category_id:
                # Автоматически добавляем аккаунт в категорию через таблицу связи
                db.add_category_userbot(category_id, session_name)
                if userbot_manager:
                    await userbot_manager.add_client(session_name, phone)
                    await userbot_manager.update_category_for_session(session_name)
                
                category = db.get_category(category_id)
                await message.answer(
                    f"✅ Сессия успешно загружена и назначена категории '{category['name']}'!\n\n"
                    f"Session: <b>{session_name}</b>\n"
                    f"Username: @{me.username or 'N/A'}\n"
                    f"Phone: {phone}\n\n"
                    f"💡 <b>Важно:</b> API credentials уже сохранены в файле сессии.\n"
                    f"Аккаунт готов к работе!",
                    parse_mode="HTML"
                )
                await state.clear()
                user_id = message.from_user.id
                await message.answer("Выберите раздел:", reply_markup=get_category_menu(category_id, user_id))
            else:
                # Обычное добавление (не для категории)
                await message.answer(
                    f"✅ Сессия успешно загружена!\n\n"
                    f"Session: <b>{session_name}</b>\n"
                    f"Username: @{me.username or 'N/A'}\n"
                    f"Phone: {phone}\n\n"
                    f"💡 <b>Важно:</b> API credentials уже сохранены в файле сессии.\n"
                    f"Аккаунт готов к работе!",
                    parse_mode="HTML"
                )
                
                # Перезагружаем аккаунт в менеджере
                if userbot_manager:
                    await userbot_manager.add_client(session_name, phone)
                
                await state.clear()
                await message.answer("Выберите раздел:", reply_markup=get_main_menu())
            
        except Exception as e:
            # Если не получилось подключиться, возможно нужны API credentials
            await message.answer(
                f"⚠️ Не удалось подключиться к сессии автоматически.\n\n"
                f"Возможно, нужны API_ID и API_HASH.\n\n"
                f"Попробуйте:\n"
                f"1. Использовать способ '➕ Добавить аккаунт' с API_ID/API_HASH\n"
                f"2. Или убедитесь, что .session файл валидный и не поврежден\n\n"
                f"Ошибка: {str(e)[:200]}"
            )
            # Удаляем нерабочий файл
            if os.path.exists(session_path):
                os.remove(session_path)
            await state.clear()
            
    except Exception as e:
        await message.answer(f"❌ Ошибка при загрузке файла: {e}")
        await state.clear()


@router.message(AddAccountStates.waiting_for_api_id)
async def add_account_api_id(message: Message, state: FSMContext):
    """Получить API_ID"""
    try:
        api_id = int(message.text.strip())
        await state.update_data(api_id=api_id)
        await state.set_state(AddAccountStates.waiting_for_api_hash)
        await message.answer("Отправьте API_HASH:")
    except ValueError:
        await message.answer("❌ API_ID должен быть числом. Попробуйте снова:")


@router.message(AddAccountStates.waiting_for_api_hash)
async def add_account_api_hash(message: Message, state: FSMContext):
    """Получить API_HASH"""
    api_hash = message.text.strip()
    await state.update_data(api_hash=api_hash)
    await state.set_state(AddAccountStates.waiting_for_phone)
    await message.answer("Отправьте номер телефона (с кодом страны, например: +79991234567):")


@router.message(AddAccountStates.waiting_for_phone)
async def add_account_phone(message: Message, state: FSMContext):
    """Получить телефон и начать авторизацию"""
    phone = message.text.strip()
    data = await state.get_data()
    api_id = data['api_id']
    api_hash = data['api_hash']

    # Создаем временную сессию для авторизации
    session_name = f"temp_{message.from_user.id}"
    session_path = os.path.join(config.SESSIONS_DIR, f"{session_name}.session")

    try:
        client = Client(
            name=session_name,
            workdir=config.SESSIONS_DIR,
            api_id=api_id,
            api_hash=api_hash
        )

        await client.connect()
        sent_code = await client.send_code(phone)
        await state.update_data(
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            session_name=session_name,
            phone_code_hash=sent_code.phone_code_hash
        )
        await state.set_state(AddAccountStates.waiting_for_code)
        await message.answer("Отправьте код подтверждения из Telegram:")
        await client.disconnect()

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(AddAccountStates.waiting_for_code)
async def add_account_code(message: Message, state: FSMContext):
    """Получить код подтверждения"""
    code = message.text.strip()
    data = await state.get_data()
    session_name = data['session_name']
    phone = data['phone']
    api_id = data['api_id']
    api_hash = data['api_hash']
    phone_code_hash = data['phone_code_hash']

    try:
        client = Client(
            name=session_name,
            workdir=config.SESSIONS_DIR,
            api_id=api_id,
            api_hash=api_hash
        )

        await client.connect()
        try:
            await client.sign_in(phone, phone_code_hash, code)
            me = await client.get_me()

            # Переименовываем сессию
            final_session_name = f"account_{me.id}"
            old_path = os.path.join(config.SESSIONS_DIR, f"{session_name}.session")
            new_path = os.path.join(config.SESSIONS_DIR, f"{final_session_name}.session")

            await client.disconnect()

            if os.path.exists(old_path):
                os.rename(old_path, new_path)

            # Сохраняем в БД
            db.add_account(final_session_name, phone, str(api_id), api_hash, "Active")

            # Проверяем есть ли category_id в state (добавление для категории)
            category_id = data.get('category_id')
            if category_id:
                # Автоматически добавляем аккаунт в категорию через таблицу связи
                db.add_category_userbot(category_id, final_session_name)
                if userbot_manager:
                    await userbot_manager.add_client(final_session_name, phone)
                    await userbot_manager.update_category_for_session(final_session_name)
                
                category = db.get_category(category_id)
                await message.answer(
                    f"✅ Аккаунт добавлен и назначен категории '{category['name']}'!\n"
                    f"Session: {final_session_name}\n"
                    f"Username: @{me.username or 'N/A'}\n"
                    f"API credentials сохранены в БД"
                )
                await state.clear()
                user_id = message.from_user.id
                await message.answer("Выберите раздел:", reply_markup=get_category_menu(category_id, user_id))
            else:
                # Обычное добавление (не для категории)
                await message.answer(
                    f"✅ Аккаунт добавлен!\n"
                    f"Session: {final_session_name}\n"
                    f"Username: @{me.username or 'N/A'}\n"
                    f"API credentials сохранены в БД"
                )

                # Перезагружаем аккаунт в менеджере
                if userbot_manager:
                    await userbot_manager.add_client(final_session_name, phone)

                await state.clear()
                await message.answer("Выберите раздел:", reply_markup=get_main_menu())

        except SessionPasswordNeeded:
            await state.set_state(AddAccountStates.waiting_for_password)
            await message.answer("Аккаунт защищен 2FA. Отправьте пароль:")
            await client.disconnect()

    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        await message.answer(f"❌ Неверный или истекший код. Попробуйте снова: {e}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(AddAccountStates.waiting_for_password)
async def add_account_password(message: Message, state: FSMContext):
    """Получить пароль 2FA"""
    password = message.text.strip()
    data = await state.get_data()
    session_name = data['session_name']
    phone = data['phone']
    api_id = data['api_id']
    api_hash = data['api_hash']

    try:
        client = Client(
            name=session_name,
            workdir=config.SESSIONS_DIR,
            api_id=api_id,
            api_hash=api_hash
        )

        await client.connect()
        await client.check_password(password)
        me = await client.get_me()

        # Переименовываем сессию
        final_session_name = f"account_{me.id}"
        old_path = os.path.join(config.SESSIONS_DIR, f"{session_name}.session")
        new_path = os.path.join(config.SESSIONS_DIR, f"{final_session_name}.session")

        await client.disconnect()

        if os.path.exists(old_path):
            os.rename(old_path, new_path)

        # Сохраняем в БД
        db.add_account(final_session_name, phone, str(api_id), api_hash, "Active")

        # Проверяем есть ли category_id в state (добавление для категории)
        category_id = data.get('category_id')
        if category_id:
            # Автоматически добавляем аккаунт в категорию через таблицу связи
            db.add_category_userbot(category_id, final_session_name)
            if userbot_manager:
                await userbot_manager.add_client(final_session_name, phone)
                await userbot_manager.update_category_for_session(final_session_name)
            
            category = db.get_category(category_id)
            await message.answer(
                f"✅ Аккаунт добавлен и назначен категории '{category['name']}'!\n"
                f"Session: {final_session_name}\n"
                f"Username: @{me.username or 'N/A'}\n"
                f"API credentials сохранены в БД"
            )
            await state.clear()
            user_id = message.from_user.id
            await message.answer("Выберите раздел:", reply_markup=get_category_menu(category_id, user_id))
        else:
            # Обычное добавление (не для категории)
            await message.answer(
                f"✅ Аккаунт добавлен!\n"
                f"Session: {final_session_name}\n"
                f"Username: @{me.username or 'N/A'}\n"
                f"API credentials сохранены в БД"
            )

            # Перезагружаем аккаунт в менеджере
            if userbot_manager:
                await userbot_manager.add_client(final_session_name, phone)

            await state.clear()
            await message.answer("Выберите раздел:", reply_markup=get_main_menu())

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "account_list")
async def list_accounts(callback: CallbackQuery):
    """Показать список аккаунтов"""
    accounts = db.get_all_accounts()
    if not accounts:
        await callback.answer("Нет аккаунтов", show_alert=True)
        return

    text = "👥 <b>Список аккаунтов:</b>\n\n"
    for acc in accounts:
        status_emoji = {
            "Active": "✅",
            "Flood": "⏳",
            "Banned": "❌"
        }.get(acc['status'], "❓")
        text += f"{status_emoji} {acc['session_name']} ({acc['phone']}) - {acc['status']}\n"

    keyboard = [[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_accounts")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "account_delete")
async def delete_accounts_start(callback: CallbackQuery):
    """Начать удаление аккаунтов"""
    accounts = db.get_all_accounts()
    if not accounts:
        await callback.answer("Нет аккаунтов", show_alert=True)
        return

    keyboard = []
    for acc in accounts:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 {acc['session_name']}",
                callback_data=f"account_delete_{acc['session_name']}"
            )
        ])

    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_accounts")])
    await callback.message.edit_text("Выберите аккаунт для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.callback_query(F.data.startswith("account_delete_"))
async def delete_account(callback: CallbackQuery):
    """Удалить аккаунт"""
    session_name = callback.data.replace("account_delete_", "")
    db.delete_account(session_name)

    # Удаляем сессию
    session_path = os.path.join(config.SESSIONS_DIR, f"{session_name}.session")
    if os.path.exists(session_path):
        os.remove(session_path)

    # Удаляем из менеджера
    if userbot_manager:
        await userbot_manager.remove_client(session_name)

    await callback.answer("Аккаунт удален", show_alert=True)
    await show_accounts(callback)


# ========== КАНАЛ МЕНЕДЖЕРОВ ==========
@router.callback_query(F.data == "admin_managers_channel")
async def show_managers_channel_settings(callback: CallbackQuery):
    """Показать настройки канала менеджеров"""
    channel_id = db.get_managers_channel_id()
    
    if channel_id:
        text = f"""📢 <b>Канал менеджеров</b>

Текущий канал: <code>{channel_id}</code>

Сообщения от пользователей будут пересылаться в этот канал."""
        keyboard = [
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="managers_channel_set")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="managers_channel_delete")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ]
    else:
        text = """📢 <b>Канал менеджеров</b>

Канал не настроен.

Сообщения от пользователей будут пересылаться в канал, указанный в .env файле (MANAGERS_CHANNEL_ID), или не будут пересылаться, если он не указан.

Для настройки канала через Telegram:
1. Добавьте бота в канал как администратора
2. Отправьте ID канала (можно получить через @userinfobot или @getidsbot)"""
        keyboard = [
            [InlineKeyboardButton(text="➕ Добавить", callback_data="managers_channel_set")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ]
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data == "managers_channel_set")
async def set_managers_channel_start(callback: CallbackQuery, state: FSMContext):
    """Начать настройку канала менеджеров"""
    await state.set_state(ManagersChannelStates.waiting_for_channel_id)
    text = """📢 <b>Настройка канала менеджеров</b>

Отправьте ID канала.

Как получить ID канала:
1. Добавьте бота в канал как администратора
2. Используйте @userinfobot или @getidsbot для получения ID
3. Или перешлите любое сообщение из канала боту @userinfobot

Отправьте ID канала (число, например: -1001234567890):"""
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_managers_channel")]]
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.message(ManagersChannelStates.waiting_for_channel_id)
async def set_managers_channel_process(message: Message, state: FSMContext):
    """Обработать ID канала"""
    data = await state.get_data()
    category_id = data.get('category_id')
    
    try:
        channel_id = int(message.text.strip())
        
        # Проверяем что это валидный ID канала (обычно отрицательное число)
        if channel_id > 0:
            await message.answer("⚠️ ID канала обычно отрицательное число (начинается с -100). Попробуйте еще раз или отправьте ❌ Отмена.")
            return
        
        if category_id:
            # Сохраняем канал для категории
            success = db.update_category(category_id, {'managers_channel_id': channel_id})
            
            if success:
                category = db.get_category(category_id)
                await message.answer(f"✅ Канал менеджеров установлен для категории '{category['name']}': <code>{channel_id}</code>", parse_mode="HTML")
                await state.clear()
                await message.answer(
                    f"📢 <b>Канал менеджеров: {category['name']}</b>\n\n"
                    f"Текущий канал: <code>{channel_id}</code>\n\n"
                    f"Сообщения от пользователей будут пересылаться в этот канал.",
                    reply_markup=get_category_menu(category_id, message.from_user.id),
                    parse_mode="HTML"
                )
            else:
                await message.answer("❌ Ошибка при сохранении канала. Попробуйте еще раз.")
        else:
            # Сохраняем глобальный канал менеджеров
            success = db.set_managers_channel_id(channel_id)
            
            if success:
                await message.answer(f"✅ Канал менеджеров установлен: <code>{channel_id}</code>", parse_mode="HTML")
                await state.clear()
                
                # Показываем обновленные настройки
                text = f"""📢 <b>Канал менеджеров</b>

Текущий канал: <code>{channel_id}</code>

Сообщения от пользователей будут пересылаться в этот канал."""
                keyboard = [
                    [InlineKeyboardButton(text="✏️ Изменить", callback_data="managers_channel_set")],
                    [InlineKeyboardButton(text="🗑 Удалить", callback_data="managers_channel_delete")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
                ]
                await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")
            else:
                await message.answer("❌ Ошибка при сохранении канала. Попробуйте еще раз.")
    except ValueError:
        await message.answer("⚠️ Неверный формат. Отправьте число (ID канала).")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "managers_channel_delete")
async def delete_managers_channel(callback: CallbackQuery):
    """Удалить настройки канала менеджеров"""
    success = db.clear_managers_channel_id()
    
    if success:
        text = """📢 <b>Канал менеджеров</b>

Настройки канала удалены.

Сообщения от пользователей будут пересылаться в канал, указанный в .env файле (MANAGERS_CHANNEL_ID), или не будут пересылаться, если он не указан."""
        keyboard = [
            [InlineKeyboardButton(text="➕ Добавить", callback_data="managers_channel_set")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
        ]
        await _safe_callback_answer(callback, "✅ Канал удален")
        await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")
    else:
        await _safe_callback_answer(callback, "❌ Ошибка при удалении", show_alert=True)


# ========== КАТЕГОРИИ ==========
@router.callback_query(F.data.startswith("category_menu_"))
async def show_category_menu(callback: CallbackQuery):
    """Показать меню категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not db.can_access_category(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    userbots = db.get_category_userbots(category_id)
    groups = db.get_private_groups_by_category(category_id)
    keywords = db.get_category_keywords(category_id)
    stopwords = db.get_category_stopwords(category_id)
    
    text = f"📁 <b>{category['name']}</b>\n\n"
    text += f"Userbot'ы: {', '.join(userbots) if userbots else 'Не назначены'}\n"
    text += f"Канал менеджеров: <code>{category.get('managers_channel_id') or 'Не настроен'}</code>\n\n"
    text += f"📊 Статистика:\n"
    text += f"• Групп: {len(groups)}\n"
    text += f"• Ключевых слов: {len(keywords)}\n"
    text += f"• Стоп-слов: {len(stopwords)}\n"
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=get_category_menu(category_id, user_id), parse_mode="HTML")


@router.callback_query(F.data == "admin_categories")
async def show_categories(callback: CallbackQuery):
    """Показать список категорий"""
    categories = db.get_all_categories()
    active_category = db.get_active_category()
    active_category_id = active_category.get('id') if active_category else None
    
    text = f"📁 <b>Категории</b>\n\n"
    if not categories:
        text += "Нет категорий. Создайте первую категорию!"
    else:
        for cat in categories:
            is_active = "✅" if cat['id'] == active_category_id else "⚪"
            userbots = db.get_category_userbots(cat['id'])
            userbots_str = ", ".join(userbots) if userbots else "Не назначены"
            channel_id = cat.get('managers_channel_id') or "Не настроен"
            text += f"{is_active} <b>{cat['name']}</b>\n"
            text += f"  • ID: <code>{cat['id']}</code>\n"
            text += f"  • Userbot'ы ({len(userbots)}): {userbots_str}\n"
            text += f"  • Канал менеджеров: <code>{channel_id}</code>\n\n"
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Создать категорию", callback_data="category_add")],
    ]
    
    if categories:
        for cat in categories:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{'✅' if cat['id'] == active_category_id else '⚪'} {cat['name']}",
                    callback_data=f"category_view_{cat['id']}"
                )
            ])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")])
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data == "category_add_cancel")
async def add_category_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена создания категории"""
    data = await state.get_data()
    category_id = data.get('category_id')
    
    # Если категория уже была создана, удаляем её
    if category_id:
        db.delete_category(category_id)
    
    await state.clear()
    user_id = callback.from_user.id
    
    await _safe_callback_answer(callback, "❌ Создание категории отменено", show_alert=True)
    
    categories = db.get_all_categories()
    if not categories:
        await _safe_edit_text(
            callback,
            "📁 <b>Категории</b>\n\n"
            "У вас пока нет категорий. Создайте первую категорию для начала работы!",
            reply_markup=get_main_menu(user_id),
            parse_mode="HTML"
        )
    else:
        await _safe_edit_text(
            callback,
            "Выберите категорию для настройки:",
            reply_markup=get_main_menu(user_id)
        )


@router.callback_query(F.data == "category_add")
async def add_category_start(callback: CallbackQuery, state: FSMContext):
    """Начать создание категории"""
    user_id = callback.from_user.id
    
    # Только админ может создавать категории
    if not db.is_admin(user_id):
        await _safe_callback_answer(callback, "❌ Только администратор может создавать категории", show_alert=True)
        return
    
    await state.set_state(CategoryStates.waiting_for_name)
    await _safe_callback_answer(callback)
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data="category_add_cancel")]]
    
    await _safe_edit_text(
        callback,
        "📁 <b>Создание категории</b>\n\nОтправьте название категории:\n\n"
        "💡 <b>Важно:</b> Команда для категории будет создана автоматически на основе названия (например, 'Машины → /машины)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.message(CategoryStates.waiting_for_name)
async def add_category_name(message: Message, state: FSMContext):
    """Получить название категории"""
    name = message.text.strip()
    if not name:
        keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data="category_add_cancel")]]
        await message.answer("❌ Название не может быть пустым. Попробуйте снова:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        return
    
    # Проверяем уникальность
    categories = db.get_all_categories()
    if any(cat['name'].lower() == name.lower() for cat in categories):
        keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data="category_add_cancel")]]
        await message.answer("❌ Категория с таким названием уже существует. Попробуйте другое название:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        return
    
    category_id = db.add_category(name)
    if not category_id:
        await message.answer("❌ Ошибка при создании категории.")
        await state.clear()
        await message.answer("Выберите раздел:", reply_markup=get_main_menu(message.from_user.id))
        return
    
    await state.update_data(category_id=category_id)
    await state.set_state(CategoryStates.waiting_for_session_name)
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data="category_add_cancel")]]
    
    # Показываем список аккаунтов
    accounts = db.get_all_accounts()
    if not accounts:
        await message.answer(
            "✅ Категория создана!\n\n"
            "⚠️ Нет доступных аккаунтов. Добавьте аккаунты в разделе '👥 Аккаунты'.\n\n"
            "Отправьте имя сессии (session_name) для этой категории или отправьте 'пропустить' для пропуска:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    else:
        text = "✅ Категория создана!\n\nВыберите userbot для этой категории:\n\n"
        for acc in accounts:
            text += f"• <code>{acc['session_name']}</code> ({acc['phone']}) - {acc['status']}\n"
        text += "\nОтправьте имя сессии (session_name) или 'пропустить' для пропуска:"
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.message(CategoryStates.waiting_for_session_name)
async def add_category_session(message: Message, state: FSMContext):
    """Получить session_name для категории (старый обработчик - теперь пропускаем)"""
    session_name = message.text.strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if not category_id:
        await message.answer("❌ Ошибка: категория не найдена.")
        await state.clear()
        return
    
    # Пропускаем назначение userbot'а при создании - можно добавить позже
    await state.set_state(CategoryStates.waiting_for_managers_channel_id)
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data="category_add_cancel")]]
    
    await message.answer(
        "✅ Категория создана!\n\n"
        "💡 <b>Совет:</b> Userbot'ы можно добавить позже в настройках категории.\n\n"
        "Отправьте ID канала менеджеров (куда будут пересылаться ответы от пользователей) "
        "или 'пропустить' для пропуска:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.message(CategoryStates.waiting_for_managers_channel_id)
async def add_category_channel(message: Message, state: FSMContext):
    """Получить ID канала менеджеров для категории"""
    channel_text = message.text.strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if not category_id:
        await message.answer("❌ Ошибка: категория не найдена.")
        await state.clear()
        return
    
    if channel_text.lower() in ['пропустить', 'skip', '']:
        channel_id = None
    else:
        try:
            channel_id = int(channel_text)
        except ValueError:
            keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data="category_add_cancel")]]
            await message.answer("❌ ID канала должен быть числом. Попробуйте снова или отправьте 'пропустить':", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
            return
    
    db.update_category(category_id, {'managers_channel_id': channel_id})
    await state.clear()
    
    category = db.get_category(category_id)
    command = db.get_category_command(category_id)
    command_text = f"\n\n💡 <b>Команда для категории:</b> <code>/{command}</code>\n" if command else ""
    
    await message.answer(
        f"✅ Категория '{category['name']}' создана!{command_text}\n\n"
        f"Теперь вы можете настроить категорию:",
        reply_markup=get_category_menu(category_id, message.from_user.id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("category_edit_"))
async def edit_category(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    await state.update_data(category_id=category_id, edit_mode=True)
    
    text = f"✏️ <b>Редактирование категории: {category['name']}</b>\n\n"
    text += "Выберите что изменить:\n\n"
    text += "1. Название категории\n"
    text += "2. Userbot\n"
    text += "3. Канал менеджеров"
    
    keyboard = [
        [InlineKeyboardButton(text="📝 Название", callback_data=f"category_edit_name_{category_id}")],
        [InlineKeyboardButton(text="👤 Userbot", callback_data=f"category_edit_session_{category_id}")],
        [InlineKeyboardButton(text="📢 Канал менеджеров", callback_data=f"category_edit_channel_{category_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_view_{category_id}")]
    ]
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


class EditCategoryStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_session_name = State()
    waiting_for_channel_id = State()


@router.callback_query(F.data.startswith("category_edit_name_"))
async def edit_category_name_start(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование названия категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    await state.set_state(EditCategoryStates.waiting_for_name)
    await state.update_data(category_id=category_id)
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        f"✏️ <b>Редактирование названия</b>\n\nТекущее название: <b>{category['name']}</b>\n\nОтправьте новое название:",
        parse_mode="HTML"
    )


@router.message(EditCategoryStates.waiting_for_name)
async def edit_category_name_process(message: Message, state: FSMContext):
    """Обработать новое название категории"""
    name = message.text.strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if not name:
        await message.answer("❌ Название не может быть пустым. Попробуйте снова:")
        return
    
    # Проверяем уникальность
    categories = db.get_all_categories()
    if any(cat['id'] != category_id and cat['name'].lower() == name.lower() for cat in categories):
        await message.answer("❌ Категория с таким названием уже существует. Попробуйте другое название:")
        return
    
    success = db.update_category(category_id, {'name': name})
    if success:
        await message.answer("✅ Название обновлено!")
    else:
        await message.answer("❌ Ошибка при обновлении названия.")
    
    await state.clear()
    await message.answer("Выберите раздел:", reply_markup=get_main_menu())


@router.callback_query(F.data.startswith("category_edit_session_"))
async def edit_category_session_start(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование userbot'а категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    await state.set_state(EditCategoryStates.waiting_for_session_name)
    await state.update_data(category_id=category_id)
    
    category_userbots = db.get_category_userbots(category_id)
    accounts = db.get_all_accounts()
    text = f"✏️ <b>Редактирование userbot'ов</b>\n\n"
    text += f"Текущие userbot'ы ({len(category_userbots)}):\n"
    if category_userbots:
        for session_name in category_userbots:
            account = db.get_account(session_name)
            if account:
                text += f"• <code>{session_name}</code> ({account['phone']}) - {account['status']}\n"
            else:
                text += f"• <code>{session_name}</code>\n"
    else:
        text += "Не назначены\n"
    
    text += "\n💡 <b>Совет:</b> Используйте меню категории для управления userbot'ами.\n"
    text += "Там можно добавлять и удалять userbot'ы."
    
    keyboard = [
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_edit_{category_id}")]
    ]
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("category_edit_channel_"))
async def edit_category_channel_start(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование канала менеджеров категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    await state.set_state(EditCategoryStates.waiting_for_channel_id)
    await state.update_data(category_id=category_id)
    
    text = f"✏️ <b>Редактирование канала менеджеров</b>\n\n"
    text += f"Текущий канал: <code>{category.get('managers_channel_id') or 'Не настроен'}</code>\n\n"
    text += "Отправьте ID канала менеджеров или 'пропустить' для удаления:"
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, parse_mode="HTML")


@router.message(EditCategoryStates.waiting_for_channel_id)
async def edit_category_channel_process(message: Message, state: FSMContext):
    """Обработать новый канал менеджеров категории"""
    channel_text = message.text.strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if channel_text.lower() in ['пропустить', 'skip', '']:
        channel_id = None
    else:
        try:
            channel_id = int(channel_text)
        except ValueError:
            await message.answer("❌ ID канала должен быть числом. Попробуйте снова или отправьте 'пропустить':")
            return
    
    success = db.update_category(category_id, {'managers_channel_id': channel_id})
    if success:
        await message.answer("✅ Канал менеджеров обновлен!")
    else:
        await message.answer("❌ Ошибка при обновлении канала менеджеров.")
    
    await state.clear()
    await message.answer("Выберите раздел:", reply_markup=get_main_menu())


@router.callback_query(F.data.startswith("category_view_"))
async def view_category(callback: CallbackQuery):
    """Просмотр деталей категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    active_category = db.get_active_category()
    is_active = active_category and active_category.get('id') == category_id
    
    channels = db.get_category_channels(category_id)
    keywords = db.get_category_keywords(category_id)
    stopwords = db.get_category_stopwords(category_id)
    
    text = f"📁 <b>{category['name']}</b>\n\n"
    text += f"Статус: {'✅ Активна' if is_active else '⚪ Неактивна'}\n"
    userbots = db.get_category_userbots(category_id)
    userbots_str = ", ".join(userbots) if userbots else "Не назначены"
    text += f"Userbot'ы ({len(userbots)}): {userbots_str}\n"
    text += f"Канал менеджеров: <code>{category.get('managers_channel_id') or 'Не настроен'}</code>\n\n"
    text += f"Каналов: {len(channels)}\n"
    text += f"Ключевых слов: {len(keywords)}\n"
    text += f"Стоп-слов: {len(stopwords)}\n"
    
    keyboard = [
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"category_edit_{category_id}")],
        [InlineKeyboardButton(text="📢 Каналы", callback_data=f"category_channels_{category_id}")],
        [InlineKeyboardButton(text="🔑 Ключевые слова", callback_data=f"category_keywords_{category_id}")],
        [InlineKeyboardButton(text="🛑 Стоп-слова", callback_data=f"category_stopwords_{category_id}")],
    ]
    
    if not is_active:
        keyboard.append([InlineKeyboardButton(text="✅ Активировать", callback_data=f"category_activate_{category_id}")])
    else:
        keyboard.append([InlineKeyboardButton(text="⚪ Деактивировать", callback_data=f"category_deactivate")])
    
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"category_delete_{category_id}")])
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_categories")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("category_activate_"))
async def activate_category(callback: CallbackQuery):
    """Активировать категорию"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    db.set_active_category(category_id)
    
    # Обновляем category_id для всех сессий
    if userbot_manager:
        # Обновляем все сессии, чтобы применить новую активную категорию
        for session_name in list(userbot_manager.clients.keys()):
            await userbot_manager.update_category_for_session(session_name)
    
    await _safe_callback_answer(callback, "✅ Категория активирована!", show_alert=True)
    await view_category(callback)


@router.callback_query(F.data == "category_deactivate")
async def deactivate_category(callback: CallbackQuery):
    """Деактивировать категорию"""
    db.set_active_category(None)
    
    # Обновляем все сессии, чтобы убрать category_id
    if userbot_manager:
        for session_name in list(userbot_manager.clients.keys()):
            await userbot_manager.update_category_for_session(session_name)
    
    await _safe_callback_answer(callback, "✅ Категория деактивирована!", show_alert=True)
    await show_categories(callback)


@router.callback_query(F.data.startswith("category_delete_"))
async def delete_category_confirm(callback: CallbackQuery):
    """Подтверждение удаления категории"""
    user_id = callback.from_user.id
    
    # Только админ может удалять категории
    if not db.is_admin(user_id):
        await _safe_callback_answer(callback, "❌ Только администратор может удалять категории", show_alert=True)
        return
    
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    text = f"⚠️ <b>Подтвердите удаление</b>\n\n"
    text += f"Категория: <b>{category['name']}</b>\n\n"
    text += "Все связи с каналами, ключевыми словами и стоп-словами будут удалены.\n\n"
    text += "Это действие нельзя отменить!"
    
    keyboard = [
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"category_delete_confirm_{category_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"category_view_{category_id}")
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_categories")]
    ]
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("category_delete_confirm_"))
async def delete_category_execute(callback: CallbackQuery):
    """Удалить категорию"""
    user_id = callback.from_user.id
    
    # Только админ может удалять категории
    if not db.is_admin(user_id):
        await _safe_callback_answer(callback, "❌ Только администратор может удалять категории", show_alert=True)
        return
    
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    # Проверяем активна ли категория
    active_category = db.get_active_category()
    if active_category and active_category.get('id') == category_id:
        db.set_active_category(None)
    
    success = db.delete_category(category_id)
    if success:
        await _safe_callback_answer(callback, "✅ Категория удалена", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка при удалении", show_alert=True)
    
    await show_categories(callback)


@router.callback_query(F.data.startswith("category_channels_"))
async def manage_category_channels(callback: CallbackQuery):
    """Управление каналами категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    category_channels = db.get_category_channels(category_id)
    all_channels = db.get_all_channels()
    
    text = f"📢 <b>Каналы категории: {category['name']}</b>\n\n"
    text += f"Добавлено: {len(category_channels)}\n\n"
    
    if category_channels:
        text += "<b>Текущие каналы:</b>\n"
        for ch in category_channels[:10]:
            text += f"• {ch.get('title') or ch['link']}\n"
    
    keyboard = []
    
    # Кнопки для добавления каналов
    for ch in all_channels:
        is_added = any(cc['id'] == ch['id'] for cc in category_channels)
        if not is_added:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"➕ {ch.get('title') or ch['link'][:30]}",
                    callback_data=f"category_channel_add_{category_id}_{ch['id']}"
                )
            ])
    
    # Кнопки для удаления каналов
    if category_channels:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить канал", callback_data=f"category_channel_remove_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_view_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("category_channel_add_"))
async def add_channel_to_category(callback: CallbackQuery):
    """Добавить канал в категорию"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[3])
        channel_id = int(parts[4])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.add_category_channel(category_id, channel_id)
    if success:
        await _safe_callback_answer(callback, "✅ Канал добавлен", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await manage_category_channels(callback)


@router.callback_query(F.data.startswith("category_channel_remove_"))
async def remove_channel_from_category(callback: CallbackQuery):
    """Удалить канал из категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category_channels = db.get_category_channels(category_id)
    if not category_channels:
        await _safe_callback_answer(callback, "Нет каналов для удаления", show_alert=True)
        return
    
    keyboard = []
    for ch in category_channels[:20]:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 {ch.get('title') or ch['link'][:30]}",
                callback_data=f"category_channel_remove_exec_{category_id}_{ch['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_channels_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "🗑 <b>Удаление канала</b>\n\nВыберите канал для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("category_channel_remove_exec_"))
async def remove_channel_from_category_execute(callback: CallbackQuery):
    """Выполнить удаление канала из категории"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[4])
        channel_id = int(parts[5])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.remove_category_channel(category_id, channel_id)
    if success:
        await _safe_callback_answer(callback, "✅ Канал удален", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await manage_category_channels(callback)


@router.callback_query(F.data.startswith("category_keywords_"))
async def manage_category_keywords(callback: CallbackQuery):
    """Управление ключевыми словами категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    category_keywords = db.get_category_keywords(category_id)
    all_keywords = db.get_all_keywords_with_ids()
    
    text = f"🔑 <b>Ключевые слова категории: {category['name']}</b>\n\n"
    text += f"Добавлено: {len(category_keywords)}\n\n"
    
    if category_keywords:
        text += "<b>Текущие ключевые слова:</b>\n"
        for kw in category_keywords[:20]:
            text += f"• {kw}\n"
    
    keyboard = []
    
    # Кнопки для добавления ключевых слов
    category_keyword_ids = set()
    # Получаем ID ключевых слов категории
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT keyword_id FROM category_keywords WHERE category_id = ?", (category_id,))
    category_keyword_ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    for kw in all_keywords:
        if kw['id'] not in category_keyword_ids:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"➕ {kw['word']}",
                    callback_data=f"category_keyword_add_{category_id}_{kw['id']}"
                )
            ])
    
    # Кнопки для удаления ключевых слов
    if category_keywords:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить ключевое слово", callback_data=f"category_keyword_remove_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_view_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("category_keyword_add_"))
async def add_keyword_to_category(callback: CallbackQuery):
    """Добавить ключевое слово в категорию"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[3])
        keyword_id = int(parts[4])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.add_category_keyword(category_id, keyword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Ключевое слово добавлено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await manage_category_keywords(callback)


@router.callback_query(F.data.startswith("category_keyword_remove_"))
async def remove_keyword_from_category(callback: CallbackQuery):
    """Удалить ключевое слово из категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category_keywords = db.get_category_keywords(category_id)
    if not category_keywords:
        await _safe_callback_answer(callback, "Нет ключевых слов для удаления", show_alert=True)
        return
    
    # Получаем ID ключевых слов категории
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT k.id, k.word FROM keywords k
        INNER JOIN category_keywords ck ON k.id = ck.keyword_id
        WHERE ck.category_id = ?
    """, (category_id,))
    keywords_with_ids = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    keyboard = []
    for kw in keywords_with_ids[:20]:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 {kw['word']}",
                callback_data=f"category_keyword_remove_exec_{category_id}_{kw['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_keywords_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "🗑 <b>Удаление ключевого слова</b>\n\nВыберите ключевое слово для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("category_keyword_remove_exec_"))
async def remove_keyword_from_category_execute(callback: CallbackQuery):
    """Выполнить удаление ключевого слова из категории"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[4])
        keyword_id = int(parts[5])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.remove_category_keyword(category_id, keyword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Ключевое слово удалено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await manage_category_keywords(callback)


@router.callback_query(F.data.startswith("category_stopwords_"))
async def manage_category_stopwords(callback: CallbackQuery):
    """Управление стоп-словами категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    category_stopwords = db.get_category_stopwords(category_id)
    all_stopwords = db.get_all_stopwords_with_ids()
    
    text = f"🛑 <b>Стоп-слова категории: {category['name']}</b>\n\n"
    text += f"Добавлено: {len(category_stopwords)}\n\n"
    
    if category_stopwords:
        text += "<b>Текущие стоп-слова:</b>\n"
        for sw in category_stopwords[:20]:
            text += f"• {sw}\n"
    
    keyboard = []
    
    # Получаем ID стоп-слов категории
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT stopword_id FROM category_stopwords WHERE category_id = ?", (category_id,))
    category_stopword_ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    for sw in all_stopwords:
        if sw['id'] not in category_stopword_ids:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"➕ {sw['word']}",
                    callback_data=f"category_stopword_add_{category_id}_{sw['id']}"
                )
            ])
    
    if category_stopwords:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить стоп-слово", callback_data=f"category_stopword_remove_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_view_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("category_stopword_add_"))
async def add_stopword_to_category(callback: CallbackQuery):
    """Добавить стоп-слово в категорию"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[3])
        stopword_id = int(parts[4])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.add_category_stopword(category_id, stopword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Стоп-слово добавлено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await manage_category_stopwords(callback)


@router.callback_query(F.data.startswith("category_stopword_remove_"))
async def remove_stopword_from_category(callback: CallbackQuery):
    """Удалить стоп-слово из категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category_stopwords = db.get_category_stopwords(category_id)
    if not category_stopwords:
        await _safe_callback_answer(callback, "Нет стоп-слов для удаления", show_alert=True)
        return
    
    # Получаем ID стоп-слов категории
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.id, s.word FROM stopwords s
        INNER JOIN category_stopwords cs ON s.id = cs.stopword_id
        WHERE cs.category_id = ?
    """, (category_id,))
    stopwords_with_ids = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    keyboard = []
    for sw in stopwords_with_ids[:20]:
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 {sw['word']}",
                callback_data=f"category_stopword_remove_exec_{category_id}_{sw['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_stopwords_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "🗑 <b>Удаление стоп-слова</b>\n\nВыберите стоп-слово для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("category_stopword_remove_exec_"))
async def remove_stopword_from_category_execute(callback: CallbackQuery):
    """Выполнить удаление стоп-слова из категории"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[4])
        stopword_id = int(parts[5])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.remove_category_stopword(category_id, stopword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Стоп-слово удалено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await manage_category_stopwords(callback)


# ========== НАЗАД ==========
@router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery, state: FSMContext):
    """Вернуться в главное меню"""
    try:
        await state.clear()
    except Exception:
        pass
    
    user_id = callback.from_user.id
    categories = db.get_all_categories()
    
    if not categories:
        await _safe_edit_text(
            callback,
            "📁 <b>Категории</b>\n\n"
            "У вас пока нет категорий. Создайте первую категорию для начала работы!",
            reply_markup=get_main_menu(user_id),
            parse_mode="HTML"
        )
    else:
        await _safe_edit_text(
            callback,
            "Выберите категорию для настройки:",
            reply_markup=get_main_menu(user_id)
        )
    
    await _safe_callback_answer(callback)

