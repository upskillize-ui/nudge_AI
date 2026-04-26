# PATCH: --workers 2 caused APScheduler jobs to fire in BOTH worker processes,
# resulting in duplicate nudges every cron tick. Run a single uvicorn worker.
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
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]