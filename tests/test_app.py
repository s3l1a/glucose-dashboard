"""All readings, timestamps, and user IDs in these tests are synthetic fixtures."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import glucose_bot
import update_glucose


def synthetic_readings(values):
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            value=value,
            datetime=start - timedelta(minutes=index * 5),
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

    def test_zero_mean(self):
        self.assertEqual(update_glucose.calculate_clinical_metrics([0, 0])["cv"], 0)

    def test_forecast_requires_three_readings(self):
        self.assertEqual(update_glucose.calculate_forecast(synthetic_readings([100, 100])), [])

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

    def test_unknown_trend_is_not_reflected(self):
        self.assertEqual(update_glucose.get_trend_symbol("synthetic-unrecognized-trend"), "?")

    def test_pydexcom_unicode_trends_are_preserved(self):
        for arrow in ("\u2192", "\u2191\u2191", "\u2193\u2193", "-"):
            self.assertEqual(update_glucose.get_trend_symbol(arrow), arrow)

    def test_synthetic_payload_is_labeled(self):
        readings = synthetic_readings([130, 125, 120])
        payload = update_glucose.build_payload(readings[0], readings, {}, synthetic=True)
        self.assertTrue(payload["synthetic"])
        self.assertTrue(payload["current"]["synthetic"])
        self.assertIn("SYNTHETIC", payload["data_source"])
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
            payload = json.loads((root / "web" / "glucose_data.json").read_text())
            self.assertTrue(payload["synthetic"])
            self.assertEqual(len(payload["history"]), 144)
            self.assertTrue(json.loads((root / "data" / "glucose.json").read_text())["synthetic"])

    def test_missing_credentials_prevents_network_call(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sys.argv", ["update_glucose.py"]),
            patch("pydexcom.Dexcom") as dexcom,
            self.assertLogs(level="ERROR"),
        ):
            self.assertEqual(update_glucose.main(), 1)
        dexcom.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
