import os

# BL-3: the suite never sends a real Telegram message, whatever the shell exports.
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ.pop("TELEGRAM_CHAT_ID", None)
