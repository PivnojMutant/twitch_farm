from streamlink import Streamlink
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
# флаг, выставляется когда в потоке обнаружена разрывность (streamlink warning)
# пока он True, мы не отправляем запросы в ИИ, даже если доступно аудио.
stream_broken = False

async def capture_frame(url: str, retries: int = 3):
    """Захватывает один кадр из потока Twitch.
    Сначала пытаемся напрямую через ffmpeg, чтобы избежать предупреждений
    о разрывности. Если это не сработает, используем Streamlink с
    повторными попытками и опциями hls-live-restart.
    """
    logger.debug(f"capture_frame: пытаемся получить кадр из {url}")

    # попробуем сначала ffmpeg (передаём URL как есть)
    img_fd, img_path = tempfile.mkstemp(suffix=".jpg")
    os.close(img_fd)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            # используем короткую команду для одного кадра, игнорируем ошибки
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
                return img_path
            else:
                logger.debug("capture_frame: ffmpeg не выдал кадр, пробуем Streamlink")
        except Exception as e:
            logger.debug(f"capture_frame: ffmpeg выдал исключение {e}, пробуем Streamlink")
    else:
        logger.debug("capture_frame: ffmpeg не найден, пропускаем этот шаг")

    # если ffmpeg не помог, используем Streamlink
    logger.debug("capture_frame: обращаемся к Streamlink")
    try:
        session = Streamlink()
        session.set_option("twitch-disable-ads", True)
        session.set_option("stream-timeout", 30)  # timeout 30 сек
        session.set_option("hls-live-restart", True)  # перезапуск на символическом сегменте
        streams = session.streams(url)
    except Exception as e:
        logger.error(f"capture_frame: не удалось получить потоки: {e}")
        return None
    if not streams:
        logger.warning(f"capture_frame: потоков не найдено для {url}")
        return None
    
    # пробуем разные качества потока (best, 720p60, 720p, 480p и т.д.)
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

    # сначала попробуем сохранить кусок файла с повторами
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    for attempt in range(retries):
        try:
            # всегда переоткрываем поток перед каждой попыткой
            with stream.open() as s, open(path, "wb") as f:
                # читаем больше данных для стабильности (5MB вместо 1MB)
                chunk = s.read(5 * 1024 * 1024)
                if not chunk or len(chunk) == 0:
                    logger.warning(f"capture_frame: попытка {attempt + 1}: получен пустой chunk")
                    continue
                f.write(chunk)
                logger.debug(f"capture_frame: прочитано {len(chunk)} байт")
                break
        except Exception as e:
            # если Streamlink сообщил о разрывности HLS-потока, то
            # пометим глобальный флаг, чтобы остановить отправку запросов в ИИ
            msg = str(e).lower()
            if "discontinuity" in msg:
                global stream_broken
                stream_broken = True
                logger.warning("capture_frame: обнаружена разрывность потока (stream discontinuity), приостановка ИИ до нового кадра")
                # не делаем больше попыток, вернём None
                return None
            logger.warning(f"capture_frame: попытка {attempt + 1}/{retries} - ошибка при чтении потока: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)  # ждем перед повторной попыткой
            else:
                logger.error(f"capture_frame: все {retries} попыток исчерпаны")
                return None

    img_path = path + ".jpg"

    # проверяем что файл не пустой перед обработкой
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        logger.error("capture_frame: файл потока пуст или не создан")
        return None

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

                # если недавно была разрывность потока и мы всё ещё не получили
                # новый кадр, не шлём запросы в ИИ вообще (чтобы не расходовать лимит)
                global stream_broken
                if stream_broken and enable_video:
                    if img:
                        # наконец получен новый кадр, снимаем блокировку
                        stream_broken = False
                        logger.info("capture_frame: кадр восстановлен, продолжаем отправку в ИИ")
                    else:
                        logger.warning("Пропускаем вызов ИИ из-за предыдущей разрывности потока")
                        # пропускаем остальные шаги и ждём следующего цикла
                        await asyncio.sleep(15)
                        continue

                if img or audio:
                    new_context = await analyze_media(img, audio, provider)
                    if new_context:
                        current_context = new_context
                        logger.info(f"Контекст обновлен: {current_context[:50]}...")
                else:
                    # определяем, почему не получилось захватить данные
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
