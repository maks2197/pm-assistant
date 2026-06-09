import openai
from .models import async_session, SystemSetting, select, TeamMember

async def get_config():
    async with async_session() as session:
        res = await session.execute(select(SystemSetting))
        return {s.key_name: s.value for s in res.scalars().all()}

async def get_ai_advice(task_title, assignee_username):
    cfg = await get_config()
    key = cfg.get("ai_api_key")
    if not key:
        return "⚠️ ИИ не настроен. Пожалуйста, добавьте API ключ в админке."
    
    provider = cfg.get("ai_provider") or "vsegpt"
    model_name = cfg.get("model_name") or "openai/gpt-4o-mini"
    
    base_urls = {
        "vsegpt": "https://api.vsegpt.ru/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "openai": None
    }

    # 1. Получаем данные участника для персонализации промпта
    async with async_session() as session:
        res = await session.execute(select(TeamMember).where(TeamMember.username == assignee_username))
        member = res.scalars().first()
        
        if member:
            # Формируем мощный системный промпт на основе роли и инструкции из админки
            sys_prompt = (
                f"Ты выступаешь в роли: {member.role} по имени {member.name}. "
                f"Твоя главная инструкция и стиль работы: {member.role_prompt}. "
                f"Проанализируй задачу и дай краткий, профессиональный совет, строго соответствуя своей роли и экспертизе."
            )
            role_name = member.role
        else:
            # Фоллбэк, если участник не найден в базе
            sys_prompt = "Ты опытный AI Project Manager. Дай краткий, структурированный и полезный совет по выполнению задачи."
            role_name = "Project Manager"

    client = openai.AsyncOpenAI(
        api_key=key,
        base_url=base_urls.get(provider),
        default_headers={"HTTP-Referer": "http://localhost", "X-Title": "AI PM Assistant"}
    )

    try:
        res = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Задача: {task_title}"}
            ],
            max_tokens=300
        )
        advice_text = res.choices[0].message.content
        
        # 2. Добавляем красивый футер для демо, показывающий персонализацию
        footer = f"\n\n_*(💡 Совет сгенерирован с учетом роли: **{role_name}**. Модель: {model_name})*_"
        
        return f"{advice_text}{footer}"
        
    except Exception as e:
        return f"❌ Ошибка ИИ: {str(e)}"
