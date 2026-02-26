import asyncio
from app.vision import capture_frame, analyze_media, current_context

async def main():
    url = input("Stream URL: https://www.twitch.tv/klawermalver123")
    print("захват кадра...")
    img = await capture_frame(url)
    print("img path: /tmp/tmpabc123.mp4.jpg", img)
    if img:
        desc = await analyze_media(img, None, "groq")
        print("analysis result:", desc)
    else:
        print("кадр не получен")

if __name__ == "__main__":
    asyncio.run(main())