from streamlink import Streamlink
import asyncio
import tempfile
import logging
import shutil
import os
from ai_clients import call_ai, transcribe_audio_file, describe_image_file

class _DiscontinuityFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        if "discontinuity" in msg:
            global stream_broken
            stream_broken = True
        return True

_sl_logger = logging.getLogger("streamlink")
_sl_logger.addFilter(_DiscontinuityFilter())

try:
    import cv2
    _has_cv2 = True
except Exception:
    cv2 = None
    _has_cv2 = False
    logging.getLogger(__name__).warning("cv2 импортировать не удалось, кадры будут извлекаться через ffmpeg")

logger = logging.getLogger(__name__)
current_context = "Стрим не запущен."
stream_broken = False

async def capture_frame(url: str, retries: int = 3):
    logger.debug(f"capture_frame: пытаемся получить кадры из {url}")
    debug_dir = os.getenv("DEBUG_FRAMES_DIR")
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)

    img_fd, img_path = tempfile.mkstemp(suffix=".jpg")
    os.close(img_fd)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            cmd = [
                ffmpeg, "-y", "-nostdin", "-i", url,
                "-frames:v", "1", "-q:v", "2", img_path,
                "-loglevel", "error",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                logger.debug(f"capture_frame: кадр сохранён через ffmpeg в {img_path}")
                if debug_dir:
                    debug_path = os.path.join(debug_dir, os.path.basename(img_path))
                    shutil.copy(img_path, debug_path)
                    logger.debug(f"capture_frame: debug-копия кадра {debug_path}")
                return img_path
            else:
                logger.debug("capture_frame: ffmpeg не выдал кадр, пробуем Streamlink")
        except Exception as e:
            logger.debug(f"capture_frame: ffmpeg выдал исключение {e}, пробуем Streamlink")
    else:
        logger.debug("capture_frame: ffmpeg не найден, пропускаем этот шаг")

    logger.debug("capture_frame: обращаемся к Streamlink")
    try:
        session = Streamlink()
        session.set_option("twitch-disable-ads", True)
        session.set_option("stream-timeout", 30)
        session.set_option("hls-live-restart", True)
        streams = session.streams(url)
    except Exception as e:
        logger.error(f"capture_frame: не удалось получить потоки: {e}")
        return None
        
    if not streams:
        logger.warning(f"capture_frame: потоков не найдено для {url}")
        return None
    
    quality_preferences = ["best", "720p60", "720p", "480p60", "480p", "360p", "worst"]
    stream = None
    for quality in quality_preferences:
        if quality in streams:
            stream = streams[quality]
            logger.debug(f"capture_frame: выбран поток качества {quality}")
            break
            
    if not stream:
        stream = next(iter(streams.values()), None)
    if not stream:
        logger.warning(f"capture_frame: нет подходящего потока для {url}")
        return None

    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    for attempt in range(retries):
        try:
            global stream_broken
            stream_broken = False 
            
            with stream.open() as s, open(path, "wb") as f:
                downloaded = 0
                target_size = 1024 * 1024
                
                while downloaded < target_size:
                    chunk = s.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                
                logger.debug(f"capture_frame: прочитано {downloaded} байт")
                
                if downloaded < 100 * 1024:
                    logger.warning(f"capture_frame: попытка {attempt + 1}: слишком мало данных ({downloaded} байт)")
                    await asyncio.sleep(2)
                    continue
                
                break
                
        except Exception as e:
            logger.warning(f"capture_frame: попытка {attempt + 1}/{retries} - ошибка: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)
            else:
                return None

    if not os.path.exists(path) or os.path.getsize(path) < 100 * 1024:
        logger.error("capture_frame: файл потока пуст или поврежден")
        return None

    if _has_cv2:
        cap = cv2.VideoCapture(path)
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            cv2.imwrite(img_path, frame)
            logger.debug(f"capture_frame: кадр сохранён в {img_path} (cv2)")
            
            # Сохраняем копию кадра
            os.makedirs("data", exist_ok=True)
            shutil.copy(img_path, "data/debug_frame.jpg")
            
            return img_path
        logger.warning(f"capture_frame: cv2 не смог прочитать кадр из {path}")
    
    # fallback: используем ffmpeg для извлечения кадра
    if not ffmpeg:
        logger.error("capture_frame: ffmpeg не найден, невозможно извлечь кадр")
        return None
        
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y",
            "-i", path,
            "-fflags", "+igndts",
            "-copyts",
            "-frames:v", "1", "-q:v", "2", img_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        
        if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
            logger.debug(f"capture_frame: кадр сохранён в {img_path} (ffmpeg)")
            
            # Сохраняем копию кадра
            os.makedirs("data", exist_ok=True)
            shutil.copy(img_path, "data/debug_frame.jpg")
            
            return img_path
        else:
            logger.warning(f"capture_frame: ffmpeg не создал изображение")
            
    except Exception as e:
        logger.error(f"capture_frame: ffmpeg ошибка: {e}")
        
    return None

async def capture_audio(url: str, duration: int = 10):
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)

    cmd = [
        "ffmpeg", "-y", "-i", url,
        "-t", str(duration),
        "-q:a", "0", path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    if os.path.exists(path) and os.path.getsize(path) > 100:
        return path
    try:
        os.remove(path)
    except Exception:
        pass
    return None

async def analyze_media(img_path: str, audio_path: str, provider: str):
    global current_context
    desc = ""
    text = ""
    if img_path:
        logger.debug(f"analyze_media: описываем изображение {img_path}")
        desc = await describe_image_file(img_path, provider)
    if audio_path:
        logger.debug(f"analyze_media: транскрибируем аудио {audio_path}")
        text = await transcribe_audio_file(audio_path, provider)

    extra = ""
    if desc:
        extra += f"Описание кадра: {desc}\n"
    if text:
        extra += f"Транскрипт аудио: {text}\n"

    truncated_context = current_context
    if len(truncated_context) > 300:
        truncated_context = truncated_context[-300:]

    # ИСПРАВЛЕННЫЙ ПРОМПТ
    if extra:
        prompt = f"Контекст: {truncated_context}\n{extra}ОПИШИ в 1 предложении, что сейчас происходит на экране (без приветствий)."
    else:
        prompt = f"Контекст: {truncated_context}\nОПИШИ в 1 предложении, что сейчас происходит на экране."

    if len(prompt) > 1500:
        prompt = prompt[-1500:]
        
    result = await call_ai(prompt, provider=provider)
    await asyncio.sleep(30)

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

                global stream_broken
                if stream_broken and enable_video:
                    if img:
                        stream_broken = False
                        logger.info("capture_frame: кадр восстановлен, продолжаем отправку в ИИ")
                    else:
                        logger.warning("Пропускаем вызов ИИ из-за предыдущей разрывности потока")
                        await asyncio.sleep(15)
                        continue

                if img or audio:
                    new_context = await analyze_media(img, audio, provider)
                    if new_context:
                        current_context = new_context
                        logger.info(f"Контекст обновлен: {current_context:}...")
                else:
                    parts = []
                    if enable_video and not img:
                        parts.append("видео")
                    if enable_audio and not audio:
                        parts.append("аудио")
                    if not parts:
                        logger.warning("Оба захвата отключены, наблюдение простаивает")
                    else:
                        logger.warning("Не удалось захватить: %s" % ", ".join(parts))
            except Exception as e:
                current_context = f"Ошибка наблюдателя: {e}"
                logger.error(f"Ошибка в observer_loop: {e}", exc_info=True)

            await asyncio.sleep(15)
    except asyncio.CancelledError:
        logger.info("Observer loop отменен")
        current_context = "Стрим не запущен."
