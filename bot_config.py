"""
Configuration for the Telegram bot.

Set these via environment variables — never commit secrets to source.
A `.env` file alongside this script (gitignored) is the easiest way; see
.env.example for the full list.

To get your bot token:  message @BotFather on Telegram  ->  /newbot
To get your chat ID:    message @userinfobot on Telegram ->  /start
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; env vars may be set by the OS

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID: int = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "")

# State files (users.json) live in DATA_DIR on cloud (persistent volume).
# Locally, defaults to the script's directory for backward compatibility.
_DATA_DIR = os.environ.get(
    "DATA_DIR", os.path.dirname(os.path.abspath(__file__))
)
# Volume mounts overlay the directories created by the Dockerfile, so we must
# re-create our state subdirs here at runtime (not just during image build).
os.makedirs(_DATA_DIR, exist_ok=True)
USERS_FILE: str = os.path.join(_DATA_DIR, "users.json")


def validate_required_secrets() -> None:
    """Exit with a helpful message if required secret env vars are missing.

    Called from main() so a misconfigured environment fails fast rather
    than 401-ing on the first upstream API call.
    """
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not ODDS_API_KEY:
        missing.append("ODDS_API_KEY")
    if not os.environ.get("PINNACLE_GUEST_KEY"):
        missing.append("PINNACLE_GUEST_KEY")
    if not missing:
        return

    print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
    print()
    print("Set them via one of:")
    print("  - .env file alongside the source (gitignored; see .env.example)")
    print("  - shell exports: `export ODDS_API_KEY=...`")
    print("  - fly.io: `fly secrets set ODDS_API_KEY=...`")
    print()
    print("Where to get them:")
    print("  - TELEGRAM_BOT_TOKEN  -> message @BotFather on Telegram")
    print("  - ODDS_API_KEY        -> https://the-odds-api.com/")
    print("  - PINNACLE_GUEST_KEY  -> pinnacle.com -> DevTools -> Network -> "
          "X-Api-Key header (rotates periodically)")
    sys.exit(1)
