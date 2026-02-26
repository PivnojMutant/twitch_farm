import asyncio
import random
from twitchio.ext import commands
from app.humanize import apply_typos, typing_delay, PERSONALITIES
from app.ai_clients import call_ai
from app.vision import current_context

class GhostBot(commands.Bot):
    def __init__(self, account, channel, provider="groq", send_chat=True):
        super().__init__(
            token=account.oauth_token,
            prefix="!",
            initial_channels=[channel]
        )
        self.account = account
        self.provider = provider
        self.send_chat = send_chat
        self._announce_task = None

    async def event_ready(self):
        # запускаем фоновую отправку сообщений
        if not self._announce_task:
            self._announce_task = asyncio.create_task(self.periodic_chat())

    async def event_message(self, message):
        if not self.send_chat:
            return

        if message.echo:
            return

        if random.random() > 0.3:
            return

        # Проверяем, существует ли personality
        personality_desc = PERSONALITIES.get(self.account.personality, PERSONALITIES["viewer"])
        
        prompt = f"""
        Контекст стрима: {current_context}
        Последнее сообщение: {message.content}
        Личность: {personality_desc}
        Ответь коротко как человек.
        Не используй форматирование, эмодзи или ссылки.
        Допускай ошибки в написании слов для большей человечности.
        Старайся не быть слишком умным, а просто поддержать беседу если это возможно.
        """

        response = await call_ai(prompt, provider=self.provider)
        response = apply_typos(response)

        await typing_delay(response)
        await message.channel.send(response)

    async def periodic_chat(self):
        """Редкие сообщения без запроса от пользователей."""
        # ждем, пока бот подключится
        await self.wait_for_ready()
        while True:
            await asyncio.sleep(random.randint(60, 180))
            if self.connected_channels:
                channel = self.connected_channels[0]
                prompt = f"Контекст стрима: {current_context}\nНапиши короткое приветственное или нейтральное сообщение в чат."
                msg = await call_ai(prompt, provider=self.provider)
                msg = apply_typos(msg)
                await channel.send(msg)

async def launch_bots(accounts, channel, provider="groq", send_chat=True):
    tasks = []
    for acc in accounts:
        bot = GhostBot(acc, channel, provider=provider, send_chat=send_chat)
        tasks.append(asyncio.create_task(bot.start()))
    await asyncio.gather(*tasks)
