import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_FILE = BASE_DIR / os.getenv("GLUCOSE_DATA_FILE", "data/glucose.json")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        not update.effective_user
        or not update.effective_chat
        or update.effective_chat.type != "private"
        or str(update.effective_user.id) != os.getenv("TELEGRAM_ALLOWED_USER_ID")
    ):
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="*Glucose Monitor Bot Active*\nSend /bg to check the local reading.\nNot for treatment decisions.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def check_bg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        not update.effective_user
        or not update.effective_chat
        or update.effective_chat.type != "private"
        or str(update.effective_user.id) != os.getenv("TELEGRAM_ALLOWED_USER_ID")
    ):
        return
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        status = (
            "LOW ALERT" if data.get("is_low") else "HIGH" if data.get("is_high") else "In range"
        )
        label = "SYNTHETIC DEMONSTRATION - NOT MEDICAL DATA\n" if data.get("synthetic") else ""
        message = (
            f"{label}{status}\n\n"
            f"*Glucose:* `{data.get('value')}` mg/dL {data.get('trend_symbol', '')}\n"
            f"*Time:* {data.get('time')}\nNot for treatment decisions."
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
        )
    except FileNotFoundError:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="No local data available yet."
        )
    except Exception:
        logging.error("Bot request failed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_user = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    if not token or token.startswith("replace-with-") or not allowed_user.isdecimal():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID locally.")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bg", check_bg))
    app.run_polling()
