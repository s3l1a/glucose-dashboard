import argparse
import json
import logging
import math
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WEB_DIR = BASE_DIR / os.getenv("WEB_DIR", "web")
DATA_FILE = BASE_DIR / os.getenv("GLUCOSE_DATA_FILE", "data/glucose.json")
DB_FILE = BASE_DIR / os.getenv("GLUCOSE_DB_FILE", "data/history.sqlite3")
SESSION_FILE = BASE_DIR / os.getenv("DEXCOM_SESSION_FILE", "data/dexcom_session.json")
ALERT_STATE_FILE = BASE_DIR / os.getenv("ALERT_STATE_FILE", "data/alert_state.json")

MMOL_FACTOR = 18.0182
RETENTION_DAYS = 14
DELTA_MAX_GAP_SECONDS = 450
WINDOWS = {
    "3h": 3 * 3600,
    "6h": 6 * 3600,
    "12h": 12 * 3600,
    "24h": 24 * 3600,
    "14d": 14 * 86400,
}


def env_int(name, default, minimum=1, maximum=1000):
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        return default


def load_config():
    unit_key = os.getenv("GLUCOSE_UNIT", "mgdl").strip().lower()
    if unit_key not in {"mgdl", "mmol"}:
        unit_key = "mgdl"
    low = env_int("LOW_THRESHOLD", 90, 40, 200)
    high = env_int("HIGH_THRESHOLD", 180, low + 10, 400)
    urgent_low = min(low, env_int("URGENT_LOW_THRESHOLD", 70, 40, low))
    return {
        "unit": "mmol/L" if unit_key == "mmol" else "mg/dL",
        "unit_key": unit_key,
        "low": low,
        "high": high,
        "urgent_low": urgent_low,
        "stale_minutes": env_int("STALE_MINUTES", 15, 5, 180),
        "alert_cooldown_minutes": env_int("ALERT_COOLDOWN_MINUTES", 30, 5, 360),
    }


def convert_value(mgdl_value, unit_key):
    if mgdl_value is None:
        return None
    if unit_key == "mmol":
        return round(mgdl_value / MMOL_FACTOR, 1)
    return round(mgdl_value)


def format_delta(delta_mgdl, unit_key):
    if delta_mgdl is None:
        return "--"
    if unit_key == "mmol":
        scaled = round(delta_mgdl / MMOL_FACTOR, 1)
        if scaled == 0:
            return "0.0"
        return f"+{scaled:.1f}" if scaled > 0 else f"{scaled:.1f}"
    rounded = round(delta_mgdl)
    return f"+{rounded}" if rounded > 0 else str(rounded)


def atomic_write_json(path, data, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data), encoding="utf-8")
    if private and os.name != "nt":
        os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def get_trend_symbol(arrow_str):
    mapping = {
        "DoubleUp": "\u21c8",
        "SingleUp": "\u2191",
        "FortyFiveUp": "\u2197",
        "Flat": "\u2192",
        "FortyFiveDown": "\u2198",
        "SingleDown": "\u2193",
        "DoubleDown": "\u21ca",
        "None": "",
        "NotComputable": "?",
    }
    value = str(arrow_str)
    if value in {*mapping.values(), "\u2191\u2191", "\u2193\u2193", "-"}:
        return value
    return mapping.get(value, "?")


