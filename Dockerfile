FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Taipei

# tzdata 是必要的：APScheduler 要用 Asia/Taipei 這個時區名
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY nanya_exit/ ./nanya_exit/
COPY data/ ./data/
COPY scheduler.py .

# 狀態預設寫到 volume 掛載點
ENV STATE_PATH=/data/state.json \
    CACHE_CSV=/data/price_cache.csv \
    SEED_CSV=data/seed_history.csv \
    NTFY_TOPIC=Exit2408 \
    TZ_NAME=Asia/Taipei \
    RUN_AT=20:00

# 沒掛 volume 時也不要直接爆掉
RUN mkdir -p /data

CMD ["python", "scheduler.py"]
