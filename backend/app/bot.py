import os
import asyncio
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from .models import async_session, SystemSetting, select, Task, TeamMember
from .ai_logic import get_ai_advice

dp = Dispatcher()
active_bot = None
MSK_TZ = ZoneInfo("Europe/Moscow")

# ЗАМЕНИТЕ НА РЕАЛЬНЫЙ IP ВАШЕГО СЕРВЕРА!
VPS_IP = "159.194.230.59" 

async def get_bot():
    global active_bot
    async with async_session() as session:
        res = await session.execute(select(SystemSetting).where(SystemSetting.key_name == "telegram_token"))
        s = res.scalars().first()
        token = s.value if s else os.getenv("TELEGRAM_TOKEN")
        if token:
            active_bot = Bot(token=token)
            return active_bot
    return None

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks")],
        [InlineKeyboardButton(text="🌙 Вечерняя синхронизация", callback_data="evening_sync")],
        [InlineKeyboardButton(text="📝 Задача с митинга (Админ)", callback_data="meeting_prompt")],
        [InlineKeyboardButton(text="🌐 Открыть Канбан-доску", url=f"http://{VPS_IP}:8000/kanban")]
    ])

@dp.message(Command("start", "menu"))
async def cmd_start(m: types.Message):
    welcome_text = (
        f"👋 Привет, {m.from_user.full_name}!\n\n"
        "Я ваш AI Project Manager. Чтобы я создал задачу, используйте ключевые слова:\n"
        "💡 *Пример:* `сделай отчет до пятницы, критично` (без @ тоже сработает!)\n\n"
        "Если @ник не указан, задача автоматически попадет в 📥 Бэклог."
    )
    await m.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "meeting_prompt")
async def prompt_meeting(callback: types.CallbackQuery):
    await callback.message.answer(
        "📝 **Добавление задач с митинга**\n\n"
        "Отправьте мне текст итогов встречи.\n"
        "Пример: `Иван делает деплой до среды, Мария готовит презентацию`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_private_text(m: types.Message):
    if m.text.startswith("/"): return
    
    if len(m.text) > 30:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить и в Канбан", callback_data=f"approve_meeting:{m.text}")],
            [InlineKeyboardButton(text="❌ В корзину", callback_data="reject_meeting")]
        ])
        await m.answer(f"🧠 **Черновик с митинга:**\n\n`{m.text}`\n\nПодтвердите:", reply_markup=keyboard, parse_mode="Markdown")
        return

    assignee = m.from_user.username or "Не_назначен"
    await process_task(m, assignee, m.text)

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.text)
async def handle_group_text(m: types.Message):
    # Игнорируем команды бота
    if m.text.startswith("/"):
        return
    
    text_lower = m.text.lower()
    # Ключевые слова, указывающие на постановку задачи
    keywords = ["сделать", "задача", "нужно", "деплой", "проверить", "исправить", "добавить", "написать", "до", "дедлайн", "срок", "подготовить", "выполнить"]
    
    # 1. Сначала проверяем наличие ключевых слов
    has_keyword = any(kw in text_lower for kw in keywords)
    if not has_keyword:
        return  # Это обычный диалог ("привет", "как дела"), игнорируем

    # 2. Если ключевые слова есть, ищем @ник
    match = re.search(r'@(\w+)', m.text)
    if match:
        assignee = match.group(1)
        title = m.text.replace(f"@{assignee}", "").strip()
    else:
        # 3. Если @ника нет, но есть ключевые слова -> отправляем в Бэклог
        assignee = "Не_назначен"
        title = m.text.strip()
    
    if not title:
        return
    
    # Передаем на обработку
    await process_task(m, assignee, title)

async def process_task(m: types.Message, assignee: str, title: str):
    print(f"=== ОБРАБОТКА ===\nОтветственный: {assignee}\nТекст: {title}")
    
    priority = "high" if re.search(r'\b(критично|срочно|важно|high|urgent|асап)\b', title, re.IGNORECASE) else "normal"
    
    # Если исполнитель "Не_назначен", статус сразу "backlog"
    status = "backlog" if assignee == "Не_назначен" else "todo"

    deadline_dt = None
    clean_title = title
    
    days_map = {'понедельник': 0, 'вторник': 1, 'среда': 2, 'четверг': 3, 'пятница': 4, 'суббота': 5, 'воскресенье': 6}
    day_match = re.search(r'(?:до|дедлайн|срок)[:\s]+(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)', title, re.IGNORECASE)
    
    if day_match:
        day_name = day_match.group(1).lower()
        target_weekday = days_map[day_name]
        today = datetime.now(MSK_TZ)
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0: days_ahead += 7
        deadline_dt = (today + timedelta(days=days_ahead)).replace(hour=18, minute=0, second=0, microsecond=0)
        clean_title = re.sub(r'(?:до|дедлайн|срок)[:\s]+' + day_name, '', title, flags=re.IGNORECASE).strip()
    else:
        date_match = re.search(r'(?:до|дедлайн|срок)[:\s]+(\d{2}\.\d{2})(?:\s+(\d{2}:\d{2}))?', title, re.IGNORECASE)
        if date_match:
            day, month = map(int, date_match.group(1).split('.'))
            time_str = date_match.group(2)
            year = datetime.now().year
            if time_str:
                hour, minute = map(int, time_str.split(':'))
                deadline_dt = datetime(year, month, day, hour, minute, tzinfo=MSK_TZ)
            else:
                deadline_dt = datetime(year, month, day, 18, 0, tzinfo=MSK_TZ)
            clean_title = re.sub(r'(?:до|дедлайн|срок)[:\s]+\d{2}\.\d{2}(?:\s+\d{2}:\d{2})?', '', title, flags=re.IGNORECASE).strip()

    if deadline_dt: deadline_dt = deadline_dt.replace(tzinfo=None)

    try:
        async with async_session() as session:
            new_task = Task(title=clean_title, assignee=assignee, priority=priority, status=status, deadline=deadline_dt, chat_id=str(m.chat.id))
            session.add(new_task)
            await session.commit()
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
        await m.reply(f"❌ Ошибка: {e}")
        return

    if m.from_user.username:
        async with async_session() as session:
            res = await session.execute(select(TeamMember).where(TeamMember.username == m.from_user.username))
            user = res.scalars().first()
            if user:
                user.xp += 10
                await session.commit()

    deadline_text = f" (дедлайн: {deadline_dt.strftime('%d.%m %H:%M')} МСК)" if deadline_dt else ""
    priority_text = " 🔥 **КРИТИЧНО**" if priority == "high" else ""
    assignee_text = f"@{assignee}" if assignee != "Не_назначен" else "📥 **Бэклог** (админ назначит на доске)"
    
    response = f"✅ Задача добавлена!\n👤 Исполнитель: {assignee_text}\n{priority_text}{deadline_text}"
    await m.reply(response, parse_mode="Markdown")

