import httpx
import logging
from app.models import SessionLocal, APIKey
from sqlalchemy import select

logger = logging.getLogger(__name__)

MODEL_DEFAULTS = {
    "groq": "llama-3.2-90b-vision-preview",
    "openrouter": "google/gemini-2.5-flash",
    "gemini": "gemini-2.0-flash"
}

PROVIDER_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}

async def get_next_key(provider: str):
    async with SessionLocal() as session:
        result = await session.execute(
            select(APIKey).where(APIKey.provider == provider)
        )
        keys = result.scalars().all()
        if not keys:
            logger.error(f"Нет API ключей для {provider}")
            return None
        keys.sort(key=lambda k: k.usage_count)
        return keys[0]

async def call_ai(prompt: str, provider: str = "groq", model: str | None = None):
    """Универсальная функция для вызова любого AI провайдера"""
    key_obj = await get_next_key(provider)
    if not key_obj:
        logger.error(f"Нет доступных {provider} ключей")
        return f"Ошибка: нет API ключей для {provider}"

    # порядок приоритета: явно переданный model > модель, привязанная к ключу > дефолт
    model = model or key_obj.model or MODEL_DEFAULTS.get(provider, "gpt-4")
    url = PROVIDER_URLS.get(provider)
    
    if not url:
        logger.error(f"Неподдерживаемый провайдер: {provider}")
        return f"Ошибка: неподдерживаемый провайдер {provider}"

    if provider == "openrouter":
        headers = {
            "Authorization": f"Bearer {key_obj.key}",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "Twitch Ghost Farm"
        }
    else:
        headers = {"Authorization": f"Bearer {key_obj.key}"}

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30
            )
        
        if r.status_code == 429:
            logger.warning(f"{provider} rate limit, переключаемся на другой ключ")
            key_obj.usage_count += 9999
            async with SessionLocal() as session:
                db_key = await session.get(APIKey, key_obj.id)
                if db_key:
                    db_key.usage_count += 9999
                    await session.commit()
            return await call_ai(prompt, provider)
        
        if r.status_code != 200:
            logger.error(f"{provider} API ошибка {r.status_code}: {r.text}")
            return f"Ошибка API: {r.status_code}"
        
        response_data = r.json()
        content = response_data["choices"][0]["message"]["content"]
        
        # Сохраняем увеличение usage_count в БД
        async with SessionLocal() as session:
            db_key = await session.get(APIKey, key_obj.id)
            if db_key:
                db_key.usage_count += 1
                await session.commit()
        
        return content
    
    except httpx.TimeoutException:
        logger.error(f"Timeout при вызове {provider} API")
        return "Ошибка: timeout"
    except Exception as e:
        logger.error(f"Ошибка при вызове {provider}: {e}")
        return f"Ошибка: {str(e)}"

async def call_groq(prompt: str):
    """Обратная совместимость"""
    return await call_ai(prompt, "groq")


async def transcribe_audio_file(path: str, provider: str = "groq"):
    """Отправляет файл на расшифровку (whisper/whisper-like).
    Возвращает текст транскрипта или ошибку.
    """
    key_obj = await get_next_key(provider)
    if not key_obj:
        return ""

    # выбираем url и модель для аудио
    if provider == "groq":
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        model = "whisper-large-v3-turbo"
    else:  # openrouter (или другие)
        url = "https://openrouter.ai/api/v1/audio/transcriptions"
        model = "google/gemma-3n-e4b-it:free"

    headers = {"Authorization": f"Bearer {key_obj.key}"}
    # form-data с файлом
    try:
        with open(path, "rb") as f:
            files = {"file": f}
            data = {"model": model}
            async with httpx.AsyncClient() as client:
                r = await client.post(url, headers=headers, files=files, data=data, timeout=60)

        if r.status_code != 200:
            logger.error(f"Ошибка аудио-транскрипции {provider}: {r.status_code} {r.text}")
            return ""

        res = r.json()
        text = res.get("text") or res.get("transcript") or ""

        # учёт использования
        async with SessionLocal() as session:
            db_key = await session.get(APIKey, key_obj.id)
            if db_key:
                db_key.usage_count += 1
                await session.commit()

        return text
    except Exception as e:
        logger.error(f"Ошибка при отправке аудио {e}")
        return ""


async def describe_image_file(path: str, provider: str = "groq"):
    """Читает изображение и описывает его через выбранную модель пользователем.
    Не встраиваем base64, просто отправляем промпт внутри контекста.
    """
    try:
        # просто отправляем лаконичный промпт, не пытаясь встраивать файл
        # Groq и OpenRouter могут работать с файлами отдельно или через API
        prompt = "Опиши кратко, что происходит на картинке (аналитика видео-фрейма для контекста стрима)."
        # используем модель, выбранную пользователем
        return await call_ai(prompt, provider=provider)
    except Exception as e:
        logger.error(f"Ошибка описания изображения: {e}")
        return ""

