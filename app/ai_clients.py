import asyncio
import httpx
import logging
import base64
from models import SessionLocal, APIKey
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

async def call_ai(prompt: str | list, provider: str = "groq", model: str | None = None, *, attempts: int = 3):
    """Универсальная функция для вызова любого AI провайдера.

    Когда сервер возвращает 429 (rate limit), делает паузу и пробует еще раз
    на другом ключе. Ограничение по количеству попыток предотвращает бесконечный
    цикл; если исчерпаны, возвращается ошибка.
    """
    if attempts <= 0:
        logger.error(f"{provider} rate limit: исчерпаны попытки ({prompt[:40]}...)")
        return "Ошибка: превышен лимит запросов, попробуйте позже"

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
            # зафиксируем, что ключ исчерпан
            logger.warning(f"{provider} rate limit ({r.headers.get('Retry-After')}) на ключе {key_obj.id}")
            key_obj.usage_count += 9999
            async with SessionLocal() as session:
                db_key = await session.get(APIKey, key_obj.id)
                if db_key:
                    db_key.usage_count += 9999
                    await session.commit()
            # перед повтором даём серверу "остыть" (учитываем Retry-After, если есть)
            retry_after = 1
            try:
                retry_after = int(r.headers.get("Retry-After", retry_after))
            except Exception:
                pass
            await asyncio.sleep(retry_after)
            return await call_ai(prompt, provider, model, attempts=attempts-1)
        
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
    """Читает изображение, кодирует в Base64 и отправляет в Vision модель."""
    try:
        # Читаем файл с диска и кодируем в base64
        with open(path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        
        base64_image = f"data:image/jpeg;base64,{encoded_string}"

        # Формируем правильный промпт по документации Groq / OpenRouter
        vision_prompt = [
            {
                "type": "text",
                "text": "Опиши ОЧЕНЬ КРАТКО (1-2 предложения), что происходит на картинке (что за игра, что за аниме, что за музыка, если ничего из этого спроси что происходит). Не пиши лишних вступлений."
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": base64_image
                }
            }
        ]

        # Отправляем этот сложный промпт в нашу универсальную функцию
        return await call_ai(vision_prompt, provider=provider)
        
    except Exception as e:
        logger.error(f"Ошибка описания изображения: {e}")
        return ""

