"""
Обработчики для меню категории
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from database.models import Database
from handlers.admin_panel import (
    _safe_callback_answer, _safe_edit_text, _pg_state_emoji,
    _is_private_invite_link, _is_public_target,
    AddPrivateGroupStates, ManagersChannelStates, AddAccountStates,
    AddKeywordsStates, AddStopwordsStates,
    get_category_menu
)
from services.userbot_manager import UserbotManager

router = Router()
db = Database()
userbot_manager: UserbotManager = None


def set_userbot_manager(manager: UserbotManager):
    """Установить менеджер userbot'ов"""
    global userbot_manager
    userbot_manager = manager


def check_category_access(user_id: int, category_id: int) -> bool:
    """Проверить доступ пользователя к категории"""
    return db.can_access_category(user_id, category_id)


# Приватные группы категории
@router.callback_query(F.data.startswith("cat_private_groups_"))
async def category_private_groups(callback: CallbackQuery):
    """Приватные группы категории"""
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
    
    groups = db.get_private_groups_by_category(category_id)
    private_groups = [g for g in groups if _is_private_invite_link(g.get("invite_link", ""))]
    
    text = f"🔒 <b>Приватные группы: {category['name']}</b>\n\n"
    text += f"Всего: {len(private_groups)}\n\n"
    
    if not private_groups:
        text += "Нет приватных групп. Добавьте первую группу!"
    else:
        for g in private_groups[:10]:
            state = g.get("state", "UNKNOWN")
            emoji = _pg_state_emoji(state)
            title = (g.get("title") or "Без названия").strip()
            text += f"{emoji} <b>{title}</b> — <code>{g.get('id')}</code> (<code>{state}</code>)\n"
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"cat_add_private_group_{category_id}")],
    ]
    
    if private_groups:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cat_delete_private_group_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_add_private_group_"))
async def cat_add_private_group_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление приватной группы в категорию"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    await state.set_state(AddPrivateGroupStates.waiting_for_private_invite_link)
    await state.update_data(category_id=category_id)
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "🔒 <b>Добавление приватной группы</b>\n\n"
        "Отправьте invite-ссылку:\n"
        "- `https://t.me/+HASH`\n"
        "- `https://t.me/joinchat/HASH`\n"
        "- `+HASH`\n\n"
        "⚠️ Важно: это именно приватное приглашение (без публичного @username).",
        parse_mode="HTML"
    )


@router.message(AddPrivateGroupStates.waiting_for_private_invite_link)
async def cat_add_private_group_process(message: Message, state: FSMContext):
    """Обработать добавление приватной группы в категорию"""
    invite_link = (message.text or "").strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if not category_id:
        await message.answer("❌ Ошибка: категория не найдена. Начните заново.")
        await state.clear()
        return
    
    # Проверяем что категория существует
    category = db.get_category(category_id)
    if not category:
        await message.answer("❌ Ошибка: категория не найдена.")
        await state.clear()
        return
    
    if not _is_private_invite_link(invite_link):
        keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"category_menu_{category_id}")]]
        await message.answer("❌ Формат не похож на приватный invite. Отправьте `https://t.me/+HASH` или `https://t.me/joinchat/HASH` или `+HASH`.", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        return

    group_id = db.add_private_group(invite_link, category_id=category_id)
    if not group_id:
        await message.answer("❌ Не удалось добавить (возможно ошибка БД).")
        await state.clear()
        return

    await message.answer("✅ Добавлено.")
    await state.clear()
    
    # Показываем список групп
    category = db.get_category(category_id)
    groups = db.get_private_groups_by_category(category_id)
    private_groups = [g for g in groups if _is_private_invite_link(g.get("invite_link", ""))]
    
    text = f"🔒 <b>Приватные группы: {category['name']}</b>\n\n"
    text += f"Всего: {len(private_groups)}\n\n"
    
    if private_groups:
        for g in private_groups[:10]:
            state_emoji = _pg_state_emoji(g.get("state", "UNKNOWN"))
            title = (g.get("title") or "Без названия").strip()
            text += f"{state_emoji} <b>{title}</b> — <code>{g.get('id')}</code>\n"
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"cat_add_private_group_{category_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")],
    ]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


