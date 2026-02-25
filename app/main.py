import os
import asyncio
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv
from models import Base, engine, SessionLocal, Account
from sqlalchemy import select
from bot_logic import launch_bots
from vision import observer_loop

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

STREAM_TASK = None
BOT_TASK = None

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/start")
async def start(stream_url: str = Form(...)):
    global STREAM_TASK, BOT_TASK

    STREAM_TASK = asyncio.create_task(observer_loop(stream_url))

    async with SessionLocal() as session:
        result = await session.execute(
            select(Account).where(Account.is_active == True)
        )
        accounts = result.scalars().all()

    BOT_TASK = asyncio.create_task(launch_bots(accounts, "target_channel"))

    return RedirectResponse("/", status_code=303)

@app.post("/stop")
async def stop():
    global STREAM_TASK, BOT_TASK
    if STREAM_TASK:
        STREAM_TASK.cancel()
    if BOT_TASK:
        BOT_TASK.cancel()
    return RedirectResponse("/", status_code=303)
