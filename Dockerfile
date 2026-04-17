FROM python:3.13-slim

WORKDIR /app

# System deps: ca-certs for HTTPS, tzdata so zoneinfo("America/New_York") works.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better build cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./

# Mount points for persistent volume (overlaid by fly.toml at runtime)
RUN mkdir -p /data/state /data/output

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data/state \
    OUTPUT_DIR=/data/output

# Default: run the Telegram bot in long-polling mode. The bot itself invokes
# odds_compare.run_headless_fetch() when users send /run.
CMD ["python", "-u", "telegram_bot.py"]