# Публичные группы категории
@router.callback_query(F.data.startswith("cat_public_groups_"))
async def category_public_groups(callback: CallbackQuery):
    """Публичные группы категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not check_category_access(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    groups = db.get_private_groups_by_category(category_id)
    public_groups = [g for g in groups if not _is_private_invite_link(g.get("invite_link", "")) and _is_public_target(g.get("invite_link", ""))]
    
    text = f"🌐 <b>Публичные группы: {category['name']}</b>\n\n"
    text += f"Всего: {len(public_groups)}\n\n"
    
    if not public_groups:
        text += "Нет публичных групп. Добавьте первую группу!"
    else:
        for g in public_groups[:10]:
            state = g.get("state", "UNKNOWN")
            emoji = _pg_state_emoji(state)
            title = (g.get("title") or "Без названия").strip()
            text += f"{emoji} <b>{title}</b> — <code>{g.get('id')}</code> (<code>{state}</code>)\n"
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"cat_add_public_group_{category_id}")],
    ]
    
    if public_groups:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cat_delete_public_group_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_add_public_group_"))
async def cat_add_public_group_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление публичной группы в категорию"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    await state.set_state(AddPrivateGroupStates.waiting_for_public_link)
    await state.update_data(category_id=category_id)
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "🌐 <b>Добавление публичной группы/канала</b>\n\n"
        "Отправьте одно из:\n"
        "- `@username`\n"
        "- `username`\n"
        "- `https://t.me/username`\n\n"
        "⚠️ Не отправляйте `t.me/+...` — это приватные инвайты (используйте кнопку «Приватная»).",
        parse_mode="HTML"
    )


@router.message(AddPrivateGroupStates.waiting_for_public_link)
async def cat_add_public_group_process(message: Message, state: FSMContext):
    """Обработать добавление публичной группы в категорию"""
    public_link = (message.text or "").strip()
    data = await state.get_data()
    category_id = data.get('category_id')
    
    if not category_id:
        await message.answer("❌ Ошибка: категория не найдена. Начните заново.")
        await state.clear()
        return
    
    # Проверяем что категория существует
    category = db.get_category(category_id)
    if not category:
        await message.answer("❌ Ошибка: категория не найдена.")
        await state.clear()
        return
    
    if not _is_public_target(public_link):
        keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"category_menu_{category_id}")]]
        await message.answer("❌ Формат не похож на публичный username/ссылку. Пример: `@username` или `https://t.me/username`.", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        return

    group_id = db.add_private_group(public_link, category_id=category_id)
    if not group_id:
        await message.answer("❌ Не удалось добавить (возможно ошибка БД).")
        await state.clear()
        return

    await message.answer("✅ Добавлено.")
    await state.clear()
    
    # Показываем список групп
    category = db.get_category(category_id)
    groups = db.get_private_groups_by_category(category_id)
    public_groups = [g for g in groups if not _is_private_invite_link(g.get("invite_link", "")) and _is_public_target(g.get("invite_link", ""))]
    
    text = f"🌐 <b>Публичные группы: {category['name']}</b>\n\n"
    text += f"Всего: {len(public_groups)}\n\n"
    
    if public_groups:
        for g in public_groups[:10]:
            state_emoji = _pg_state_emoji(g.get("state", "UNKNOWN"))
            title = (g.get("title") or "Без названия").strip()
            text += f"{state_emoji} <b>{title}</b> — <code>{g.get('id')}</code>\n"
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"cat_add_public_group_{category_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")],
    ]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


# Userbot категории
@router.callback_query(F.data.startswith("cat_userbot_"))
async def category_userbot(callback: CallbackQuery, state: FSMContext):
    """Настройка userbot'а категории"""
    # Очищаем state при возврате в меню
    await state.clear()
    
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not check_category_access(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    category_userbots = db.get_category_userbots(category_id)
    accounts = db.get_all_accounts()
    
    text = f"👤 <b>Userbot'ы категории: {category['name']}</b>\n\n"
    text += f"Назначено: <b>{len(category_userbots)}</b>\n\n"
    
    if category_userbots:
        text += "<b>Текущие userbot'ы:</b>\n"
        for session_name in category_userbots:
            account = db.get_account(session_name)
            if account:
                text += f"✅ <code>{session_name}</code> ({account['phone']}) - {account['status']}\n"
            else:
                text += f"✅ <code>{session_name}</code> (аккаунт не найден в БД)\n"
    else:
        text += "Нет назначенных userbot'ов.\n"
    
    text += "\n"
    
    if accounts:
        text += "Доступные аккаунты:\n"
        for acc in accounts:
            is_assigned = "✅" if acc['session_name'] in category_userbots else "⚪"
            text += f"{is_assigned} <code>{acc['session_name']}</code> ({acc['phone']}) - {acc['status']}\n"
    else:
        text += "Нет доступных аккаунтов. Добавьте аккаунты в разделе '👥 Аккаунты'."
    
    keyboard = []
    
    # Кнопка добавления нового userbot'а
    keyboard.append([InlineKeyboardButton(text="➕ Добавить новый userbot", callback_data=f"cat_add_userbot_{category_id}")])
    
    if accounts:
        keyboard.append([InlineKeyboardButton(text="📋 Выбрать из существующих", callback_data=f"cat_select_userbot_{category_id}")])
    
    if category_userbots:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить userbot", callback_data=f"cat_remove_userbot_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_set_userbot_"))
async def category_set_userbot(callback: CallbackQuery):
    """Добавить userbot в категорию"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[3])
        session_name = "_".join(parts[4:])  # На случай если session_name содержит подчеркивания
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    # Проверяем, не назначен ли уже этот userbot
    category_userbots = db.get_category_userbots(category_id)
    if session_name in category_userbots:
        await _safe_callback_answer(callback, "⚠️ Этот userbot уже назначен категории", show_alert=True)
        await category_userbot(callback)
        return
    
    success = db.add_category_userbot(category_id, session_name)
    if success:
        await _safe_callback_answer(callback, "✅ Userbot добавлен в категорию!", show_alert=True)
        if userbot_manager:
            await userbot_manager.update_category_for_session(session_name)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await category_userbot(callback)


@router.callback_query(F.data.startswith("cat_remove_userbot_"))
async def category_remove_userbot(callback: CallbackQuery):
    """Показать список userbot'ов для удаления"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category_userbots = db.get_category_userbots(category_id)
    if not category_userbots:
        await _safe_callback_answer(callback, "Нет userbot'ов для удаления", show_alert=True)
        return
    
    text = "🗑 <b>Удаление userbot'а</b>\n\nВыберите userbot для удаления:"
    
    keyboard = []
    for session_name in category_userbots:
        account = db.get_account(session_name)
        if account:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🗑 {session_name} ({account['phone']})",
                    callback_data=f"cat_remove_userbot_exec_{category_id}_{session_name}"
                )
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🗑 {session_name}",
                    callback_data=f"cat_remove_userbot_exec_{category_id}_{session_name}"
                )
            ])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_userbot_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_remove_userbot_exec_"))
