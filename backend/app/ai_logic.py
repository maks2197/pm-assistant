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
        return "⚠️ ИИ не настроен в админке. Укажите API ключ."
    
    provider = cfg.get("ai_provider") or "vsegpt"
    model_name = cfg.get("model_name") or "openai/gpt-4o-mini"
    
    base_urls = {
        "vsegpt": "https://api.vsegpt.ru/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "openai": None
    }

    async with async_session() as session:
        res = await session.execute(select(TeamMember).where(TeamMember.username == assignee_username))
        member = res.scalars().first()
        sys_prompt = member.role_prompt if member else "Ты ассистент команды, давай краткие и полезные советы."

    client = openai.AsyncOpenAI(
        api_key=key,
        base_url=base_urls.get(provider),
        default_headers={"HTTP-Referer": "http://localhost", "X-Title": "AI PM"}
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
        # Добавляем информацию о модели в конец ответа
        return f"{advice_text}\n\n_*(Модель: {model_name})*_"
    except Exception as e:
        return f"❌ Ошибка ИИ: {str(e)}"
