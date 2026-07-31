# Four workers so student requests are served concurrently. The old duplicate-
# nudge problem (cron firing in every worker) is solved by leader election in
# main.py — exactly one worker binds the lock port and runs the scheduler.
# If you need horizontal scaling, move scheduler to a separate sidecar process
# or use a distributed lock (e.g., redis-based apscheduler-redis).
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libmariadb-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p models
EXPOSE 7860
# PORT: Render injects it; HuggingFace expects 7860 — the fallback keeps one
# Dockerfile working on both platforms during and after the migration.
CMD ["sh", "-c", "uvicorn main:asgi --host 0.0.0.0 --port ${PORT:-7860} --workers 4"]