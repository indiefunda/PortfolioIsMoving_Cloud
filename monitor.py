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

import atexit
import json
import os
import sys
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

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
LOG_FILE = os.path.join(BASE_DIR, "monitor.log")
LOCK_FILE = os.path.join(BASE_DIR, "monitor.lock")

# Keep only the most recent runs in the history file (keeps it small on disk).
RUN_HISTORY_LIMIT = 500

# Google Cloud "Always Free" egress allowance from North America (bytes).
# The VM lives in us-central1 (N. America), so the limit is 1 GB/month.
# See https://cloud.google.com/free/docs/free-cloud-features
FREE_EGRESS_MONTHLY_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB

# US Eastern timezone (NY market). zoneinfo uses the OS timezone database on
# Linux (always present on Debian) and the 'tzdata' PyPI package on Windows,
# so it imports cleanly even under cron's minimal environment (cron
# historically failed to import pytz).
EASTERN = ZoneInfo("America/New_York")

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
# A simple cross-platform advisory lock so that if monitor.py ever gets
# launched twice at the same moment (e.g. two overlapping cron fires), the
# read-modify-write of run_history.json / state.json / network_usage.json
# can't race and silently drop records. On Linux this uses flock(); on other
# OSes it degrades to no-op (still safe, just not locked).
try:
    import fcntl  # POSIX only

    def _lock_file(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock_file(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
except ImportError:  # Windows / non-POSIX
    def _lock_file(f):
        pass

    def _unlock_file(f):
        pass


def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _write_json(path, data):
    # Write atomically: write to a temp file, fsync, then os.replace() over
    # the real path. A crash mid-write can never leave a truncated/corrupt
    # JSON that _read_json would silently reset to defaults.
    tmp = os.path.join(os.path.dirname(path) or ".",
                       "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _acquire_single_instance():
    """
    Try to become the only running monitor instance.

    Returns a handle the caller must release (via _release_single_instance),
    or None if another run is already in progress. On POSIX this uses a
    non-blocking flock; on Windows (no fcntl) it falls back to an
    exclusive-create pidfile with a staleness check so a crashed run's file
    can't block the next one forever.
    """
    try:
        import fcntl
        fh = open(LOCK_FILE, "w", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(str(os.getpid()))
            fh.flush()
            return fh
        except OSError:
            fh.close()
            return None
    except ImportError:
        pass  # no fcntl -> use the pidfile fallback below

    # Windows fallback: exclusive-create pidfile.
    try:
        if os.path.exists(LOCK_FILE):
            try:
                stale_pid = int(open(LOCK_FILE, encoding="utf-8").read().strip() or "0")
            except (OSError, ValueError):
                stale_pid = 0
            if stale_pid:
                try:
                    os.kill(stale_pid, 0)  # raises if that pid is gone
                    return None           # another instance is still alive
                except OSError:
                    pass                  # stale -> overwrite below
        fh = open(LOCK_FILE, "x", encoding="utf-8")
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except (FileExistsError, OSError):
        return None


def _release_single_instance(fh):
    try:
        if fh is not None:
            fh.close()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    except Exception:
        pass


def load_config():
    return _read_json(CONFIG_FILE, {})


def load_secrets():
    return _read_json(SECRETS_FILE, {})


def load_state():
    return _read_json(STATE_FILE, {"date": "", "alert_levels": {}, "daily_usage": {}})


def save_state(state):
    # Lock so two overlapping runs can't clobber each other's alert levels.
    try:
        with open(STATE_FILE, "a+", encoding="utf-8") as lock_handle:
            _lock_file(lock_handle)
            _write_json(STATE_FILE, state)
            _unlock_file(lock_handle)
    except Exception as exc:
        print(f"  [error] could not write state: {exc}", file=sys.stderr)


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
        # Lock the history file while we read-modify-write it. Without this,
        # two overlapping runs (e.g. the old double-cron bug) could both read
        # the same list, both insert, and one overwrite would erase the other
        # run's record — making runs look like they "never happened".
        with open(RUN_HISTORY_FILE, "a+", encoding="utf-8") as lock_handle:
            _lock_file(lock_handle)
            lock_handle.seek(0)
            records = load_run_history()
            records.insert(0, record)  # newest first
            records = records[:RUN_HISTORY_LIMIT]
            _write_json(RUN_HISTORY_FILE, records)
            _unlock_file(lock_handle)
    except Exception as exc:
        print(f"  [error] could not write run history: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Data retention / cleanup
# ---------------------------------------------------------------------------
# The VM is a tiny free e2-micro instance with a small boot disk. These
# routines run once per cron fire (cheap) to make sure the files that grow
# over time never balloon: the log, the run history, and the per-day egress
# tally are all trimmed so we only keep the last ~7 days. This stops the disk
# from slowly filling up with garbage on a weak free server.
RETENTION_DAYS = 7
LOG_MAX_BYTES = 1024 * 1024  # keep monitor.log bounded under ~1 MB


def _is_within_days(timestamp_str, days):
    """True if a record timestamp like '2026-08-13 11:50:01 EDT' (or just a
    'YYYY-MM-DD' date) is within the last `days` days."""
    if not timestamp_str:
        return True  # keep records with no timestamp rather than drop them
    try:
        date_part = str(timestamp_str).split()[0]
        rec_date = datetime.strptime(date_part, "%Y-%m-%d").date()
    except (ValueError, IndexError):
        return True
    cutoff = datetime.now(EASTERN).date() - timedelta(days=days)
    return rec_date >= cutoff


def cleanup_old_data():
    """Trim the files that accumulate over time so the free VM never fills
    its small disk. Call once at the start of every run (cheap)."""
    # 1) monitor.log -> keep only the last ~1 MB (bounded disk usage). This
    #    file is appended to by cron every run and would otherwise grow
    #    forever; trimming by size is the robust way to bound it.
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            with open(LOG_FILE, "rb") as f:
                f.seek(-LOG_MAX_BYTES, os.SEEK_END)
                tail = f.read()
            with open(LOG_FILE, "wb") as f:
                f.write(tail)
    except Exception as exc:
        print(f"  [error] could not trim log: {exc}", file=sys.stderr)

    # 2) run_history.json -> drop records older than RETENTION_DAYS so we only
    #    keep about a week of history, not months.
    try:
        if os.path.exists(RUN_HISTORY_FILE):
            with open(RUN_HISTORY_FILE, "a+", encoding="utf-8") as lock_handle:
                _lock_file(lock_handle)
                lock_handle.seek(0)
                records = load_run_history()
                kept = [r for r in records
                        if _is_within_days(r.get("timestamp"), RETENTION_DAYS)]
                if len(kept) != len(records):
                    _write_json(RUN_HISTORY_FILE, kept)
                _unlock_file(lock_handle)
    except Exception as exc:
        print(f"  [error] could not trim run history: {exc}", file=sys.stderr)

    # 3) network_usage.json -> keep only the last RETENTION_DAYS of per-day
    #    tallies (the running monthly total is kept as-is).
    try:
        if os.path.exists(NETWORK_USAGE_FILE):
            with open(NETWORK_USAGE_FILE, "a+", encoding="utf-8") as lock_handle:
                _lock_file(lock_handle)
                lock_handle.seek(0)
                data = load_network_usage()
                days = data.get("days", {})
                if days:
                    kept_days = {
                        d: v for d, v in days.items()
                        if _is_within_days(d, RETENTION_DAYS)
                    }
                    if len(kept_days) != len(days):
                        data["days"] = kept_days
                        save_network_usage(data)
                _unlock_file(lock_handle)
    except Exception as exc:
        print(f"  [error] could not trim network usage: {exc}", file=sys.stderr)


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
    try:
        # Lock the tally while we read-modify-write it, so two overlapping
        # runs can't both start from the same baseline and lose each other's
        # bytes (which would under-count our free-tier egress).
        with open(NETWORK_USAGE_FILE, "a+", encoding="utf-8") as lock_handle:
            _lock_file(lock_handle)
            lock_handle.seek(0)
            data = load_network_usage()
            if data.get("month") != month:
                # New month - start a fresh tally for this month.
                data = {"month": month, "monthly_bytes": 0, "days": {}}
            data["monthly_bytes"] = data.get("monthly_bytes", 0) + run_egress_bytes
            days = data.setdefault("days", {})
            days[day] = int(days.get(day, 0)) + run_egress_bytes
            save_network_usage(data)
            _unlock_file(lock_handle)
        return data
    except Exception as exc:
        print(f"  [error] could not record network usage: {exc}", file=sys.stderr)
        return load_network_usage()


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
    """
    Return the live price from twelvedata as {symbol: current_price}. The
    previous-close anchor is fetched separately from Yahoo (see get_prices),
    so the % move is always measured against the real last trading day's close.
    """
    global last_usage, last_call_bytes
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
                # The whole chunk came back in ONE response body; attribute
                # an equal share of its bytes to each symbol in the chunk so
                # the per-symbol display doesn't overcount by 8x.
                last_call_bytes[chunk[0].upper()] = body_bytes // len(chunk)
            else:
                # A batch of multiple symbols in one response body.
                for sym, q in data.items():
                    if isinstance(q, dict) and "price" in q:
                        live[sym.upper()] = float(q["price"])
                        last_call_bytes[sym.upper()] = body_bytes // len(chunk)
        except Exception as exc:
            print(f"  [error] twelvedata price: {exc}", file=sys.stderr)

    if used_min is not None:
        last_usage = {"used_min": used_min, "left_min": left_min, "limit_min": 8}
    return live


def _fetch_finnhub(symbols, api_key):
    """
    Return the live price from finnhub as {symbol: current_price}. The
    previous-close anchor is fetched separately from Yahoo (see get_prices),
    so the % move is always measured against the real last trading day's close
    (Finnhub's own 'pc' field is unreliable and can report a stale/wrong
    previous close, which caused wrong % moves like HUIZ showing -22%).
    """
    global last_usage, last_call_bytes
    live = {}
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
                live[sym] = float(data["c"])
                last_call_bytes[sym] = len(resp.content)
        except Exception as exc:
            print(f"  [error] finnhub {sym}: {exc}", file=sys.stderr)
    if used_min:
        last_usage = {"used_min": used_min, "left_min": 60 - used_min, "limit_min": 60}
    return live


def _last_completed_close(chart):
    """
    Return the close of the LAST COMPLETED trading day from a Yahoo chart
    response, or None if it can't be determined.

    Yahoo's 'chartPreviousClose' meta field is NOT reliable — after a data
    glitch or a one-off spike it can point several days back (e.g. HUIZ
    reported $1.66 from 08/07 instead of $1.29 from 08/13). Instead we read
    the real daily closes from the chart's timestamp + quote.close arrays and
    take the close of the most recent day that is strictly BEFORE today (ET),
    i.e. the last fully-finished trading day. That is the correct anchor for
    measuring today's % move.
    """
    try:
        timestamps = chart.get("timestamp") or []
        closes = (chart.get("indicators", {})
                  .get("quote", [{}])[0].get("close") or [])
        if not timestamps or len(closes) < len(timestamps):
            return None
        today_et = datetime.now(EASTERN).date()
        # Walk from the newest bar backwards; the first bar whose date is
        # strictly before today is the last completed trading day.
        for i in range(len(timestamps) - 1, -1, -1):
            bar_date = datetime.fromtimestamp(timestamps[i], ZoneInfo("UTC")).date()
            if bar_date < today_et:
                close = closes[i]
                if close:
                    return float(close)
                return None
    except Exception:
        return None
    return None


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
            prev_close = _last_completed_close(chart) or meta.get("previousClose")
            if current is not None:
                result[sym] = (float(current), float(prev_close) if prev_close else float(current))
                last_call_bytes[sym] = len(resp.content)
        except Exception as exc:
            print(f"  [error] yahoo {sym}: {exc}", file=sys.stderr)
    return result


def _fetch_yahoo_anchor(symbols):
    """
    Return {symbol: prev_close} where prev_close is the REAL last completed
    trading day's close, read from Yahoo's daily close array (NOT the
    unreliable 'chartPreviousClose' meta field, which can point several days
    back after a data glitch). This is the anchor every % move is measured
    against, and it resets correctly each trading day.
    """
    anchors = {}
    for sym in symbols:
        sym = sym.upper()
        try:
            resp = requests.get(YAHOO_QUOTE.format(symbol=sym), headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            chart = data.get("chart", {}).get("result", [{}])[0]
            prev_close = _last_completed_close(chart)
            if prev_close:
                anchors[sym] = prev_close
                # NOTE: deliberately do NOT record this anchor call's bytes in
                # last_call_bytes. The anchor is a shared/overhead call; the
                # per-symbol byte figure should reflect the live-price call
                # from the configured provider (finnhub/twelvedata), not get
                # overwritten by the anchor's response size.
        except Exception as exc:
            print(f"  [error] yahoo anchor {sym}: {exc}", file=sys.stderr)
    return anchors


def get_prices(symbols, provider=DEFAULT_PROVIDER, api_key=""):
    """
    Get {symbol: (current, prev_close)} for the given symbols.

    current    = live price from the configured provider (finnhub/twelvedata/yahoo)
    prev_close = the REAL last trading day's close, always fetched from Yahoo.

    Measuring every % move against Yahoo's previous close (instead of the
    provider's own 'previous close' field) means alerts always relate to the
    last trading day's close and reset correctly each day, even when a
    provider reports a stale value.
    """
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    last_call_bytes.clear()
    if not symbols:
        return {}, {}
    provider = (provider or DEFAULT_PROVIDER).lower()

    # Live prices from the configured provider.
    if provider == "twelvedata" and api_key:
        live = _fetch_twelvedata(symbols, api_key)
    elif provider == "finnhub" and api_key:
        live = _fetch_finnhub(symbols, api_key)
    else:
        # Yahoo is the provider: it already returns (current, prev_close).
        yahoo = _fetch_yahoo(symbols)
        return yahoo, dict(last_call_bytes)

    # Anchor (last trading day's close) always comes from Yahoo.
    anchors = _fetch_yahoo_anchor(symbols)

    # Merge: live price + Yahoo anchor. If Yahoo fails for a symbol, fall back
    # to the live price itself (treat it as flat) so we never crash on it.
    result = {}
    for sym in symbols:
        if sym in live:
            current = live[sym]
            prev_close = anchors.get(sym, current)
            result[sym] = (current, prev_close)
    return result, dict(last_call_bytes)


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


def format_alert(symbol, current, prev_close, pct, threshold, retrigger_step=3.0):
    direction = "🚨 UP" if pct > 0 else "📉 DOWN"
    arrow = "▲" if pct > 0 else "▼"
    # Next re-trigger level: same direction, step further out.
    next_level = (abs(pct) + retrigger_step) * (1 if pct > 0 else -1)
    return (
        f"{direction} {symbol}\n"
        f"{arrow} {abs(pct):.1f}%  (threshold {threshold}%)\n"
        f"Price: ${current:.2f}  |  Prev close: ${prev_close:.2f}\n"
        f"Next alert if it reaches {next_level:+.1f}%"
    )


def is_market_hours(now_et):
    if now_et.weekday() >= 5:  # Sat/Sun
        return False
    open_t = dtime(9, 25)
    close_t = dtime(16, 5)
    return open_t <= now_et.time() <= close_t


def main():
    # Single-instance guard: if a previous run is still going (overlapping
    # cron fire, or the panel's "Run now" hitting at the same minute as
    # cron), skip instead of sending duplicate alerts or racing the JSON
    # files. The handle is released automatically on every exit path.
    lock_fh = _acquire_single_instance()
    if lock_fh is None:
        print("Another monitor run is already in progress - skipping this run.")
        return
    atexit.register(_release_single_instance, lock_fh)

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

    # Trim old log/history/egress data first so the free VM's small disk
    # never fills up with accumulated garbage. Cheap: runs every cron fire.
    cleanup_old_data()

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
    # The re-trigger step: once a stock alerts, it only alerts again when the
    # move intensifies by at least this many percentage points in the SAME
    # direction (e.g. 5% -> then 8% -> then 11% for a step of 3).
    retrigger_step = float(config.get("retrigger_step_pct", 3.0))
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
        # Outside market hours: exit silently WITHOUT writing a run record.
        # This keeps the run history clean (no "outside hours" spam) and
        # avoids unnecessary disk writes. cron only fires during market-window
        # hours now, so this is just the small DST boundary overshoot.
        print(f"Skipping: outside market hours ({now_et.strftime('%a %H:%M %Z')}).")
        return

    state = load_state()
    today = now_et.strftime("%Y-%m-%d")
    if state.get("date") != today:
        # New day: reset the per-stock alert levels to 0 so each stock can
        # trigger fresh again today.
        state = {"date": today, "alert_levels": {}, "daily_usage": {}}

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

        # --- Direction-aware, step-based alerting (per stock, per day) ---
        # Each stock remembers the % move that last triggered an alert today.
        # It alerts again ONLY when the move intensifies by >= retrigger_step
        # percentage points in the SAME direction. Opposite-direction moves or
        # smaller fluctuations never re-trigger.
        alerted = False
        alert_levels = state.setdefault("alert_levels", {})
        last_level = alert_levels.get(symbol)

        if last_level is None:
            # First alert of the day: fire when it crosses the threshold.
            should_alert = abs(pct) >= threshold
        else:
            # Same direction AND intensified by at least the step amount.
            same_direction = (pct > 0) == (last_level > 0)
            intensified = abs(pct) >= abs(last_level) + retrigger_step
            should_alert = same_direction and intensified

        if should_alert:
            msg = format_alert(symbol, current, prev_close, pct, threshold, retrigger_step)
            if send_telegram(token, chat_id, msg):
                print(f"  -> Alert sent for {symbol} at {pct:+.2f}%")
                alert_levels[symbol] = pct  # remember this level for re-triggers
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
