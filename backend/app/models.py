from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, Text, select, update, delete, DateTime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
import os
from datetime import datetime

Base = declarative_base()

class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    username = Column(String, unique=True)
    role = Column(String)
    role_prompt = Column(Text, default="Ты эксперт. Дай краткий совет.")
    xp = Column(Integer, default=0)
    level = Column(Integer, default=1)

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    description = Column(Text, nullable=True)
    assignee = Column(String)
    status = Column(String, default="todo")
    ai_advice = Column(Text, nullable=True)
    deadline = Column(DateTime, nullable=True)
    chat_id = Column(String, nullable=True)

class SystemSetting(Base):
    __tablename__ = "system_settings"
    key_name = Column(String, primary_key=True)
    value = Column(String)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@db:5432/pm_bot")
engine = create_async_engine(DATABASE_URL)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)