async def category_remove_userbot_execute(callback: CallbackQuery):
    """Выполнить удаление userbot'а из категории"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[4])
        session_name = "_".join(parts[5:])  # На случай если session_name содержит подчеркивания
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.remove_category_userbot(category_id, session_name)
    if success:
        await _safe_callback_answer(callback, "✅ Userbot удален из категории!", show_alert=True)
        if userbot_manager:
            await userbot_manager.update_category_for_session(session_name)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await category_userbot(callback)


@router.callback_query(F.data.startswith("cat_add_userbot_"))
async def category_add_userbot_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление нового userbot'а для категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    # Проверяем есть ли глобальные настройки API
    global_api = db.get_global_api_settings()
    has_global_api = global_api and global_api.get('api_id') and global_api.get('api_hash')
    
    await state.update_data(category_id=category_id)
    
    text = f"👤 <b>Добавление userbot'а для категории: {category['name']}</b>\n\n"
    
    if has_global_api:
        text += "Выберите способ добавления:\n\n"
        text += "📱 <b>Быстрый способ:</b> Используйте '📱 Добавить по телефону' - потребуется только номер телефона и код!\n\n"
        text += "📝 <b>Полный способ:</b> Продолжить с вводом API_ID/API_HASH для этого аккаунта"
        
        keyboard = [
            [InlineKeyboardButton(text="📱 Быстрый (только телефон)", callback_data=f"cat_account_add_simple_{category_id}")],
            [InlineKeyboardButton(text="📝 Полный (с API_ID/API_HASH)", callback_data=f"cat_account_add_full_{category_id}")],
            [InlineKeyboardButton(text="📁 Загрузить сессию", callback_data=f"cat_account_add_session_{category_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_userbot_{category_id}")],
        ]
    else:
        text += "Выберите способ добавления:\n\n"
        text += "📝 <b>Полный способ:</b> Продолжить с вводом API_ID/API_HASH для этого аккаунта\n\n"
        text += "📁 <b>Загрузка сессии:</b> Загрузить готовый .session файл"
        
        keyboard = [
            [InlineKeyboardButton(text="📝 Полный (с API_ID/API_HASH)", callback_data=f"cat_account_add_full_{category_id}")],
            [InlineKeyboardButton(text="📁 Загрузить сессию", callback_data=f"cat_account_add_session_{category_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_userbot_{category_id}")],
        ]
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_select_userbot_"))
async def category_select_userbot(callback: CallbackQuery):
    """Показать список существующих аккаунтов для выбора"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    category_userbots = db.get_category_userbots(category_id)
    accounts = db.get_all_accounts()
    
    if not accounts:
        await _safe_callback_answer(callback, "Нет доступных аккаунтов", show_alert=True)
        return
    
    text = f"📋 <b>Выбор userbot'а для категории: {category['name']}</b>\n\n"
    text += "Выберите аккаунт из списка:"
    
    keyboard = []
    
    for acc in accounts:
        # Показываем только тех, кто еще не назначен категории
        if acc['session_name'] not in category_userbots:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"➕ {acc['session_name']} ({acc['phone']})",
                    callback_data=f"cat_set_userbot_{category_id}_{acc['session_name']}"
                )
            ])
    
    if not keyboard:
        text += "\n\n⚠️ Все доступные аккаунты уже назначены этой категории."
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_userbot_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_account_add_simple_"))
async def cat_account_add_simple_start(callback: CallbackQuery, state: FSMContext):
    """Начать упрощенное добавление аккаунта для категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    global_api = db.get_global_api_settings()
    if not global_api or not global_api.get('api_id') or not global_api.get('api_hash'):
        await _safe_callback_answer(callback, "❌ Сначала настройте глобальные API credentials в '⚙️ Настройки API'", show_alert=True)
        return
    
    await state.set_state(AddAccountStates.waiting_for_phone_simple)
    await state.update_data(category_id=category_id)
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"cat_userbot_{category_id}")]]
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "📱 <b>Упрощенное добавление userbot'а</b>\n\n"
        "Используются глобальные настройки API.\n\n"
        "Отправьте номер телефона (с кодом страны, например: +79991234567):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("cat_account_add_full_"))
async def cat_account_add_full_start(callback: CallbackQuery, state: FSMContext):
    """Начать полное добавление аккаунта для категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    await state.set_state(AddAccountStates.waiting_for_api_id)
    await state.update_data(category_id=category_id)
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"cat_userbot_{category_id}")]]
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "📝 <b>Полное добавление userbot'а</b>\n\n"
        "Отправьте API_ID:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("cat_account_add_session_"))
