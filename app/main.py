import os
import asyncio
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, JSONResponse
from dotenv import load_dotenv
from app.models import Base, engine, SessionLocal, Account, APIKey
from sqlalchemy import select
from app.bot_logic import launch_bots
from app.vision import observer_loop
from app.logger import setup_logging, log_buffer
import logging
from collections import deque

setup_logging()
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

STREAM_TASK = None
BOT_TASK = None
# выбранный провайдер на время сессии
CURRENT_PROVIDER = "groq"
# runtime flags
SEND_CHAT = True
CAPTURE_AUDIO = True
CAPTURE_VIDEO = True
# хранение истории сообщений в памяти (не для продакшена)
MESSAGE_HISTORY = deque(maxlen=200)

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get("/")
async def index(request: Request):
    async with SessionLocal() as session:
        acct_res = await session.execute(select(Account))
        accounts = acct_res.scalars().all()
        key_res = await session.execute(select(APIKey))
        api_keys = key_res.scalars().all()
    return templates.TemplateResponse("index.html", {"request": request, "accounts": accounts, "api_keys": api_keys})

@app.post("/start")
async def start(
    stream_url: str = Form(...),
    channel: str = Form(...),
    provider: str = Form("groq"),
    send_chat: bool = Form(False),
    capture_audio: bool = Form(True),
    capture_video: bool = Form(True),
):
    global STREAM_TASK, BOT_TASK, CURRENT_PROVIDER
    CURRENT_PROVIDER = provider

    logger.info(f"Запуск с URL: {stream_url}, канал: {channel}, провайдер: {provider}, send_chat={send_chat}, capture_audio={capture_audio}, capture_video={capture_video}")

    # сохраняем флаги в глобальные переменные
    global SEND_CHAT, CAPTURE_AUDIO, CAPTURE_VIDEO
    SEND_CHAT = send_chat
    CAPTURE_AUDIO = capture_audio
    CAPTURE_VIDEO = capture_video

    STREAM_TASK = asyncio.create_task(observer_loop(stream_url, provider, capture_audio, capture_video))  # flags passed as booleans

    async with SessionLocal() as session:
        result = await session.execute(
            select(Account).where(Account.is_active == True)
        )
        accounts = result.scalars().all()
        logger.info(f"Найдено аккаунтов: {len(accounts)}")

    if accounts and send_chat:
        BOT_TASK = asyncio.create_task(launch_bots(accounts, channel, provider, send_chat))
    else:
        logger.warning("Боты не будут запущены (либо нет аккаунтов, либо отправка чата отключена)")

    return RedirectResponse("/", status_code=303)


async def send_twitch_irc(account, channel: str, message: str):
    """Простой отправщик через Twitch IRC (однократное соединение).
    Использует oauth_token из account.oauth_token (формат oauth:...)
    """
    import asyncio
    host = "irc.chat.twitch.tv"
    port = 6667
    token = account.oauth_token
    nick = account.username
    chan = f"#{channel.lstrip('#')}"

    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(f"PASS {token}\r\n".encode())
        writer.write(f"NICK {nick}\r\n".encode())
        writer.write(f"JOIN {chan}\r\n".encode())
        await writer.drain()

        # небольшая пауза чтобы сервер обработал JOIN
        await asyncio.sleep(1)
        writer.write(f"PRIVMSG {chan} :{message}\r\n".encode())
        await writer.drain()

        await asyncio.sleep(0.5)
        writer.write(b"QUIT\r\n")
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке IRC: {e}")
        return False


@app.post("/send-message")
async def send_message(account_id: int = Form(...), channel: str = Form(...), message: str = Form(...)):
    try:
        async with SessionLocal() as session:
            acc = await session.get(Account, account_id)
            if not acc:
                logger.warning(f"Попытка отправки от несуществующего аккаунта: {account_id}")
                return RedirectResponse("/", status_code=303)

        ok = await send_twitch_irc(acc, channel, message)
        entry = {"account": acc.username, "channel": channel, "message": message, "success": ok}
        MESSAGE_HISTORY.append(entry)
        if ok:
            logger.info(f"Сообщение от {acc.username} в {channel}: {message}")
        else:
            logger.error(f"Не удалось отправить сообщение от {acc.username}")
    except Exception as e:
        logger.error(f"Ошибка в /send-message: {e}")

    return RedirectResponse("/", status_code=303)

@app.post("/stop")
async def stop():
    global STREAM_TASK, BOT_TASK
    if STREAM_TASK:
        STREAM_TASK.cancel()
    if BOT_TASK:
        BOT_TASK.cancel()
    return RedirectResponse("/", status_code=303)

@app.post("/add-api-key")
async def add_api_key(key: str = Form(...), provider: str = Form(...), model: str = Form(default="")):
    try:
        async with SessionLocal() as session:
            api_key = APIKey(key=key, provider=provider, model=model)
            session.add(api_key)
            await session.commit()
            logger.info(f"Добавлен API ключ для {provider}")
    except Exception as e:
        logger.error(f"Ошибка при добавлении API ключа: {e}")
    
    return RedirectResponse("/", status_code=303)

@app.post("/add-account")
async def add_account(
    username: str = Form(...),
    oauth_token: str = Form(...),
    personality: str = Form(...),
    proxy: str = Form(default=None)
):
    try:
        async with SessionLocal() as session:
            # Проверяем, не существует ли уже такой аккаунт
            result = await session.execute(
                select(Account).where(Account.username == username)
            )
            if result.scalars().first():
                logger.warning(f"Аккаунт {username} уже существует")
                return RedirectResponse("/", status_code=303)
            
            account = Account(
                username=username,
                oauth_token=oauth_token,
                personality=personality,
                proxy=proxy if proxy else None,
                is_active=True
            )
            session.add(account)
            await session.commit()
            logger.info(f"Добавлен аккаунт {username}")
    except Exception as e:
        logger.error(f"Ошибка при добавлении аккаунта: {e}")
    
    return RedirectResponse("/", status_code=303)

@app.get("/delete-account/{account_id}")
async def delete_account(account_id: int):
    try:
        async with SessionLocal() as session:
            account = await session.get(Account, account_id)
            if account:
                await session.delete(account)
                await session.commit()
                logger.info(f"Удален аккаунт {account.username}")
    except Exception as e:
        logger.error(f"Ошибка при удалении аккаунта: {e}")
    
    return RedirectResponse("/", status_code=303)

@app.get("/api/logs")
async def get_logs():
    """Возвращает последние логи в JSON формате"""
    return JSONResponse({"logs": log_buffer.get_logs()})

@app.get("/delete-api-key/{key_id}")
async def delete_api_key(key_id: int):
    try:
        async with SessionLocal() as session:
            key = await session.get(APIKey, key_id)
            if key:
                await session.delete(key)
                await session.commit()
                logger.info(f"Удалён API ключ {key_id}")
    except Exception as e:
        logger.error(f"Ошибка при удалении API ключа: {e}")
    return RedirectResponse("/", status_code=303)

@app.get("/api/messages")
async def get_messages():
    return JSONResponse({"messages": list(MESSAGE_HISTORY)})
