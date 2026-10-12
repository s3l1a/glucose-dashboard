# Glucose Dashboard

A static dashboard with a Python Dexcom updater and an optional Telegram bot. The dashboard displays recent readings, summary statistics, a simple linear forecast, and local system telemetry.

This project is not medical advice and is not suitable for treatment decisions. Forecasts, estimates, and browser alarms are not validated clinical tools.

## Local Setup

Use Python 3.10 or newer and Node.js 18 or newer for frontend checks.

```sh
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -r requirements.txt
```

## Synthetic Demonstration

```sh
python update_glucose.py --demo
python -m http.server 8000 --bind localhost --directory web
```

Open `http://localhost:8000`. All demonstration readings are generated mathematically with fixed fictional timestamps and labeled **SYNTHETIC DEMONSTRATION - NOT MEDICAL DATA** in the output and dashboard. They are not derived from anyone's medical data. Demo mode does not contact Dexcom and does not sound alarms. Generated JSON is ignored by Git; no medical records or original runtime data are included in this repository.

## Configuration

For private live use, create a local `.env` using `.env.example` as the template, or set environment variables directly. Never commit real values.

| Variable | Purpose |
| --- | --- |
| `DEXCOM_USERNAME` | Required for a live Dexcom update. |
| `DEXCOM_PASSWORD` | Required for a live Dexcom update. |
| `DEXCOM_REGION` | Dexcom region: `us`, `ous`, or `jp`; default `us`. |
| `WEB_DIR` | Dashboard output directory; default `web`. |
| `GLUCOSE_DATA_FILE` | Local reading file shared with the bot; default `data/glucose.json`. |
| `TELEGRAM_BOT_TOKEN` | Required only for the optional bot. |
| `TELEGRAM_ALLOWED_USER_ID` | Required numeric Telegram user ID; other users and group chats are ignored. |

Relative file paths are resolved from this project directory. Demo mode needs no credentials. Run `python update_glucose.py` for one live update, or `python glucose_bot.py` for the optional bot. The updater does not schedule itself; the dashboard refreshes every 30 seconds. Browser audio requires the enable-sound button and may be blocked by browser policy.

Do not expose a live dashboard or its JSON on a public web server. The local demo server provides no authentication. Credentials, runtime readings, logs, keys, backups, caches, and local configuration must remain private. The service worker caches only application assets, not reading data. Chart libraries are loaded from the public jsDelivr CDN, so charts require connectivity on the first load.

## Checks

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
python -m compileall -q update_glucose.py glucose_bot.py tests
node checks.mjs
```

The frontend is static and has no bundling step. `checks.mjs` validates its JavaScript syntax, manifest, and local asset references. Tests use only synthetic inputs and do not contact Dexcom or Telegram.

## License

MIT License. See [LICENSE](LICENSE).