async def cat_account_add_session_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление аккаунта через сессию для категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    await state.set_state(AddAccountStates.waiting_for_session_name)
    await state.update_data(category_id=category_id)
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"cat_userbot_{category_id}")]]
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "📁 <b>Загрузка готовой сессии</b>\n\n"
        "Этот способ позволяет добавить аккаунт БЕЗ API_ID и API_HASH!\n\n"
        "1. Отправьте имя для сессии (например: account_123456789)\n"
        "2. Затем отправьте .session файл\n\n"
        "Отправьте имя сессии:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


# Канал менеджеров категории
@router.callback_query(F.data.startswith("cat_managers_channel_"))
async def category_managers_channel(callback: CallbackQuery):
    """Настройка канала менеджеров категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not check_category_access(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    channel_id = category.get('managers_channel_id')
    
    text = f"📢 <b>Канал менеджеров: {category['name']}</b>\n\n"
    text += f"Текущий канал: <code>{channel_id or 'Не настроен'}</code>\n\n"
    text += "Сообщения от пользователей будут пересылаться в этот канал."
    
    keyboard = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"cat_set_channel_{category_id}")],
    ]
    
    if channel_id:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cat_remove_channel_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_set_channel_"))
async def category_set_channel_start(callback: CallbackQuery, state: FSMContext):
    """Начать настройку канала менеджеров категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    await state.set_state(ManagersChannelStates.waiting_for_channel_id)
    await state.update_data(category_id=category_id)
    
    text = "📢 <b>Настройка канала менеджеров</b>\n\n"
    text += "Отправьте ID канала (число, например: -1001234567890):"
    
    keyboard = [[InlineKeyboardButton(text="❌ Отмена", callback_data=f"cat_managers_channel_{category_id}")]]
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_remove_channel_"))
async def category_remove_channel(callback: CallbackQuery):
    """Удалить канал менеджеров из категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    success = db.update_category(category_id, {'managers_channel_id': None})
    if success:
        await _safe_callback_answer(callback, "✅ Канал удален!", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await category_managers_channel(callback)


# Ключевые слова категории
@router.callback_query(F.data.startswith("cat_keywords_"))
async def category_keywords_menu(callback: CallbackQuery):
    """Меню ключевых слов категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not check_category_access(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    keywords = db.get_category_keywords(category_id)
    
    text = f"🔑 <b>Ключевые слова: {category['name']}</b>\n\n"
    text += f"Добавлено: {len(keywords)}\n\n"
    
    if keywords:
        text += "<b>Текущие ключевые слова:</b>\n"
        for kw in keywords[:20]:
            text += f"• {kw}\n"
    
    keyboard = []
    
    # Кнопка для добавления нового ключевого слова
    keyboard.append([InlineKeyboardButton(text="➕ Добавить новое", callback_data=f"cat_keyword_add_new_{category_id}")])
    
    if keywords:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cat_keyword_remove_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_keyword_add_new_"))
async def category_keyword_add_new_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление нового ключевого слова в категорию"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not check_category_access(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    await state.set_state(AddKeywordsStates.waiting_for_keywords)
    await state.update_data(category_id=category_id)
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "➕ <b>Добавление нового ключевого слова</b>\n\n"
        "Отправьте ключевые слова:\n\n"
        "💡 <b>Форматы ввода:</b>\n"
        "• Через запятую: <code>окна, двери, стекло</code>\n"
        "• Каждое с новой строки:\n<code>окна\nдвери\nстекло</code>\n"
        "• Или смешанный формат\n\n"
        "Они будут добавлены в общий список и автоматически привязаны к категории.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cat_keywords_{category_id}")]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("cat_keyword_add_"))
