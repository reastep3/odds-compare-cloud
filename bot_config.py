"""
Configuration for the Telegram bot.

Set these via environment variables, or edit the fallback values below.
  export TELEGRAM_BOT_TOKEN="7123456789:AAH..."
  export TELEGRAM_CHAT_ID="123456789"

To get your bot token:  message @BotFather on Telegram  ->  /newbot
To get your chat ID:    message @userinfobot on Telegram ->  /start
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; env vars may be set by the OS

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID: int = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "720d081206a747be72810d4e6905ccb6")

# State files (users.json) live in DATA_DIR on cloud (persistent volume).
# Locally, defaults to the script's directory for backward compatibility.
_DATA_DIR = os.environ.get(
    "DATA_DIR", os.path.dirname(os.path.abspath(__file__))
)
# Volume mounts overlay the directories created by the Dockerfile, so we must
# re-create our state subdirs here at runtime (not just during image build).
os.makedirs(_DATA_DIR, exist_ok=True)
USERS_FILE: str = os.path.join(_DATA_DIR, "users.json")
