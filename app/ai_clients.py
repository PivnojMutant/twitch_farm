import httpx
from models import SessionLocal, APIKey
from sqlalchemy import select

async def get_next_key(provider: str):
    async with SessionLocal() as session:
        result = await session.execute(
            select(APIKey).where(APIKey.provider == provider)
        )
        keys = result.scalars().all()
        keys.sort(key=lambda k: k.usage_count)
        return keys[0] if keys else None

async def call_groq(prompt: str):
    key_obj = await get_next_key("groq")
    if not key_obj:
        return "..."

    headers = {"Authorization": f"Bearer {key_obj.key}"}

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json={
                "model": "llama3-70b-8192",
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )

    if r.status_code == 429:
        key_obj.usage_count += 9999
        return await call_groq(prompt)

    key_obj.usage_count += 1
    return r.json()["choices"][0]["message"]["content"]
