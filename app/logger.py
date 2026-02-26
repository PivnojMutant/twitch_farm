import logging
import os
from collections import deque
from typing import List

class LogBuffer(logging.Handler):
    """Хранит последние логи в памяти для отправки на веб-интерфейс"""
    
    def __init__(self, max_logs: int = 100):
        super().__init__()
        self.logs: deque = deque(maxlen=max_logs)
    
    def emit(self, record):
        try:
            log_entry = self.format(record)
            self.logs.append({
                "level": record.levelname,
                "message": log_entry,
                "time": record.created
            })
        except Exception:
            self.handleError(record)
    
    def get_logs(self) -> List[dict]:
        """Возвращает все логи"""
        return list(self.logs)
    
    def clear(self):
        """Очищает буфер"""
        self.logs.clear()

# Глобальный инстанс логов
log_buffer = LogBuffer()

def setup_logging():
    """Настраивает логирование с буфером"""
    logger = logging.getLogger()
    # allow debug via ENV variable DEBUG=1
    level = logging.DEBUG if os.getenv('DEBUG', '').lower() in ('1','true','yes') else logging.INFO
    logger.setLevel(level)
    
    # Удаляем старые хендлеры если есть
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Добавляем буферизованный хендлер
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    log_buffer.setFormatter(formatter)
    logger.addHandler(log_buffer)
    
    # Добавляем console хендлер
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
