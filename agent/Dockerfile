FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Предзагружаем модели VAD и определения конца реплики,
# иначе первый звонок будет ждать скачивания.
RUN python agent.py download-files || true
CMD ["python", "agent.py", "start"]