# --- CALLBACKS ---
@dp.callback_query(F.data.startswith("approve_meeting:"))
async def approve_meeting_task(callback: types.CallbackQuery):
    task_text = callback.data.split(":", 1)[1]
    assignee = callback.from_user.username or "Не_назначен"
    status = "backlog" if assignee == "Не_назначен" else "todo"
    
    try:
        async with async_session() as session:
            new_task = Task(title="Задача с митинга", description=task_text, assignee=assignee, status=status, chat_id=str(callback.message.chat.id))
            session.add(new_task)
            await session.commit()
        await callback.message.edit_text(f"✅ Подтверждено и добавлено в Канбан!\n\n`{task_text}`", parse_mode="Markdown")
        await callback.answer("Создано")
    except Exception as e:
        await callback.answer("Ошибка БД", show_alert=True)

@dp.callback_query(F.data == "reject_meeting")
async def reject_meeting_task(callback: types.CallbackQuery):
    await callback.message.edit_text("🗑 Задача отправлена в корзину.")
    await callback.answer("Отклонено")

@dp.callback_query(F.data == "evening_sync")
async def evening_sync_callback(callback: types.CallbackQuery):
    await callback.message.answer("⏳ Анализирую задачи...")
    await callback.answer()
    async with async_session() as session:
        res = await session.execute(select(Task).where(Task.status != 'done'))
        tasks = res.scalars().all()
        if not tasks:
            await callback.message.answer("🎉 Все задачи выполнены!")
            return
        report = "🌙 **Вечерняя синхронизация**\n\n"
        user_tasks = defaultdict(list)
        for t in tasks:
            user_tasks[t.assignee].append(f"• {t.title} ({t.status})")
        for user, user_task_list in user_tasks.items():
            user_mention = f"@{user}" if user != "Не_назначен" else "📥 **Бэклог**"
            report += f"👤 {user_mention}:\n" + "\n".join(user_task_list) + "\n\n"
        await callback.message.answer(report, parse_mode="Markdown")

@dp.callback_query(F.data == "my_tasks")
async def my_tasks_callback(callback: types.CallbackQuery):
    username = callback.from_user.username
    if not username:
        await callback.message.answer("Установите username в Telegram.")
        return
    async with async_session() as session:
        res = await session.execute(select(Task).where(Task.assignee == username, Task.status != 'done').order_by(Task.deadline.asc()))
        tasks = res.scalars().all()
        if not tasks:
            await callback.message.answer("🎉 У вас нет активных задач!")
            return
        report = "📋 **Ваши задачи:**\n\n"
        for t in tasks:
            mark = "🔥 " if t.priority == "high" else ""
            dl = f" (до {t.deadline.strftime('%d.%m')})" if t.deadline else ""
            report += f"• {mark}**{t.title}**{dl}\n  Статус: {t.status}\n"
        report += f"\n🌐 [Открыть Канбан](http://{VPS_IP}:8000/kanban)"
        await callback.message.answer(report, parse_mode="Markdown", disable_web_page_preview=True)
    await callback.answer()

# --- ФОН ---
async def check_deadlines():
    global active_bot
    while True:
        await asyncio.sleep(300) # 5 минут для демо
        if not active_bot: continue
        now = datetime.now()
        soon = now + timedelta(hours=2)
        async with async_session() as session:
            res = await session.execute(select(Task).where(Task.deadline != None, Task.deadline > now, Task.deadline <= soon, Task.status != 'done'))
            for task in res.scalars().all():
                if task.chat_id:
                    try:
                        mention = f"@{task.assignee}" if task.assignee != "Не_назначен" else "Не назначен"
                        await active_bot.send_message(chat_id=task.chat_id, text=f"🔥 **НАПОМИНАНИЕ!**\nЗадача: *{task.title}*\nОтветственный: {mention}\nДедлайн: {task.deadline.strftime('%d.%m в %H:%M')} МСК", parse_mode="Markdown")
                    except: pass

async def start_polling():
    bot = await get_bot()
    if bot:
        asyncio.create_task(check_deadlines())
        print("✅ Бот запущен (демо-режим: 5 мин).")
        await dp.start_polling(bot)
    else:
        print("❌ Токен бота не найден!")
