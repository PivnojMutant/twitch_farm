import os
import asyncio
from app.vision import capture_frame, analyze_media, current_context

async def main():
    url = input("Stream URL: https://www.twitch.tv/klawermalver123")
    print("захват кадра...")
    img = await capture_frame(url)
    print("img path:", img)
    debug_dir = os.getenv("DEBUG_FRAMES_DIR")
    if debug_dir:
        print(f"(если установлен DEBUG_FRAMES_DIR, копия кадра сохраняется в {debug_dir})")
    if img:
        desc = await analyze_media(img, None, "groq")
        print("analysis result:", desc)
    else:
        print("кадр не получен")

if __name__ == "__main__":
    asyncio.run(main())