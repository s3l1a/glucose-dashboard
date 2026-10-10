import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WEB_DIR = BASE_DIR / os.getenv("WEB_DIR", "web")
DATA_FILE = BASE_DIR / os.getenv("GLUCOSE_DATA_FILE", "data/glucose.json")


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
        temperature = Path("/sys/class/thermal/thermal_zone0/temp").read_text()
        telemetry["cpu_temp"] = round(int(temperature.strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        total = int(next(line for line in lines if line.startswith("MemTotal:")).split()[1])
        available = int(next(line for line in lines if line.startswith("MemAvailable:")).split()[1])
        telemetry["ram_percent"] = round((total - available) / total * 100, 1)
    except (OSError, ValueError, StopIteration, ZeroDivisionError):
        pass
    return telemetry


def calculate_clinical_metrics(values):
    if not values:
        return {}
    n = len(values)
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / n
    std_dev = math.sqrt(variance)
    return {
        "mean": round(mean, 1),
        "std_dev": round(std_dev, 1),
        "cv": round(std_dev / mean * 100 if mean > 0 else 0, 1),
        "gmi": round(3.31 + 0.02392 * mean, 2),
        "tir_in_range": round(sum(90 <= value <= 180 for value in values) / n * 100, 1),
        "tir_low": round(sum(value < 90 for value in values) / n * 100, 1),
        "tir_high": round(sum(value > 180 for value in values) / n * 100, 1),
    }


def calculate_forecast(history_readings):
    if len(history_readings) < 3:
        return []
    recent = list(reversed(history_readings[:4]))
    xs = [reading.datetime.timestamp() / 60 for reading in recent]
    ys = [reading.value for reading in recent]
    x_bar, y_bar = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - x_bar) ** 2 for x in xs)
    slope = (
        sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys)) / denominator if denominator else 0
    )
    points = [{"time": recent[-1].datetime.strftime("%H:%M"), "value": ys[-1]}]
    for minutes in (10, 20, 30):
        points.append(
            {
                "time": (recent[-1].datetime + timedelta(minutes=minutes)).strftime("%H:%M"),
                "value": max(40, min(400, round(ys[-1] + slope * minutes))),
            }
        )
    return points


def build_payload(bg, history, telemetry, synthetic=False):
    delta = bg.value - history[1].value if len(history) >= 2 else 0
    color = "#ef4444" if bg.value < 90 else "#eab308" if bg.value > 180 else "#22c55e"
    return {
        "synthetic": synthetic,
        "data_source": "SYNTHETIC DEMONSTRATION - NOT MEDICAL DATA"
        if synthetic
        else "Private live data",
        "current": {
            "synthetic": synthetic,
            "value": bg.value,
            "delta": f"+{delta}" if delta > 0 else str(delta),
            "trend": str(bg.trend_arrow),
            "trend_symbol": get_trend_symbol(bg.trend_arrow),
            "time": bg.datetime.strftime("%H:%M"),
            "timestamp": int(bg.datetime.timestamp()),
            "is_low": bg.value < 90,
            "is_high": bg.value > 180,
            "is_crashing": str(bg.trend_arrow)
            in ["DoubleDown", "SingleDown", "\u2193\u2193", "\u2193"],
            "color": color,
        },
        "stats": calculate_clinical_metrics([reading.value for reading in history]),
        "telemetry": telemetry,
        "history": [
            {"time": reading.datetime.strftime("%H:%M"), "value": reading.value}
            for reading in reversed(history)
        ],
        "forecast": calculate_forecast(history),
    }


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
    try:
        if args.demo:
            start = datetime(2000, 1, 1, tzinfo=timezone.utc)
            history = [
                SimpleNamespace(
                    value=round(135 + 32 * math.sin(index / 13)),
                    datetime=start + timedelta(minutes=index * 5),
                    trend_arrow="Flat",
                )
                for index in range(144)
            ]
            history.reverse()
            bg = history[0]
            telemetry = {"cpu_temp": None, "ram_percent": None, "api_latency_ms": None}
        else:
            from pydexcom import Dexcom

            username = os.getenv("DEXCOM_USERNAME", "")
            password = os.getenv("DEXCOM_PASSWORD", "")
            if (
                not username
                or not password
                or any(value.startswith("replace-with-") for value in (username, password))
            ):
                logging.error("Set DEXCOM_USERNAME and DEXCOM_PASSWORD locally, or use --demo.")
                return 1
            started = time.monotonic()
            dexcom = Dexcom(
                username=username,
                password=password,
                region=os.getenv("DEXCOM_REGION", "us"),
            )
            bg = dexcom.get_current_glucose_reading()
            history = dexcom.get_glucose_readings(minutes=720)
            if not bg or not history:
                logging.warning("No readings returned.")
                return 1
            telemetry = get_system_telemetry()
            telemetry["api_latency_ms"] = round((time.monotonic() - started) * 1000)
        payload = build_payload(bg, history, telemetry, synthetic=args.demo)
        WEB_DIR.mkdir(parents=True, exist_ok=True)
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        (WEB_DIR / "glucose_data.json").write_text(json.dumps(payload), encoding="utf-8")
        DATA_FILE.write_text(json.dumps(payload["current"]), encoding="utf-8")
        logging.info("Synthetic demonstration generated." if args.demo else "Local data updated.")
        return 0
    except Exception:
        logging.error("Update failed. Check local configuration and connectivity.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
