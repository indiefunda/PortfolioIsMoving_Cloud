#!/usr/bin/env python3
"""
PortfolioIsMoving (Cloud) - free stock movement monitor.

Runs on a Google Cloud "Always Free" e2-micro VM. Checks configured stocks
against their previous trading day's close and sends a Telegram alert when a
stock moves more than the configured threshold.

Runs every 10 minutes via cron (real cron on your own VM - reliable, unlike
GitHub Actions scheduled runs). Reads config_local.json and secrets_local.json
from the same folder.

Price sources (set in config_local.json -> provider):
  - finnhub (default):  real-time US stocks, 60 calls/min free, gets prev close
  - twelvedata:         real-time US stocks, 8 credits/min free
  - yahoo:              ~15 min delayed, unlimited, no key

Alerts: Telegram bot (free)
"""

import json
import os
import sys
from datetime import datetime, time as dtime

import pytz
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_local.json")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets_local.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
RUN_HISTORY_FILE = os.path.join(BASE_DIR, "run_history.json")
NETWORK_USAGE_FILE = os.path.join(BASE_DIR, "network_usage.json")

# Keep only the most recent runs in the history file (keeps it small on disk).
RUN_HISTORY_LIMIT = 500

# Google Cloud "Always Free" egress allowance from North America (bytes).
# The VM lives in us-central1 (N. America), so the limit is 1 GB/month.
# See https://cloud.google.com/free/docs/free-cloud-features
FREE_EGRESS_MONTHLY_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB

# US Eastern timezone (NY market)
EASTERN = pytz.timezone("US/Eastern")

# Telegram API
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# ---- Price provider endpoints ----
TWELVE_PRICE = "https://api.twelvedata.com/price?symbol={symbols}&apikey={key}"
TWELVE_USAGE = "https://api.twelvedata.com/api_usage?apikey={key}"
FINNHUB_QUOTE = "https://finnhub.io/api/v1/quote?symbol={symbol}&token={key}"
YAHOO_QUOTE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

DEFAULT_PROVIDER = "finnhub"
TWELVE_BATCH_SIZE = 8


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config():
    return _read_json(CONFIG_FILE, {})


def load_secrets():
    return _read_json(SECRETS_FILE, {})


def load_state():
    return _read_json(STATE_FILE, {"date": "", "alerted": [], "daily_usage": {}})


def save_state(state):
    _write_json(STATE_FILE, state)


# ---------------------------------------------------------------------------
# Run history (structured logging, readable by the control panel)
# ---------------------------------------------------------------------------
def load_run_history():
    """Return the list of past run records (newest first)."""
    records = _read_json(RUN_HISTORY_FILE, [])
    if not isinstance(records, list):
        return []
    return records


