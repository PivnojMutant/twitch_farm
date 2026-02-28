FROM python:3.11-slim
WORKDIR /app

# Объединяем установку зависимостей в один слой
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libgl1 \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install twitchio==2.9.1

# Копируем СОДЕРЖИМОЕ твоей папки app в корень /app контейнера
COPY app/ .

# Запускаем напрямую main:app (так как мы уже внутри папки с файлом)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]