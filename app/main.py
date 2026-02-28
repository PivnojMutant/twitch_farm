import os
import asyncio
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from models import Base, engine, SessionLocal, Account, APIKey
from sqlalchemy import select
from bot_logic import launch_bots
from vision import observer_loop
from logger import setup_logging, log_buffer
import logging
from collections import deque

setup_logging()
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/data", StaticFiles(directory="data"), name="data")

STREAM_TASK = None
BOT_TASK = None
CURRENT_PROVIDER = "groq"
SEND_CHAT = True
CAPTURE_AUDIO = True
CAPTURE_VIDEO = True
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

# ЖЕЛЕЗОБЕТОННЫЙ ПЕРЕХВАТЧИК ФОРМЫ (без падений 422)
@app.post("/start")
async def start(request: Request):
    global STREAM_TASK, BOT_TASK, CURRENT_PROVIDER
    global SEND_CHAT, CAPTURE_AUDIO, CAPTURE_VIDEO
    
    try:
        form = await request.form()
        stream_url = form.get("stream_url", "")
        channel = form.get("channel", "")
        provider = form.get("provider", "groq")
        
        is_send_chat = form.get("send_chat") is not None
        is_cap_audio = form.get("capture_audio") is not None
        is_cap_vid = form.get("capture_video") is not None

        CURRENT_PROVIDER = provider
        SEND_CHAT = is_send_chat
        CAPTURE_AUDIO = is_cap_audio
        CAPTURE_VIDEO = is_cap_vid

        logger.info(f"⚙️ ПОПЫТКА ЗАПУСКА: Канал={channel}, Чат={is_send_chat}")

        if STREAM_TASK:
            STREAM_TASK.cancel()
        if BOT_TASK:
            BOT_TASK.cancel()

        STREAM_TASK = asyncio.create_task(observer_loop(stream_url, provider, is_cap_audio, is_cap_vid))

        async with SessionLocal() as session:
            result = await session.execute(select(Account))
            all_accounts = result.scalars().all()
            active_accounts = [acc for acc in all_accounts if acc.is_active]

        if active_accounts and is_send_chat:
            logger.info("✅ Передаю команду на запуск ботов...")
            BOT_TASK = asyncio.create_task(launch_bots(active_accounts, channel, provider, is_send_chat))
        else:
            logger.warning(f"🔴 БОТЫ НЕ ЗАПУЩЕНЫ! Активных: {len(active_accounts)}, Галочка чата: {is_send_chat}")

    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ: {e}", exc_info=True)
        
    return RedirectResponse("/", status_code=303)


async def send_twitch_irc(account, channel: str, message: str):
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
    logger.info("🛑 СИСТЕМА ОСТАНОВЛЕНА")
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
            result = await session.execute(select(Account).where(Account.username == username))
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