def get_system_telemetry():
    telemetry = {"cpu_temp": None, "ram_percent": None}
    try:
        temperature = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8")
        telemetry["cpu_temp"] = round(int(temperature.strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        total = int(next(line for line in lines if line.startswith("MemTotal:")).split()[1])
        available = int(next(line for line in lines if line.startswith("MemAvailable:")).split()[1])
        telemetry["ram_percent"] = round((total - available) / total * 100, 1)
    except (OSError, ValueError, StopIteration, ZeroDivisionError):
        pass
    return telemetry


def calculate_clinical_metrics(values, low=90, high=180, unit_key="mgdl"):
    if not values:
        return {}
    n = len(values)
    mean_mgdl = sum(values) / n
    variance = sum((value - mean_mgdl) ** 2 for value in values) / n
    std_mgdl = math.sqrt(variance)
    mean_out = round(mean_mgdl / MMOL_FACTOR, 1) if unit_key == "mmol" else round(mean_mgdl, 1)
    std_out = round(std_mgdl / MMOL_FACTOR, 2) if unit_key == "mmol" else round(std_mgdl, 1)
    return {
        "count": n,
        "mean": mean_out,
        "mean_mgdl": round(mean_mgdl, 1),
        "std_dev": std_out,
        "cv": round(std_mgdl / mean_mgdl * 100 if mean_mgdl > 0 else 0, 1),
        "gmi": round(3.31 + 0.02392 * mean_mgdl, 2),
        "tir_in_range": round(sum(low <= value <= high for value in values) / n * 100, 1),
        "tir_low": round(sum(value < low for value in values) / n * 100, 1),
        "tir_high": round(sum(value > high for value in values) / n * 100, 1),
    }


def calculate_forecast(history_readings, unit_key="mgdl"):
    if len(history_readings) < 3:
        return []
    ordered_desc = sorted(
        enumerate(history_readings),
        key=lambda pair: (pair[1].datetime, -pair[0]),
        reverse=True,
    )
    anchor = ordered_desc[0][1].datetime
    recent = [
        reading
        for _, reading in reversed(ordered_desc[:4])
        if (anchor - reading.datetime).total_seconds() <= 25 * 60
    ]
    if len(recent) < 3:
        return []
    xs = [reading.datetime.timestamp() / 60 for reading in recent]
    ys = [reading.value for reading in recent]
    x_bar, y_bar = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - x_bar) ** 2 for x in xs)
    slope = (
        sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denominator if denominator else 0
    )
    points = [
        {
            "timestamp": int(anchor.timestamp()),
            "time": anchor.strftime("%H:%M"),
            "value": convert_value(ys[-1], unit_key),
            "mgdl": ys[-1],
        }
    ]
    for minutes in (10, 20, 30):
        future_dt = anchor + timedelta(minutes=minutes)
        clamped = max(40, min(400, round(ys[-1] + slope * minutes)))
        points.append(
            {
                "timestamp": int(future_dt.timestamp()),
                "time": future_dt.strftime("%H:%M"),
                "value": convert_value(clamped, unit_key),
                "mgdl": clamped,
            }
        )
    return points


def sync_history_store(db_path, readings, retention_days=RETENTION_DAYS):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        (int(reading.datetime.timestamp()), int(reading.value), str(reading.trend_arrow))
        for reading in readings
    ]
    latest_ts = max((row[0] for row in rows), default=int(time.time()))
    cutoff_ts = latest_ts - retention_days * 86400
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS readings ("
                "ts INTEGER PRIMARY KEY, value INTEGER NOT NULL, trend TEXT NOT NULL)"
            )
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO readings (ts, value, trend) VALUES (?, ?, ?)",
                    rows,
                )
            conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff_ts,))
            stored = conn.execute(
                "SELECT ts, value, trend FROM readings ORDER BY ts DESC"
            ).fetchall()
    finally:
        conn.close()
    if os.name != "nt" and db_path.exists():
        os.chmod(db_path, 0o600)
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return [
        SimpleNamespace(
            value=value,
            datetime=datetime.fromtimestamp(ts, tz=local_tz),
            trend_arrow=trend,
        )
        for ts, value, trend in stored
    ]


def fetch_dexcom_readings(username, password, region, session_file):
    import requests
    from pydexcom import Dexcom
    from pydexcom.const import DEXCOM_APPLICATION_IDS, DEXCOM_BASE_URLS
    from pydexcom.errors import SessionError

    cached = {}
    try:
        cached = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}

    dexcom = None
    if (
        cached.get("region") == region
        and cached.get("username") == username
        and cached.get("account_id")
        and cached.get("session_id")
        and region in DEXCOM_BASE_URLS
    ):
        client = Dexcom.__new__(Dexcom)
        client._base_url = DEXCOM_BASE_URLS[region]
        client._application_id = DEXCOM_APPLICATION_IDS[region]
        client._password = password
        client._username = username
        client._account_id = cached["account_id"]
        client._session_id = cached["session_id"]
        client._session = requests.Session()
        try:
            readings = client.get_glucose_readings(minutes=720)
            dexcom = client
        except SessionError:
            dexcom = None

    if dexcom is None:
        dexcom = Dexcom(username=username, password=password, region=region)
        readings = dexcom.get_glucose_readings(minutes=720)

    if getattr(dexcom, "_account_id", None) and getattr(dexcom, "_session_id", None):
        atomic_write_json(
            session_file,
            {
                "region": region,
                "username": username,
                "account_id": dexcom._account_id,
                "session_id": dexcom._session_id,
            },
            private=True,
        )
    return readings


