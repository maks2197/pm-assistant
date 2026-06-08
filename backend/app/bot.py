import os
import asyncio
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from .models import async_session, SystemSetting, select, Task, TeamMember
from .ai_logic import get_ai_advice

dp = Dispatcher()
active_bot = None
MSK_TZ = ZoneInfo("Europe/Moscow")

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

@dp.message(Command("start"))
async def start(m: types.Message):
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("👋 Привет! Напиши мне задачу.\nПример: *Сделать деплой фронта до пятницы, критично для демо*", parse_mode="Markdown")
    else:
        await m.answer("👋 AI PM Система Активна!\n\nПиши: `@ник задача до ДД.ММ` или `до дня недели`\nПример: @ivan сделать деплой до пятницы, критично", parse_mode="Markdown")

async def process_task(m: types.Message, assignee: str, title: str):
    print(f"=== ОБРАБОТКА: {title} ===")
    
    # 1. Определение приоритета
    priority = "normal"
    if re.search(r'\b(критично|срочно|важно|high|urgent|асап)\b', title, re.IGNORECASE):
        priority = "high"
        print("🔥 Обнаружен высокий приоритет!")

    # 2. Парсинг дедлайна (поддержка "до пятницы" и "до 12.06")
    deadline_dt = None
    clean_title = title
    
    # Попытка найти день недели
    days_map = {'понедельник': 0, 'вторник': 1, 'среда': 2, 'четверг': 3, 'пятница': 4, 'суббота': 5, 'воскресенье': 6}
    day_match = re.search(r'(?:до|дедлайн)[:\s]+(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)', title, re.IGNORECASE)
    
    if day_match:
        day_name = day_match.group(1).lower()
        target_weekday = days_map[day_name]
        today = datetime.now(MSK_TZ)
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0: # Если день уже прошел на этой неделе, берем следующую
            days_ahead += 7
        deadline_dt = (today + timedelta(days=days_ahead)).replace(hour=18, minute=0, second=0, microsecond=0)
        clean_title = re.sub(r'(?:до|дедлайн)[:\s]+' + day_name, '', title, flags=re.IGNORECASE).strip()
    else:
        # Стандартный парсинг ДД.ММ
        date_match = re.search(r'(?:до|дедлайн)[:\s]+(\d{2}\.\d{2})(?:\s+(\d{2}:\d{2}))?', title, re.IGNORECASE)
        if date_match:
            day, month = map(int, date_match.group(1).split('.'))
            time_str = date_match.group(2)
            year = datetime.now().year
            if time_str:
                hour, minute = map(int, time_str.split(':'))
                deadline_dt = datetime(year, month, day, hour, minute, tzinfo=MSK_TZ)
            else:
                deadline_dt = datetime(year, month, day, 18, 0, tzinfo=MSK_TZ)
            clean_title = re.sub(r'(?:до|дедлайн)[:\s]+\d{2}\.\d{2}(?:\s+\d{2}:\d{2})?', '', title, flags=re.IGNORECASE).strip()

    # Убираем часовой пояс для БД
    if deadline_dt:
        deadline_dt = deadline_dt.replace(tzinfo=None)

    try:
        async with async_session() as session:
            new_task = Task(
                title=clean_title, 
                assignee=assignee, 
                priority=priority,
                deadline=deadline_dt, 
                chat_id=str(m.chat.id)
            )
            session.add(new_task)
            await session.commit()
            print(f"✅ Задача создана. Приоритет: {priority}")
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
        await m.reply(f"❌ Ошибка при создании задачи: {e}")
        return

    # Начисление XP
    if m.from_user.username:
        async with async_session() as session:
            res = await session.execute(select(TeamMember).where(TeamMember.username == m.from_user.username))
            user = res.scalars().first()
            if user:
                user.xp += 10
                await session.commit()

    # Ответ в чат
    deadline_text = f" (дедлайн: {deadline_dt.strftime('%d.%m %H:%M')} МСК)" if deadline_dt else ""
    priority_text = " 🔥 **КРИТИЧНО**" if priority == "high" else ""
    
    await m.reply(
        f"✅ Задача для @{assignee} добавлена на доску!{priority_text}{deadline_text}\n\n"
        f"Совет ИИ можно получить на Канбан-доске."
    )

@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_private_text(m: types.Message):
    if m.text.startswith("/"): return
    assignee = m.from_user.username or "unknown"
    await process_task(m, assignee, m.text)

@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.text)
async def handle_group_text(m: types.Message):
    if m.text.startswith("/"): return
    match = re.search(r'@(\w+)', m.text)
    if not match: return
    
    assignee = match.group(1)
    title = m.text.replace(f"@{assignee}", "").strip()
    if not title: return
    
    await process_task(m, assignee, title)

async def check_deadlines():
    global active_bot
    while True:
        # ⚠️ ДЛЯ ДЕМО: 300 секунд (5 минут). После хакатона поменяйте на 3600 (1 час)
        await asyncio.sleep(300) 
        if not active_bot: continue
            
        now = datetime.now()
        soon = now + timedelta(hours=2)
        
        async with async_session() as session:
            res = await session.execute(
                select(Task).where(
                    Task.deadline != None,
                    Task.deadline > now,
                    Task.deadline <= soon,
                    Task.status != 'done'
                )
            )
            tasks = res.scalars().all()
            
            for task in tasks:
                if task.chat_id:
                    try:
                        await active_bot.send_message(
                            chat_id=task.chat_id,
                            text=(
                                f"🔥 **НАПОМИНАНИЕ!**\n"
                                f"Задача: *{task.title}*\n"
                                f"Ответственный: @{task.assignee}\n"
                                f"Дедлайн: {task.deadline.strftime('%d.%m в %H:%M')} МСК\n\n"
                                f"Пожалуйста, обновите статус на доске!"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        print(f"Ошибка отправки напоминания: {e}")


async def start_polling():
    bot = await get_bot()
    if bot:
        asyncio.create_task(check_deadlines())
        print("✅ Бот запущен. Проверка дедлайнов каждые 5 минут (демо-режим).")
        await dp.start_polling(bot)
    else:
        print("❌ Токен бота не найден!")
