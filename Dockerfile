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
COPY static/ ./static/

# Mount points for persistent volume (overlaid by fly.toml at runtime)
RUN mkdir -p /data/state /data/output

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data/state \
    OUTPUT_DIR=/data/output

# The Telegram bot AND the FastAPI webapp run in the same process,
# sharing one asyncio loop. Long-polling stays primary; the HTTP server
# listens on :8080 (which fly.toml forwards 80/443 to).
EXPOSE 8080
CMD ["python", "-u", "telegram_bot.py"]