async def category_keyword_add(callback: CallbackQuery):
    """Добавить существующее ключевое слово в категорию"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[3])
        keyword_id = int(parts[4])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.add_category_keyword(category_id, keyword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Добавлено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await category_keywords_menu(callback)


@router.callback_query(F.data.startswith("cat_keyword_remove_") & ~F.data.startswith("cat_keyword_remove_exec_"))
async def category_keyword_remove(callback: CallbackQuery):
    """Удалить ключевое слово из категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    keywords = db.get_category_keywords(category_id)
    if not keywords:
        await _safe_callback_answer(callback, "Нет ключевых слов", show_alert=True)
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
                callback_data=f"cat_keyword_remove_exec_{category_id}_{kw['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_keywords_{category_id}")])
    
    category = db.get_category(category_id)
    category_name = category['name'] if category else f"ID {category_id}"
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        f"🗑 <b>Удаление ключевого слова</b>\n\nКатегория: <b>{category_name}</b>\n\nВыберите ключевое слово для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("cat_keyword_remove_exec_"))
async def category_keyword_remove_execute(callback: CallbackQuery):
    """Выполнить удаление ключевого слова"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[4])
        keyword_id = int(parts[5])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.remove_category_keyword(category_id, keyword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Удалено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    # Проверяем, остались ли еще ключевые слова
    keywords = db.get_category_keywords(category_id)
    if keywords:
        # Если есть еще слова, остаемся в меню удаления - показываем обновленный список
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
                    callback_data=f"cat_keyword_remove_exec_{category_id}_{kw['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_keywords_{category_id}")])
        
        category = db.get_category(category_id)
        category_name = category['name'] if category else f"ID {category_id}"
        
        await _safe_edit_text(
            callback,
            f"🗑 <b>Удаление ключевого слова</b>\n\nКатегория: <b>{category_name}</b>\n\nВыберите ключевое слово для удаления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )
    else:
        # Если слов не осталось, возвращаемся в главное меню
        category = db.get_category(category_id)
        if not category:
            await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
            return
        
        text = f"🔑 <b>Ключевые слова: {category['name']}</b>\n\n"
        text += f"Добавлено: 0\n\n"
        
        keyboard = [
            [InlineKeyboardButton(text="➕ Добавить новое", callback_data=f"cat_keyword_add_new_{category_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")]
        ]
        
        await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


# Стоп-слова категории
@router.callback_query(F.data.startswith("cat_stopwords_"))
async def category_stopwords_menu(callback: CallbackQuery):
    """Меню стоп-слов категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not check_category_access(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    category = db.get_category(category_id)
    if not category:
        await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
        return
    
    stopwords = db.get_category_stopwords(category_id)
    
    text = f"🛑 <b>Стоп-слова: {category['name']}</b>\n\n"
    text += f"Добавлено: {len(stopwords)}\n\n"
    
    if stopwords:
        text += "<b>Текущие стоп-слова:</b>\n"
        for sw in stopwords[:20]:
            text += f"• {sw}\n"
    
    keyboard = []
    
    # Кнопка для добавления нового стоп-слова
    keyboard.append([InlineKeyboardButton(text="➕ Добавить новое", callback_data=f"cat_stopword_add_new_{category_id}")])
    
    if stopwords:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cat_stopword_remove_{category_id}")])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")])
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@router.callback_query(F.data.startswith("cat_stopword_add_new_"))
async def category_stopword_add_new_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление нового стоп-слова в категорию"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    # Проверяем права доступа
    if not check_category_access(user_id, category_id):
        await _safe_callback_answer(callback, "❌ Доступ запрещен", show_alert=True)
        return
    
    await state.set_state(AddStopwordsStates.waiting_for_stopwords)
    await state.update_data(category_id=category_id)
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        "➕ <b>Добавление нового стоп-слова</b>\n\n"
        "Отправьте стоп-слова:\n\n"
        "💡 <b>Форматы ввода:</b>\n"
        "• Через запятую: <code>реклама, спам, продажа</code>\n"
        "• Каждое с новой строки:\n<code>реклама\nспам\nпродажа</code>\n"
        "• Или смешанный формат\n\n"
        "Они будут добавлены в общий список и автоматически привязаны к категории.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cat_stopwords_{category_id}")]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("cat_stopword_add_"))
