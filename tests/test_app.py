"""All readings, timestamps, and user IDs in these tests are synthetic fixtures."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import glucose_bot
import update_glucose


def synthetic_readings(values, step_minutes=5):
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            value=value,
            datetime=start - timedelta(minutes=index * step_minutes),
            trend_arrow="Flat",
        )
        for index, value in enumerate(values)
    ]


class CalculationTests(unittest.TestCase):
    def test_empty_metrics(self):
        self.assertEqual(update_glucose.calculate_clinical_metrics([]), {})

    def test_thresholds_and_metrics(self):
        result = update_glucose.calculate_clinical_metrics([80, 90, 180, 200])
        self.assertEqual(result["mean"], 137.5)
        self.assertEqual(result["tir_low"], 25)
        self.assertEqual(result["tir_high"], 25)
        self.assertEqual(result["tir_in_range"], 50)

    def test_custom_thresholds_and_mmol_conversion(self):
        result = update_glucose.calculate_clinical_metrics(
            [72, 108, 180], low=70, high=180, unit_key="mmol"
        )
        self.assertEqual(result["tir_in_range"], 100.0)
        self.assertAlmostEqual(result["mean"], 6.7, places=1)

    def test_zero_mean(self):
        self.assertEqual(update_glucose.calculate_clinical_metrics([0, 0])["cv"], 0)

    def test_forecast_requires_three_recent_readings(self):
        self.assertEqual(update_glucose.calculate_forecast(synthetic_readings([100, 100])), [])
        sparse = synthetic_readings([130, 125, 120], step_minutes=20)
        self.assertEqual(update_glucose.calculate_forecast(sparse), [])

    def test_linear_forecast(self):
        forecast = update_glucose.calculate_forecast(synthetic_readings([130, 125, 120, 115]))
        self.assertEqual([point["value"] for point in forecast], [130, 140, 150, 160])

    def test_forecast_limits(self):
        high = update_glucose.calculate_forecast(synthetic_readings([390, 300, 210, 120]))
        low = update_glucose.calculate_forecast(synthetic_readings([50, 140, 230, 320]))
        self.assertEqual(high[-1]["value"], 400)
        self.assertEqual(low[-1]["value"], 40)

    def test_duplicate_timestamps(self):
        readings = synthetic_readings([120, 110, 100])
        for reading in readings:
            reading.datetime = readings[0].datetime
        self.assertEqual(update_glucose.calculate_forecast(readings)[-1]["value"], 120)

    def test_delta_suppressed_across_large_gap(self):
        readings = synthetic_readings([150, 120], step_minutes=20)
        payload = update_glucose.build_payload(readings[0], readings, {}, synthetic=True)
        self.assertEqual(payload["current"]["delta"], "--")
        self.assertFalse(payload["current"]["delta_available"])

    def test_stale_detection_for_live_readings(self):
        readings = synthetic_readings([115, 112, 110])
        stale_now = int(readings[0].datetime.timestamp()) + 25 * 60
        payload = update_glucose.build_payload(
            readings[0], readings, {}, synthetic=False, now_ts=stale_now
        )
        self.assertTrue(payload["current"]["is_stale"])
        self.assertEqual(payload["current"]["age_minutes"], 25)

    def test_sqlite_history_store_retains_and_prunes(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "history.sqlite3"
            now = datetime(2000, 1, 20, tzinfo=timezone.utc)
            fresh = SimpleNamespace(value=120, datetime=now, trend_arrow="Flat")
            expired = SimpleNamespace(
                value=100, datetime=now - timedelta(days=16), trend_arrow="Flat"
            )
            stored = update_glucose.sync_history_store(db_path, [fresh, expired], retention_days=14)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].value, 120)

    def test_unknown_trend_is_not_reflected(self):
        self.assertEqual(update_glucose.get_trend_symbol("synthetic-unrecognized-trend"), "?")

    def test_pydexcom_unicode_trends_are_preserved(self):
        for arrow in ("\u2192", "\u2191\u2191", "\u2193\u2193", "-"):
            self.assertEqual(update_glucose.get_trend_symbol(arrow), arrow)

    def test_synthetic_payload_is_labeled_and_multi_range(self):
        readings = synthetic_readings([130, 125, 120])
        payload = update_glucose.build_payload(readings[0], readings, {}, synthetic=True)
        self.assertTrue(payload["synthetic"])
        self.assertTrue(payload["current"]["synthetic"])
        self.assertIn("SYNTHETIC", payload["data_source"])
        self.assertIn("14d", payload["stats_by_range"])
        self.assertEqual([point["value"] for point in payload["history"]], [120, 125, 130])

    def test_demo_is_offline_and_writes_only_synthetic_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(update_glucose, "WEB_DIR", root / "web"),
                patch.object(update_glucose, "DATA_FILE", root / "data" / "glucose.json"),
                patch("sys.argv", ["update_glucose.py", "--demo"]),
                patch("pydexcom.Dexcom") as dexcom,
            ):
                self.assertEqual(update_glucose.main(), 0)
            dexcom.assert_not_called()
            payload = json.loads((root / "web" / "glucose_data.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["synthetic"])
            self.assertEqual(len(payload["history"]), 289)
            self.assertEqual(payload["stats_by_range"]["14d"]["count"], 4032)
            self.assertTrue(
                json.loads((root / "data" / "glucose.json").read_text(encoding="utf-8"))[
                    "synthetic"
                ]
            )

    def test_missing_credentials_prevents_network_call(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sys.argv", ["update_glucose.py"]),
            patch("pydexcom.Dexcom") as dexcom,
            self.assertLogs(level="ERROR"),
        ):
            self.assertEqual(update_glucose.main(), 1)
        dexcom.assert_not_called()

    def test_push_alert_cooldown_and_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "alert_state.json"
            cfg = update_glucose.load_config()
            low_reading = {
                "synthetic": False,
                "is_stale": False,
                "is_urgent_low": False,
                "is_low": True,
                "is_crashing": False,
                "value": 82,
                "unit": "mg/dL",
                "delta": "-5",
                "trend_symbol": "\u2193",
                "time": "12:00",
            }
            urgent_reading = dict(low_reading, is_urgent_low=True, value=64)
            fake_resp = MagicMock()
            fake_resp.status = 200
            fake_resp.__enter__.return_value = fake_resp
            with (
                patch.dict(
                    os.environ,
                    {"TELEGRAM_BOT_TOKEN": "123:test", "TELEGRAM_ALLOWED_USER_ID": "42"},
                    clear=True,
                ),
                patch("urllib.request.urlopen", return_value=fake_resp) as mock_open,
            ):
                self.assertTrue(
                    update_glucose.maybe_send_telegram_alert(
                        low_reading, cfg, state_file=state_file, now_ts=1000
                    )
                )
                self.assertFalse(
                    update_glucose.maybe_send_telegram_alert(
                        low_reading, cfg, state_file=state_file, now_ts=1200
                    )
                )
                self.assertTrue(
                    update_glucose.maybe_send_telegram_alert(
                        urgent_reading, cfg, state_file=state_file, now_ts=1300
                    )
                )
                self.assertEqual(mock_open.call_count, 2)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_user_in_group_cannot_read_file(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=0), effective_chat=SimpleNamespace(id=0, type="group")
        )
        with (
            patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "0"}, clear=True),
            patch.object(Path, "read_text") as read,
        ):
            await glucose_bot.start(update, context)
            await glucose_bot.check_bg(update, context)
        read.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_unconfigured_bot_does_not_respond(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=0),
            effective_chat=SimpleNamespace(id=0, type="private"),
        )
        with patch.dict(os.environ, {}, clear=True):
            await glucose_bot.start(update, context)
            await glucose_bot.check_bg(update, context)
        context.bot.send_message.assert_not_awaited()

    async def test_unauthorized_user_cannot_read_file(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=1, type="private"),
        )
        with (
            patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "0"}, clear=True),
            patch.object(Path, "read_text") as read,
        ):
            await glucose_bot.start(update, context)
            await glucose_bot.check_bg(update, context)
        read.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_authorized_synthetic_message_is_labeled(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=0),
            effective_chat=SimpleNamespace(id=0, type="private"),
        )
        with (
            patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "0"}, clear=True),
            patch.object(
                Path,
                "read_text",
                return_value=json.dumps({"synthetic": True, "value": 123, "time": "00:00"}),
            ),
        ):
            await glucose_bot.check_bg(update, context)
        message = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("SYNTHETIC DEMONSTRATION - NOT MEDICAL DATA", message)

    async def test_stale_live_reading_warns_user(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=0),
            effective_chat=SimpleNamespace(id=0, type="private"),
        )
        with (
            patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID": "0"}, clear=True),
            patch.object(
                Path,
                "read_text",
                return_value=json.dumps(
                    {"synthetic": False, "value": 140, "time": "10:00", "timestamp": 1000}
                ),
            ),
        ):
            await glucose_bot.check_bg(update, context)
        message = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("STALE DATA", message)


if __name__ == "__main__":
    unittest.main()