def append_run_record(record):
    """
    Append a single run record to the history file and trim it to the most
    recent RUN_HISTORY_LIMIT entries. Each record is a dict. This is what the
    local control panel reads to show you what the monitor saw.
    """
    try:
        records = load_run_history()
        records.insert(0, record)  # newest first
        records = records[:RUN_HISTORY_LIMIT]
        _write_json(RUN_HISTORY_FILE, records)
    except Exception as exc:
        print(f"  [error] could not write run history: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Network egress measurement
# ---------------------------------------------------------------------------
# Google's free-tier egress limit is per calendar month. We measure the VM's
# actual transmitted bytes from /proc/net/dev (all interfaces except loopback)
# and accumulate them into a small tally file. Since this VM only runs the
# monitor, that tally is effectively the app's egress.
def read_egress_bytes():
    """
    Return the total transmitted bytes across all non-loopback interfaces,
    read from /proc/net/dev. Returns None if the file isn't available (e.g.
    when running locally on Windows).
    """
    try:
        total = 0
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()
        # Skip the two header lines.
        for line in lines[2:]:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue
            fields = rest.split()
            # fields[0] = received bytes, fields[8] = transmitted bytes.
            if len(fields) > 8:
                total += int(fields[8])
        return total
    except Exception:
        return None


def load_network_usage():
    """
    Load the cumulative egress tally. Structure:
      {
        "month": "2026-08",
        "monthly_bytes": 123456,
        "days": {"2026-08-12": 123456}
      }
    """
    return _read_json(NETWORK_USAGE_FILE, {})


def save_network_usage(data):
    try:
        _write_json(NETWORK_USAGE_FILE, data)
    except Exception as exc:
        print(f"  [error] could not write network usage: {exc}", file=sys.stderr)


def record_network_usage(run_egress_bytes):
    """
    Add this run's egress bytes to the cumulative monthly + per-day tally.
    Returns the updated tally dict.
    """
    now_et = datetime.now(EASTERN)
    month = now_et.strftime("%Y-%m")
    day = now_et.strftime("%Y-%m-%d")
    data = load_network_usage()
    if data.get("month") != month:
        # New month - start a fresh tally for this month.
        data = {"month": month, "monthly_bytes": 0, "days": {}}
    data["monthly_bytes"] = data.get("monthly_bytes", 0) + run_egress_bytes
    days = data.setdefault("days", {})
    days[day] = int(days.get(day, 0)) + run_egress_bytes
    save_network_usage(data)
    return data


def format_bytes(n):
    """Human-friendly bytes -> KB / MB / GB."""
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------
last_usage = {}

# Tracks the response body size (bytes) of the last price call per symbol.
# Populated by the _fetch_* functions and read back in main() so each price
# record can show how many bytes that single API call cost.
last_call_bytes = {}


def get_last_usage():
    return last_usage


def get_provider_usage(provider, api_key):
    if provider == "twelvedata" and api_key:
        try:
            resp = requests.get(TWELVE_USAGE.format(key=api_key), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return {
                "used_min": data.get("current_usage"),
                "left_min": max(0, data.get("plan_limit", 8) - data.get("current_usage", 0)),
                "limit_min": data.get("plan_limit", 8),
                "daily_used": data.get("daily_usage", 0),
                "daily_limit": data.get("plan_daily_limit", 800),
                "delay": "real-time",
            }
        except Exception as exc:
            print(f"  [error] twelvedata usage: {exc}", file=sys.stderr)
            return None
    return None


def _fetch_twelvedata(symbols, api_key):
    global last_usage, last_call_bytes
    result = {}
    live = {}
    used_min = None
    left_min = None
    for i in range(0, len(symbols), TWELVE_BATCH_SIZE):
        chunk = symbols[i:i + TWELVE_BATCH_SIZE]
        try:
            resp = requests.get(
                TWELVE_PRICE.format(symbols=",".join(chunk), key=api_key),
                timeout=15,
            )
            resp.raise_for_status()
            body_bytes = len(resp.content)
            try:
                used_min = int(resp.headers.get("api-credits-used", used_min or 0))
                left_min = int(resp.headers.get("api-credits-left", left_min or 0))
            except (ValueError, TypeError):
                pass
            data = resp.json()
            if isinstance(data, dict) and "price" in data:
                live[chunk[0].upper()] = float(data["price"])
                last_call_bytes[chunk[0].upper()] = body_bytes
            else:
                # A batch of multiple symbols in one response body.
                for sym, q in data.items():
                    if isinstance(q, dict) and "price" in q:
                        live[sym.upper()] = float(q["price"])
                        last_call_bytes[sym.upper()] = body_bytes
        except Exception as exc:
            print(f"  [error] twelvedata price: {exc}", file=sys.stderr)

    prev = _fetch_yahoo(symbols)
    for sym in symbols:
        sym = sym.upper()
        if sym in live and sym in prev and prev[sym] is not None:
            result[sym] = (live[sym], prev[sym])
        elif sym in live:
            result[sym] = (live[sym], live[sym])

    if used_min is not None:
        last_usage = {"used_min": used_min, "left_min": left_min, "limit_min": 8}
    return result


def _fetch_finnhub(symbols, api_key):
    global last_usage, last_call_bytes
    result = {}
    used_min = 0
    for sym in symbols:
        sym = sym.upper()
        try:
            resp = requests.get(
                FINNHUB_QUOTE.format(symbol=sym, key=api_key),
                timeout=15,
            )
            resp.raise_for_status()
            used_min += 1
            data = resp.json()
            if "c" in data and data["c"]:
                current = float(data["c"])
                prev_close = float(data.get("pc") or current)
                result[sym] = (current, prev_close)
                last_call_bytes[sym] = len(resp.content)
        except Exception as exc:
            print(f"  [error] finnhub {sym}: {exc}", file=sys.stderr)
    if used_min:
        last_usage = {"used_min": used_min, "left_min": 60 - used_min, "limit_min": 60}
    return result


def _fetch_yahoo(symbols):
    global last_call_bytes
    result = {}
    for sym in symbols:
        sym = sym.upper()
        try:
            resp = requests.get(YAHOO_QUOTE.format(symbol=sym), headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            chart = data.get("chart", {}).get("result", [{}])[0]
            meta = chart.get("meta", {})
            current = meta.get("regularMarketPrice")
            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
            if current is not None:
                result[sym] = (float(current), float(prev_close) if prev_close else float(current))
                last_call_bytes[sym] = len(resp.content)
        except Exception as exc:
            print(f"  [error] yahoo {sym}: {exc}", file=sys.stderr)
    return result


def get_prices(symbols, provider=DEFAULT_PROVIDER, api_key=""):
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    last_call_bytes.clear()
    if not symbols:
        return {}, {}
    provider = (provider or DEFAULT_PROVIDER).lower()
    if provider == "twelvedata" and api_key:
        prices = _fetch_twelvedata(symbols, api_key)
    elif provider == "finnhub" and api_key:
        prices = _fetch_finnhub(symbols, api_key)
    else:
        prices = _fetch_yahoo(symbols)
    return prices, dict(last_call_bytes)


def send_telegram(token, chat_id, message):
    if not token or not chat_id:
        return False
    url = TELEGRAM_API.format(token=token)
    payload = {"chat_id": chat_id, "text": message}
    try:
        resp = requests.post(url, data=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"  [error] Telegram send failed: {exc}", file=sys.stderr)
        return False


def format_alert(symbol, current, prev_close, pct, threshold):
    direction = "🚨 UP" if pct > 0 else "📉 DOWN"
    arrow = "▲" if pct > 0 else "▼"
    return (
        f"{direction} {symbol}\n"
        f"{arrow} {abs(pct):.1f}%  (threshold {threshold}%)\n"
        f"Price: ${current:.2f}  |  Prev close: ${prev_close:.2f}"
    )


def is_market_hours(now_et):
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    open_t = dtime(9, 25)
    close_t = dtime(16, 5)
    return open_t <= now_et.time() <= close_t


def main():
    start_time = datetime.now(EASTERN)
    start_egress = read_egress_bytes()
    record = {
        "timestamp": start_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "status": "ran",
        "provider": "",
        "tickers_checked": 0,
        "prices": [],
        "alerts_sent": [],
        "alerts_failed": [],
        "duration_sec": None,
        "egress_bytes": None,
        "error": None,
    }

    config = load_config()
    if not config:
        print("No config_local.json found. Run setup_cloud.sh first.")
        record["status"] = "error"
        record["error"] = "No config_local.json found."
        append_run_record(record)
        return

    enabled = config.get("enabled", False)
    if not enabled:
        print("Monitoring is DISABLED (enabled=false). Set it to true in config_local.json.")
        record["status"] = "disabled"
        append_run_record(record)
        return

    tickers = config.get("tickers", [])
    threshold = float(config.get("threshold_pct", 5.0))
    provider = config.get("provider", DEFAULT_PROVIDER)
    record["provider"] = provider
    secrets = load_secrets()
    token = secrets.get("telegram_bot_token", "")
    chat_id = secrets.get("telegram_chat_id", "")
    api_key = secrets.get(f"{provider}_key", "") or secrets.get("price_api_key", "")

    if not tickers:
        print("No tickers configured. Add some to config_local.json.")
        record["status"] = "error"
        record["error"] = "No tickers configured."
        append_run_record(record)
        return
    if not token or not chat_id:
        print("Telegram credentials missing in secrets_local.json.")
        record["status"] = "error"
        record["error"] = "Telegram credentials missing in secrets_local.json."
        append_run_record(record)
        return

    now_et = datetime.now(EASTERN)
    if not is_market_hours(now_et):
        print(f"Skipping: outside market hours ({now_et.strftime('%a %H:%M %Z')}).")
        record["status"] = "outside_market_hours"
        append_run_record(record)
        return

    state = load_state()
    today = now_et.strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "alerted": [], "daily_usage": {}}

    print(f"[{now_et.strftime('%Y-%m-%d %H:%M %Z')}] Checking {len(tickers)} ticker(s) "
          f"via {provider}...")
    record["tickers_checked"] = len(tickers)

    prices, call_bytes = get_prices(tickers, provider=provider, api_key=api_key)

    if api_key and provider == "twelvedata":
        usage = state.setdefault("daily_usage", {})
        usage[provider] = int(usage.get(provider, 0)) + len(prices)

    for symbol in tickers:
        symbol = symbol.strip().upper()
        if not symbol:
            continue

        pair = prices.get(symbol)
        if pair is None:
            print(f"  - {symbol}: no data")
            record["prices"].append({
                "symbol": symbol, "current": None, "prev_close": None,
                "pct": None, "alert": False, "note": "no data",
                "egress_bytes": call_bytes.get(symbol),
            })
            continue
        current, prev_close = pair

        if current is None or prev_close is None or prev_close == 0:
            print(f"  - {symbol}: no valid price data")
            record["prices"].append({
                "symbol": symbol, "current": current, "prev_close": prev_close,
                "pct": None, "alert": False, "note": "no valid price data",
                "egress_bytes": call_bytes.get(symbol),
            })
            continue

        pct = (current - prev_close) / prev_close * 100
        print(f"  - {symbol}: ${current:.2f} (prev ${prev_close:.2f}) = {pct:+.2f}%")

        alerted = False
        if abs(pct) >= threshold and symbol not in state.get("alerted", []):
            msg = format_alert(symbol, current, prev_close, pct, threshold)
            if send_telegram(token, chat_id, msg):
                print(f"  -> Alert sent for {symbol}")
                state.setdefault("alerted", []).append(symbol)
                record["alerts_sent"].append(symbol)
                alerted = True
            else:
                print(f"  -> Alert FAILED for {symbol}")
                record["alerts_failed"].append(symbol)

        record["prices"].append({
            "symbol": symbol,
            "current": round(current, 4),
            "prev_close": round(prev_close, 4),
            "pct": round(pct, 2),
            "alert": alerted,
            "note": None,
            "egress_bytes": call_bytes.get(symbol),
        })

    save_state(state)

    record["duration_sec"] = round((datetime.now(EASTERN) - start_time).total_seconds(), 2)

    # Measure actual egress for this run and fold it into the monthly/day tally.
    end_egress = read_egress_bytes()
    if start_egress is not None and end_egress is not None and end_egress >= start_egress:
        run_egress = end_egress - start_egress
        record["egress_bytes"] = run_egress
        record_network_usage(run_egress)

    append_run_record(record)


if __name__ == "__main__":
    main()
