import asyncio
import random
from twitchio.ext import commands
from humanize import apply_typos, typing_delay, PERSONALITIES
from ai_clients import call_groq
from vision import current_context

class GhostBot(commands.Bot):
    def __init__(self, account, channel):
        super().__init__(
            token=account.oauth_token,
            prefix="!",
            initial_channels=[channel]
        )
        self.account = account

    async def event_message(self, message):
        if message.echo:
            return

        if random.random() > 0.3:
            return

        prompt = f"""
        Контекст стрима: {current_context}
        Последнее сообщение: {message.content}
        Личность: {PERSONALITIES[self.account.personality]}
        Ответь коротко как человек.
        """

        response = await call_groq(prompt)
        response = apply_typos(response)

        await typing_delay(response)
        await message.channel.send(response)

async def launch_bots(accounts, channel):
    tasks = []
    for acc in accounts:
        bot = GhostBot(acc, channel)
        tasks.append(asyncio.create_task(bot.start()))
    await asyncio.gather(*tasks)
