import streamlink
import cv2
import asyncio
import tempfile
from ai_clients import call_groq

current_context = "Стрим не запущен."

async def capture_frame(url: str):
    streams = streamlink.streams(url)
    stream = streams["best"]
    fd, path = tempfile.mkstemp(suffix=".mp4")

    with stream.open() as s, open(path, "wb") as f:
        f.write(s.read(1024 * 1024))

    cap = cv2.VideoCapture(path)
    ret, frame = cap.read()
    cap.release()
    if ret:
        img_path = path + ".jpg"
        cv2.imwrite(img_path, frame)
        return img_path
    return None

async def observer_loop(url: str):
    global current_context
    while True:
        try:
            img = await capture_frame(url)
            if img:
                prompt = "Опиши кратко, что происходит на стриме."
                current_context = await call_groq(prompt)
        except Exception as e:
            current_context = f"Ошибка наблюдателя: {e}"

        await asyncio.sleep(15)