async def category_stopword_add(callback: CallbackQuery):
    """Добавить существующее стоп-слово в категорию"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[3])
        stopword_id = int(parts[4])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.add_category_stopword(category_id, stopword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Добавлено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    await category_stopwords_menu(callback)


@router.callback_query(F.data.startswith("cat_stopword_remove_") & ~F.data.startswith("cat_stopword_remove_exec_"))
async def category_stopword_remove(callback: CallbackQuery):
    """Удалить стоп-слово из категории"""
    try:
        category_id = int(callback.data.split("_")[-1])
    except Exception:
        await _safe_callback_answer(callback, "Некорректный ID", show_alert=True)
        return
    
    stopwords = db.get_category_stopwords(category_id)
    if not stopwords:
        await _safe_callback_answer(callback, "Нет стоп-слов", show_alert=True)
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
                callback_data=f"cat_stopword_remove_exec_{category_id}_{sw['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_stopwords_{category_id}")])
    
    category = db.get_category(category_id)
    category_name = category['name'] if category else f"ID {category_id}"
    
    await _safe_callback_answer(callback)
    await _safe_edit_text(
        callback,
        f"🗑 <b>Удаление стоп-слова</b>\n\nКатегория: <b>{category_name}</b>\n\nВыберите стоп-слово для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("cat_stopword_remove_exec_"))
async def category_stopword_remove_execute(callback: CallbackQuery):
    """Выполнить удаление стоп-слова"""
    try:
        parts = callback.data.split("_")
        category_id = int(parts[4])
        stopword_id = int(parts[5])
    except Exception:
        await _safe_callback_answer(callback, "Некорректные параметры", show_alert=True)
        return
    
    success = db.remove_category_stopword(category_id, stopword_id)
    if success:
        await _safe_callback_answer(callback, "✅ Удалено", show_alert=True)
    else:
        await _safe_callback_answer(callback, "❌ Ошибка", show_alert=True)
    
    # Проверяем, остались ли еще стоп-слова
    stopwords = db.get_category_stopwords(category_id)
    if stopwords:
        # Если есть еще слова, остаемся в меню удаления - показываем обновленный список
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
                    callback_data=f"cat_stopword_remove_exec_{category_id}_{sw['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_stopwords_{category_id}")])
        
        category = db.get_category(category_id)
        category_name = category['name'] if category else f"ID {category_id}"
        
        await _safe_edit_text(
            callback,
            f"🗑 <b>Удаление стоп-слова</b>\n\nКатегория: <b>{category_name}</b>\n\nВыберите стоп-слово для удаления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )
    else:
        # Если слов не осталось, возвращаемся в главное меню
        category = db.get_category(category_id)
        if not category:
            await _safe_callback_answer(callback, "Категория не найдена", show_alert=True)
            return
        
        text = f"🛑 <b>Стоп-слова: {category['name']}</b>\n\n"
        text += f"Добавлено: 0\n\n"
        
        keyboard = [
            [InlineKeyboardButton(text="➕ Добавить новое", callback_data=f"cat_stopword_add_new_{category_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"category_menu_{category_id}")]
        ]
        
        await _safe_edit_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")