def compute_delta_mgdl(bg, ordered_desc):
    bg_ts = bg.datetime.timestamp()
    for candidate in ordered_desc:
        gap = bg_ts - candidate.datetime.timestamp()
        if 60 <= gap <= DELTA_MAX_GAP_SECONDS:
            return bg.value - candidate.value
    return None


def build_payload(bg, history, telemetry, synthetic=False, config=None, now_ts=None):
    cfg = config or load_config()
    unit_key = cfg["unit_key"]
    low, high, urgent_low = cfg["low"], cfg["high"], cfg["urgent_low"]
    ordered_desc = sorted(history, key=lambda reading: reading.datetime, reverse=True)
    latest_ts = int(bg.datetime.timestamp())
    ref_now = int(now_ts if now_ts is not None else (latest_ts if synthetic else time.time()))
    age_minutes = max(0, round((ref_now - latest_ts) / 60))
    is_stale = (not synthetic) and age_minutes > cfg["stale_minutes"]

    delta_mgdl = compute_delta_mgdl(bg, ordered_desc)
    delta_str = format_delta(delta_mgdl, unit_key)
    color = "#ef4444" if bg.value < low else "#eab308" if bg.value > high else "#22c55e"
    trend_str = str(bg.trend_arrow)
    is_crashing = trend_str in ["DoubleDown", "SingleDown", "\u21ca", "\u2193\u2193", "\u2193"]

    stats_by_range = {}
    for label, window_seconds in WINDOWS.items():
        cutoff = latest_ts - window_seconds
        window_vals = [
            reading.value for reading in ordered_desc if reading.datetime.timestamp() >= cutoff
        ]
        stats_by_range[label] = calculate_clinical_metrics(
            window_vals, low=low, high=high, unit_key=unit_key
        )

    history_24h_cutoff = latest_ts - WINDOWS["24h"]
    chart_readings = [
        reading
        for reading in reversed(ordered_desc)
        if reading.datetime.timestamp() >= history_24h_cutoff
    ]

    return {
        "synthetic": synthetic,
        "generated_at": ref_now,
        "config": {
            "unit": cfg["unit"],
            "unit_key": unit_key,
            "low": convert_value(low, unit_key),
            "high": convert_value(high, unit_key),
            "urgent_low": convert_value(urgent_low, unit_key),
            "low_mgdl": low,
            "high_mgdl": high,
            "urgent_low_mgdl": urgent_low,
            "stale_minutes": cfg["stale_minutes"],
        },
        "current": {
            "synthetic": synthetic,
            "unit": cfg["unit"],
            "value": convert_value(bg.value, unit_key),
            "mgdl": bg.value,
            "delta": delta_str,
            "delta_available": delta_mgdl is not None,
            "trend": trend_str,
            "trend_symbol": get_trend_symbol(bg.trend_arrow),
            "time": bg.datetime.strftime("%H:%M"),
            "timestamp": latest_ts,
            "age_minutes": age_minutes,
            "is_stale": is_stale,
            "is_urgent_low": bg.value < urgent_low,
            "is_low": bg.value < low,
            "is_high": bg.value > high,
            "is_crashing": is_crashing,
            "color": color,
        },
        "stats": stats_by_range["12h"],
        "stats_by_range": stats_by_range,
        "telemetry": telemetry,
        "history": [
            {
                "timestamp": int(reading.datetime.timestamp()),
                "time": reading.datetime.strftime("%H:%M"),
                "value": convert_value(reading.value, unit_key),
                "mgdl": reading.value,
            }
            for reading in chart_readings
        ],
        "forecast": calculate_forecast(ordered_desc, unit_key=unit_key),
    }


