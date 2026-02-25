import random
import asyncio

PERSONALITIES = {
    "viewer": "Ты обычный зритель Twitch.",
    "gamer": "Ты активный геймер и разбираешься в игре.",
    "hater": "Ты слегка токсичный хейтер.",
    "polite": "Ты вежливый и позитивный зритель.",
    "troll": "Ты тролль, но без жёсткого нарушения правил."
}

def apply_typos(text: str) -> str:
    text = text.lower()
    if random.random() < 0.3:
        pos = random.randint(0, len(text)-1)
        text = text[:pos] + text[pos]*2 + text[pos+1:]
    return text

async def typing_delay(text: str):
    delay = len(text) * random.uniform(0.05, 0.15)
    await asyncio.sleep(delay)
