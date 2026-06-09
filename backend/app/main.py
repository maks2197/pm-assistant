import os
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update, delete
from aiogram import Bot
from aiogram.enums import ParseMode

from .models import engine, Base, async_session, Task, SystemSetting, TeamMember
from .bot import start_polling
from .ai_logic import get_ai_advice

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(start_polling())

@app.get("/kanban", response_class=HTMLResponse)
async def kanban(request: Request):
    async with async_session() as session:
        t = await session.execute(select(Task).order_by(Task.id.desc()))
        m = await session.execute(select(TeamMember).order_by(TeamMember.xp.desc()))
        return templates.TemplateResponse(request, "kanban.html", {"tasks": t.scalars().all(), "members": m.scalars().all()})

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    async with async_session() as session:
        m = await session.execute(select(TeamMember))
        s = await session.execute(select(SystemSetting))
        settings = {row.key_name: row.value for row in s.scalars().all()}
        return templates.TemplateResponse(request, "admin.html", {"members": m.scalars().all(), "settings": settings})

@app.post("/api/admin/settings")
async def settings(data: dict = Body(...)):
    async with async_session() as session:
        for k, v in data.items():
            if v: await session.merge(SystemSetting(key_name=k, value=str(v)))
        await session.commit()
    return {"ok": True}

@app.post("/api/team/upsert")
async def upsert_member(data: dict = Body(...)):
    username = data['username'].replace("@", "").strip()
    async with async_session() as session:
        res = await session.execute(select(TeamMember).where(TeamMember.username == username))
        member = res.scalars().first()
        if member:
            member.name, member.role, member.role_prompt = data['name'], data['role'], data['prompt']
        else:
            session.add(TeamMember(name=data['name'], username=username, role=data['role'], role_prompt=data['prompt']))
        await session.commit()
    return {"ok": True}

@app.post("/api/tasks/update")
async def update_task(data: dict = Body(...)):
    async with async_session() as session:
        task_id = int(data['id'])
        deadline_str = data.get('deadline')
        deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M") if deadline_str and str(deadline_str).strip() else None
        await session.execute(update(Task).where(Task.id == task_id).values(
            title=data.get('title', ''), assignee=data.get('assignee', '').replace("@", ""),
            description=data.get('description', ''), deadline=deadline_dt
        ))
        await session.commit()
    return {"ok": True}

@app.post("/api/tasks/get_advice")
async def get_task_advice(data: dict = Body(...)):
    task_id = int(data['id'])
    async with async_session() as session:
        res = await session.execute(select(Task).where(Task.id == task_id))
        task = res.scalars().first()
        if not task: return {"error": "Not found"}
        if task.ai_advice: return {"advice": task.ai_advice}
        advice = await get_ai_advice(task.title, task.assignee)
        await session.execute(update(Task).where(Task.id == task_id).values(ai_advice=advice))
        await session.commit()
        return {"advice": advice}

@app.post("/api/tasks/move")
async def move_task(data: dict = Body(...)):
    task_id = int(data['id'])
    new_status = data['status']
    
    async with async_session() as session:
        res = await session.execute(select(Task).where(Task.id == task_id))
        task = res.scalars().first()
        await session.execute(update(Task).where(Task.id == task_id).values(status=new_status))
        await session.commit()

    # 🔥 ОТПРАВКА УВЕДОМЛЕНИЯ О ПЕРЕНОСЕ
    if task and task.chat_id:
        try:
            async with async_session() as session:
                s_res = await session.execute(select(SystemSetting).where(SystemSetting.key_name == "telegram_token"))
                token = (s_res.scalars().first().value) if s_res.scalars().first() else os.getenv("TELEGRAM_TOKEN")
            if token:
                bot = Bot(token=token)
                status_map = {"backlog": "📥 В Бэклог", "todo": "📌 В работу", "progress": "⚡ Продолжена", "done": "✅ Завершена"}
                await bot.send_message(chat_id=task.chat_id, text=f"🔄 **Статус изменен!**\nЗадача: *{task.title}*\nНовый статус: {status_map.get(new_status, new_status)}", parse_mode=ParseMode.MARKDOWN)
                await bot.session.close()
        except Exception as e:
            print(f"Ошибка уведомления: {e}")
    return {"ok": True}

@app.post("/api/tasks/delete")
async def delete_task(data: dict = Body(...)):
    async with async_session() as session:
        await session.execute(delete(Task).where(Task.id == int(data['id'])))
        await session.commit()
    return {"ok": True}
