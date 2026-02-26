import streamlink
import asyncio
import tempfile
import logging
import shutil
import os
from app.ai_clients import call_ai, transcribe_audio_file, describe_image_file

# try to import cv2 but fall back if unavailable
try:
    import cv2
    _has_cv2 = True
except Exception:
    cv2 = None
    _has_cv2 = False
    logging.getLogger(__name__).warning("cv2 импортировать не удалось, кадры будут извлекаться через ffmpeg")

logger = logging.getLogger(__name__)
current_context = "Стрим не запущен."

async def capture_frame(url: str):
    """Захватывает один кадр из потока Twitch.
    Если cv2 доступен, используется VideoCapture, иначе ffmpeg.
    """
    logger.debug(f"capture_frame: пытаемся получить потоки {url}")
    try:
        streams = streamlink.streams(url)
    except Exception as e:
        logger.error(f"capture_frame: не удалось получить потоки: {e}")
        return None
    if not streams:
        logger.warning(f"capture_frame: потоков не найдено для {url}")
        return None
    stream = streams.get("best") or next(iter(streams.values()), None)
    if not stream:
        logger.warning(f"capture_frame: нет подходящего потока для {url}")
        return None

    # сначала попробуем сохранить небольшой кусок файла
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    try:
        with stream.open() as s, open(path, "wb") as f:
            chunk = s.read(1024 * 1024)
            f.write(chunk)
    except Exception as e:
        logger.error(f"capture_frame: ошибка при чтении потока: {e}")
        return None

    img_path = path + ".jpg"

    if _has_cv2:
        cap = cv2.VideoCapture(path)
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            cv2.imwrite(img_path, frame)
            logger.debug(f"capture_frame: кадр сохранён в {img_path} (cv2)")
            return img_path
        logger.warning(f"capture_frame: cv2 не смог прочитать кадр из {path}")
    
    # fallback: используем ffmpeg для извлечения кадра
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.error("capture_frame: ffmpeg не найден, невозможно извлечь кадр")
        return None
    try:
        # -y overwrite, -i input, -frames:v 1 один кадр, -q:v 2 качество
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", path,
            "-frames:v", "1", "-q:v", "2", img_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
            logger.debug(f"capture_frame: кадр сохранён в {img_path} (ffmpeg)")
            return img_path
        else:
            logger.warning(f"capture_frame: ffmpeg не создал изображение")
    except Exception as e:
        logger.error(f"capture_frame: ffmpeg ошибка: {e}")
    return None

async def capture_audio(url: str, duration: int = 10):
    """Записывает аудио поток из URL на duration секунд и возвращает путь к файлу.
    Если файл размером 0 байт, возвращает None.
    """
    import os
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)

    # ffmpeg может читать поток Twitch напрямую
    cmd = [
        "ffmpeg", "-y", "-i", url,
        "-t", str(duration),
        "-q:a", "0", path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if os.path.exists(path) and os.path.getsize(path) > 100:  # пусть хотя бы 100 байт
        return path
    # удаляем пустой файл
    try:
        os.remove(path)
    except Exception:
        pass
    return None


async def analyze_media(img_path: str, audio_path: str, provider: str):
    """Комбинирует описание изображения и транскрипт аудио и возвращает итоговый контекст/сообщение.
    Обрезает слишком длинный контекст, чтобы избежать ошибок API.
    """
    global current_context
    desc = ""
    text = ""
    if img_path:
        logger.debug(f"analyze_media: описываем изображение {img_path}")
        desc = await describe_image_file(img_path, provider)
        logger.debug(f"analyze_media: описание картинки: {desc!r}")
    if audio_path:
        logger.debug(f"analyze_media: транскрибируем аудио {audio_path}")
        text = await transcribe_audio_file(audio_path, provider)
        logger.debug(f"analyze_media: текст аудио: {text!r}")

    # формируем дополнительный кусок контекста из новых данных
    extra = ""
    if desc:
        extra += f"Описание кадра: {desc}\n"
    if text:
        extra += f"Транскрипт аудио: {text}\n"

    # создаём основной prompt, включая предыдущее состояние и новые сведения
    truncated_context = current_context
    if len(truncated_context) > 300:
        truncated_context = truncated_context[-300:]

    if extra:
        prompt = f"Контекст: {truncated_context}\n{extra}Напиши короткое сообщение в чат (макс 30 слов)."
    else:
        prompt = f"Контекст: {truncated_context}\nНапиши короткое сообщение в чат (макс 30 слов)."

    # лимит 1500 символов для максимальной совместимости
    if len(prompt) > 1500:
        prompt = prompt[-1500:]
    logger.debug(f"analyze_media: отправляем prompt к AI: {prompt!r}")
    result = await call_ai(prompt, provider=provider)
    logger.debug(f"analyze_media: AI вернул {result!r}")

    # если модель отдала новый текст, обновляем глобальный текущий контекст
    if result:
        current_context = result
    return result


async def observer_loop(url: str, provider: str = "groq", enable_audio: bool = True, enable_video: bool = True):
    global current_context
    logger.info(f"Начинаю наблюдение за стримом: {url} (provider={provider})")
    try:
        while True:
            try:
                img = await capture_frame(url) if enable_video else None
                audio = await capture_audio(url) if enable_audio else None
                if img or audio:
                    new_context = await analyze_media(img, audio, provider)
                    if new_context:
                        current_context = new_context
                        logger.info(f"Контекст обновлен: {current_context[:50]}...")
                else:
                    logger.warning("Не удалось захватить ни кадр, ни аудио (возможно они отключены)")
            except Exception as e:
                current_context = f"Ошибка наблюдателя: {e}"
                logger.error(f"Ошибка в observer_loop: {e}", exc_info=True)

            await asyncio.sleep(15)
    except asyncio.CancelledError:
        logger.info("Observer loop отменен")
        current_context = "Стрим не запущен."
