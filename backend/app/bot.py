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
        res = await session.execute(
            select(SystemSetting).where(SystemSetting.key_name == "telegram_token")
        )
        s = res.scalars().first()
        token = s.value if s else os.getenv("TELEGRAM_TOKEN")
        if token:
            active_bot = Bot(token=token)
            return active_bot
    return None


@dp.message(Command("start"))
async def start(m: types.Message):
    if m.chat.type == ChatType.PRIVATE:
        await m.answer(
            "👋 Привет! Напиши мне задачу и дедлайн.\n"
            "Пример: *Подготовить отчет до 25.10 15:00*\n"
            "Или просто задачу без дедлайна: *Подготовить отчет*",
            parse_mode="Markdown"
        )
    else:
        await m.answer(
            "👋 AI PM Система Активна!\n\n"
            "Пиши: `@ник задача до ДД.ММ` или `до ДД.ММ ЧЧ:ММ`\n"
            "Пример: @ivan сделать дизайн до 25.10 18:00",
            parse_mode="Markdown"
        )


async def process_task(m: types.Message, assignee: str, title: str):
    print(f"=== НАЧАЛО ОБРАБОТКИ ЗАДАЧИ ===")
    print(f"Ответственный: {assignee}")
    print(f"Текст задачи: {title}")

    deadline_match = re.search(
        r'(?:до|дедлайн)[:\s]+(\d{2}\.\d{2})(?:\s+(\d{2}:\d{2}))?',
        title,
        re.IGNORECASE
    )
    deadline_dt = None
    clean_title = title

    if deadline_match:
        print(f"✅ Найден дедлайн: {deadline_match.group(0)}")
        day, month = map(int, deadline_match.group(1).split('.'))
        time_str = deadline_match.group(2)
        year = datetime.now().year

        if time_str:
            hour, minute = map(int, time_str.split(':'))
            deadline_dt = datetime(year, month, day, hour, minute, tzinfo=MSK_TZ)
            print(f"⏰ Время указано: {hour}:{minute} МСК")
        else:
            deadline_dt = datetime(year, month, day, 18, 0, tzinfo=MSK_TZ)
            print(f"⏰ Время не указано, устанавливаем 18:00 МСК")

        clean_title = re.sub(
            r'(?:до|дедлайн)[:\s]+\d{2}\.\d{2}(?:\s+\d{2}:\d{2})?',
            '',
            title,
            flags=re.IGNORECASE
        ).strip()
        print(f"🧹 Очищенный текст задачи: {clean_title}")
    else:
        print("❌ Дедлайн не найден в тексте")

    # КРИТИЧЕСКИ ВАЖНО: убираем часовой пояс перед сохранением в базу
    if deadline_dt:
        deadline_dt = deadline_dt.replace(tzinfo=None)
        print(f"💾 Сохраняем дедлайн (без TZ): {deadline_dt}")

    try:
        async with async_session() as session:
            new_task = Task(
                title=clean_title,
                assignee=assignee,
                deadline=deadline_dt,
                chat_id=str(m.chat.id)
            )
            session.add(new_task)
            await session.commit()
            print(f"✅ Задача успешно создана с ID: {new_task.id}")
    except Exception as e:
        print(f"❌ ОШИБКА ПРИ СОЗДАНИИ ЗАДАЧИ: {e}")
        import traceback
        traceback.print_exc()
        await m.reply(f"❌ Ошибка при создании задачи: {e}")
        return

    # Начисляем XP тому, кто поставил задачу
    if m.from_user.username:
        async with async_session() as session:
            res = await session.execute(
                select(TeamMember).where(TeamMember.username == m.from_user.username)
            )
            user = res.scalars().first()
            if user:
                user.xp += 10
                await session.commit()

    # Отправляем подтверждение
    if deadline_dt:
        deadline_text = f" (дедлайн: {deadline_dt.strftime('%d.%m %H:%M')} МСК)"
    else:
        deadline_text = " (без дедлайна)"

    await m.reply(
        f"✅ Задача для @{assignee} добавлена на доску!{deadline_text}\n\n"
        f"Совет ИИ можно получить на Канбан-доске."
    )


@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_private_text(m: types.Message):
    print(f"!!! ПОЛУЧЕНО ЛИЧНОЕ СООБЩЕНИЕ: {m.text}")
    if m.text.startswith("/"):
        return
    assignee = m.from_user.username or "unknown"
    await process_task(m, assignee, m.text)


@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]), F.text)
async def handle_group_text(m: types.Message):
    print(f"!!! ПОЛУЧЕНО СООБЩЕНИЕ В ГРУППЕ: {m.text}")
    if m.text.startswith("/"):
        return

    match = re.search(r'@(\w+)', m.text)
    if not match:
        print("!!! НЕ НАЙДЕН УПОМИНАНИЕ @nik")
        return

    assignee = match.group(1)
    title = m.text.replace(f"@{assignee}", "").strip()
    if not title:
        return

    await process_task(m, assignee, title)


async def check_deadlines():
    global active_bot
    while True:
        await asyncio.sleep(3600)  # Проверка каждый час
        if not active_bot:
            continue

        # КРИТИЧЕСКИ ВАЖНО: используем naive datetime (без часового пояса)
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
        print("✅ Бот запущен и начал polling")
        await dp.start_polling(bot)
    else:
        print("❌ Токен бота не найден! Проверьте .env файл")
