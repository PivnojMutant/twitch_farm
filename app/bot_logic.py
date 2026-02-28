import asyncio
import random
import logging
from twitchio.ext import commands
from humanize import apply_typos, typing_delay, PERSONALITIES
from ai_clients import call_ai
import vision

logger = logging.getLogger(__name__)

class GhostBot(commands.Bot):
    def __init__(self, account, channel, provider="groq", send_chat=True):
        # Очистка токена
        token = account.oauth_token.strip()
        if not token.startswith('oauth:'):
            token = f"oauth:{token}"
        clean_channel = channel.replace('#', '').strip().lower()

        super().__init__(
            token=token,
            prefix="!",
            initial_channels=[clean_channel]
        )
        self.account = account
        self.provider = provider
        self.send_chat = send_chat
        self._announce_task = None

    async def event_ready(self):
        logger.info(f"🟢 БОТ {self.account.username} успешно вошел на Twitch!")
        if not self._announce_task:
            self._announce_task = asyncio.create_task(self.periodic_chat())

    async def event_message(self, message):
        if not self.send_chat or message.echo or random.random() > 0.15:
            return

        personality_desc = PERSONALITIES.get(self.account.personality, PERSONALITIES["viewer"])
        
        prompt = f"""
        Ты — персонаж в чате Twitch со следующим характером: {personality_desc}.
        Стрим сейчас: {vision.current_context}.
        Зритель {message.author.name} написал: "{message.content}".

        ЗАДАЧА: Напиши ответ длиной 1-5 слов.
        ПРАВИЛА:
        1. Только строчные буквы.
        2. Никаких знаков препинания в конце (никаких точек!).
        3. Никаких эмодзи и кавычек.
        4. Начни ответ с обращения: {message.author.name}
        """

        response = await call_ai(prompt, provider=self.provider)
        if not response or "Ошибка" in response:
            return

        response = response.replace('"', '').replace('*', '').strip('.!?').lower()
        response = apply_typos(response)

        logger.info(f"💬 [ОТВЕТ В ЧАТ] {self.account.username}: {response}")
        await typing_delay(response)
        await message.channel.send(response)

    async def periodic_chat(self):
        await self.wait_for_ready()
        logger.info(f"🟡 Таймер для {self.account.username} запущен!")
        
        while True:
            try:
                await asyncio.sleep(random.randint(45, 120))
                
                if self.connected_channels:
                    channel = self.connected_channels[0]
                    prompt = f"""
                    Ты зритель на Twitch-стриме.
                    Контекст на экране: {vision.current_context}
                    
                    ЗАДАЧА: Написать свои мысли в чат (1-5 слов).
                    ПРАВИЛА: Только строчные буквы, без точек, без кавычек, максимально лениво.
                    """
                    
                    msg = await call_ai(prompt, provider=self.provider)
                    
                    if not msg or "Ошибка" in msg:
                        continue
                        
                    msg = msg.replace('"', '').replace('*', '').strip('.!?').lower()
                    msg = apply_typos(msg)
                    
                    logger.info(f"🕰️ [САМ ПО СЕБЕ] {self.account.username}: {msg}")
                    await channel.send(msg)
                else:
                    logger.warning(f"🔴 ОШИБКА: {self.account.username} не видит канал!")
                    
            except Exception as e:
                logger.error(f"❌ Ошибка в periodic_chat: {e}")

async def launch_bots(accounts, channel, provider="groq", send_chat=True):
    logger.info(f"🚀 Запуск ботов инициирован! Аккаунтов: {len(accounts)}")
    try:
        tasks = []
        for acc in accounts:
            bot = GhostBot(acc, channel, provider=provider, send_chat=send_chat)
            tasks.append(asyncio.create_task(bot.start()))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.error(f"❌ Полный провал запуска ботов: {e}")