def maybe_send_telegram_alert(current, config, state_file=ALERT_STATE_FILE, now_ts=None):
    if current.get("synthetic") or current.get("is_stale"):
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_user = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    if not token or token.startswith("replace-with-") or not allowed_user.isdecimal():
        return False

    kind = None
    if current.get("is_urgent_low"):
        kind = "urgent_low"
        heading = "URGENT LOW"
    elif current.get("is_low"):
        kind = "low"
        heading = "LOW ALERT"
    elif current.get("is_crashing"):
        kind = "crashing"
        heading = "RAPID DROP"

    ref_now = int(now_ts if now_ts is not None else time.time())
    state = {}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    if kind is None:
        if state.get("active_kind"):
            atomic_write_json(state_file, {"active_kind": None, "sent_at": 0}, private=True)
        return False

    cooldown_sec = config["alert_cooldown_minutes"] * 60
    last_sent = int(state.get("sent_at") or 0)
    last_kind = state.get("active_kind")
    escalated = kind == "urgent_low" and last_kind != "urgent_low"
    if last_kind is not None and not escalated and ref_now - last_sent < cooldown_sec:
        return False

    text = (
        f"{heading}\n\n"
        f"Glucose: {current['value']} {current['unit']} {current.get('trend_symbol', '')}\n"
        f"Delta: {current['delta']} {current['unit']} ({current['time']})\n"
        "Not for treatment decisions."
    )
    body = json.dumps({"chat_id": int(allowed_user), "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if 200 <= response.status < 300:
                atomic_write_json(
                    state_file,
                    {"active_kind": kind, "sent_at": ref_now},
                    private=True,
                )
                return True
    except OSError:
        logging.warning("Telegram alert delivery failed.")
    return False


def mark_existing_stale(config):
    now_ts = int(time.time())
    web_path = WEB_DIR / "glucose_data.json"
    for target, nested in ((web_path, True), (DATA_FILE, False)):
        if not target.exists():
            continue
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
            cur = doc.get("current") if nested else doc
            if not isinstance(cur, dict) or cur.get("synthetic"):
                continue
            ts = int(cur.get("timestamp") or 0)
            if ts <= 0:
                continue
            cur["age_minutes"] = max(0, round((now_ts - ts) / 60))
            cur["is_stale"] = cur["age_minutes"] > config["stale_minutes"]
            if nested:
                doc["generated_at"] = now_ts
            atomic_write_json(target, doc, private=not nested)
        except (OSError, ValueError, TypeError):
            continue


def generate_synthetic_history():
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    total_points = (14 * 24 * 60) // 5
    readings = []
    for index in range(total_points):
        phase = index + 22
        wave = (
            132 + 30 * math.sin(phase / 14) + 14 * math.cos(phase / 37) + 8 * math.sin(phase / 5.5)
        )
        slope = (30 / 14) * math.cos(phase / 14) - (14 / 37) * math.sin(phase / 37)
        trend = "FortyFiveUp" if slope > 1.1 else "FortyFiveDown" if slope < -1.1 else "Flat"
        readings.append(
            SimpleNamespace(
                value=round(wave),
                datetime=start + timedelta(minutes=index * 5),
                trend_arrow=trend,
            )
        )
    readings.reverse()
    return readings


def main():
    parser = argparse.ArgumentParser(
        description="Update the local dashboard or generate synthetic demonstration data."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Generate synthetic data without contacting Dexcom.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = load_config()
    try:
        if args.demo:
            history = generate_synthetic_history()
            bg = history[0]
            telemetry = {"cpu_temp": None, "ram_percent": None, "api_latency_ms": None}
            payload = build_payload(bg, history, telemetry, synthetic=True, config=config)
        else:
            username = os.getenv("DEXCOM_USERNAME", "")
            password = os.getenv("DEXCOM_PASSWORD", "")
            region = os.getenv("DEXCOM_REGION", "us")
            if (
                not username
                or not password
                or any(value.startswith("replace-with-") for value in (username, password))
            ):
                logging.error("Set DEXCOM_USERNAME and DEXCOM_PASSWORD locally, or use --demo.")
                return 1
            started = time.monotonic()
            live_readings = fetch_dexcom_readings(username, password, region, SESSION_FILE)
            latency_ms = round((time.monotonic() - started) * 1000)
            if not live_readings:
                mark_existing_stale(config)
                logging.warning("No readings returned.")
                return 1
            history = sync_history_store(DB_FILE, live_readings)
            bg = history[0]
            telemetry = get_system_telemetry()
            telemetry["api_latency_ms"] = latency_ms
            payload = build_payload(bg, history, telemetry, synthetic=False, config=config)
            maybe_send_telegram_alert(payload["current"], config)

        atomic_write_json(WEB_DIR / "glucose_data.json", payload)
        atomic_write_json(DATA_FILE, payload["current"], private=True)
        logging.info("Synthetic demonstration generated." if args.demo else "Local data updated.")
        return 0
    except Exception:
        if not args.demo:
            mark_existing_stale(config)
        logging.error("Update failed. Check local configuration and connectivity.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
