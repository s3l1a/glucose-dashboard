import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_FILE = BASE_DIR / os.getenv("GLUCOSE_DATA_FILE", "data/glucose.json")
ALERT_STATE_FILE = BASE_DIR / os.getenv("ALERT_STATE_FILE", "data/alert_state.json")


def is_authorized(update: Update) -> bool:
    allowed = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    return bool(
        allowed
        and update.effective_user
        and update.effective_chat
        and update.effective_chat.type == "private"
        and str(update.effective_user.id) == allowed
    )


def stale_threshold_minutes() -> int:
    raw = os.getenv("STALE_MINUTES", "15")
    return int(raw) if raw.isdecimal() else 15


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "*Glucose Monitor Bot Active*\n"
            "Send /bg to check the local reading or /snooze to silence push alerts.\n"
            "Not for treatment decisions."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def check_bg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        synthetic = bool(data.get("synthetic"))
        unit = data.get("unit", "mg/dL")
        ts = int(data.get("timestamp") or 0)
        age_min = (
            max(0, round((time.time() - ts) / 60))
            if (ts > 0 and not synthetic)
            else int(data.get("age_minutes") or 0)
        )
        stale = (not synthetic) and (
            bool(data.get("is_stale")) or (ts > 0 and age_min > stale_threshold_minutes())
        )
        if stale:
            status = f"STALE DATA ({age_min}m ago)"
        elif data.get("is_urgent_low"):
            status = "URGENT LOW"
        elif data.get("is_low"):
            status = "LOW ALERT"
        elif data.get("is_high"):
            status = "HIGH"
        else:
            status = "In range"

        label = "SYNTHETIC DEMONSTRATION - NOT MEDICAL DATA\n" if synthetic else ""
        delta = data.get("delta")
        delta_part = f" ({delta} {unit})" if delta and delta != "--" else ""
        age_part = f" ({age_min}m ago)" if (ts > 0 and not synthetic) else ""
        message = (
            f"{label}{status}\n\n"
            f"*Glucose:* `{data.get('value')}` {unit} {data.get('trend_symbol', '')}{delta_part}\n"
            f"*Time:* {data.get('time')}{age_part}\nNot for treatment decisions."
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


async def snooze_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    minutes = 30
    if context.args and context.args[0].isdecimal():
        minutes = max(5, min(240, int(context.args[0])))
    ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    until_ts = int(time.time()) + minutes * 60
    tmp = ALERT_STATE_FILE.with_name(f".{ALERT_STATE_FILE.name}.tmp")
    tmp.write_text(json.dumps({"active_kind": "snoozed", "sent_at": until_ts}), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, ALERT_STATE_FILE)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Push alerts snoozed for {minutes} minutes.",
    )


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
    app.add_handler(CommandHandler("snooze", snooze_alerts))
    app.run_polling()
