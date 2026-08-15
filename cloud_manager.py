#!/usr/bin/env python3
"""
PortfolioIsMoving (Cloud) - local Windows control panel.

Serves an HTML dashboard that manages your Google Cloud VM. It wraps the
official `gcloud` CLI so you never type SSH commands. Authentication uses
Google's own OAuth login (opens in your browser) - the app never sees your
username or password.

Run it with:   python cloud_manager.py
Then open:     http://localhost:8000

Requires: Python + the Google Cloud CLI (gcloud). If gcloud is missing, the
app shows install instructions.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_local.json")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets_local.json")
PANEL_LOG_FILE = os.path.join(BASE_DIR, "panel.log")
PANEL_LOG_MAX_BYTES = 1024 * 1024
PORT = 8000

# VM defaults
VM_NAME = "stock-monitor"
VM_ZONE = "us-central1-a"      # free e2-micro region
VM_MACHINE = "e2-micro"
VM_IMAGE = "debian-12"
VM_PROJECT = None              # set after auth via gcloud config

# Zones that offer the free e2-micro machine type. The app tries each in
# order until one has capacity (some zones run out temporarily).
VM_ZONES = [
    "us-central1-a",
    "us-central1-b",
    "us-central1-c",
    "us-east1-b",
    "us-east1-c",
    "us-west1-a",
    "us-west1-b",
    "us-east4-a",
    "us-east4-b",
    "us-east4-c",
]

DEFAULT_CONFIG = {
    "tickers": [],
    "threshold_pct": 5.0,
    "retrigger_step_pct": 3.0,
    "enabled": True,
    "provider": "finnhub",
}
DEFAULT_SECRETS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "finnhub_key": "",
    "twelvedata_key": "",
}


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
    # Write atomically (temp file + os.replace) so a crash mid-write can't
    # leave a corrupt JSON that load_config/load_secrets silently reset.
    tmp = os.path.join(os.path.dirname(path) or ".",
                       "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _log_panel(message):
    """Append one line to the panel's log file, keeping it bounded ~1 MB."""
    try:
        with open(PANEL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(message + "\n")
        if os.path.getsize(PANEL_LOG_FILE) > PANEL_LOG_MAX_BYTES:
            with open(PANEL_LOG_FILE, "rb") as f:
                f.seek(-PANEL_LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = f.read()
            with open(PANEL_LOG_FILE, "wb") as f:
                f.write(tail)
    except Exception:
        pass


def load_config():
    cfg = _read_json(CONFIG_FILE, dict(DEFAULT_CONFIG))
    merged = dict(DEFAULT_CONFIG); merged.update(cfg or {}); return merged


def load_secrets():
    sec = _read_json(SECRETS_FILE, dict(DEFAULT_SECRETS))
    merged = dict(DEFAULT_SECRETS); merged.update(sec or {}); return merged


def save_config(cfg): _write_json(CONFIG_FILE, cfg)
def save_secrets(sec): _write_json(SECRETS_FILE, sec)


# ---------------------------------------------------------------------------
# gcloud helpers
# ---------------------------------------------------------------------------
# Known gcloud install locations (Windows), used if gcloud isn't on PATH.
# $USERPROFILE and $LOCALAPPDATA are expanded at runtime so this works for
# any user, not just the original author.
GCLOUD_CANDIDATES = [
    "gcloud",
    r"$USERPROFILE\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"$USERPROFILE\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud",
    r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"$LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
]


def _find_gcloud():
    """Return the gcloud command to use, or None if not found."""
    # 1. On PATH
    found = shutil.which("gcloud")
    if found:
        return found
    # 2. Known install locations (expand env-var placeholders)
    localappdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    for cand in GCLOUD_CANDIDATES:
        if cand.startswith("$LOCALAPPDATA") and localappdata:
            cand = cand.replace("$LOCALAPPDATA", localappdata)
        if cand.startswith("$USERPROFILE") and userprofile:
            cand = cand.replace("$USERPROFILE", userprofile)
        if cand and os.path.exists(cand):
            return cand
    return None


def gcloud_available():
    return _find_gcloud() is not None


# TTL cache for the slow "read" endpoints (/api/status, /api/logs,
# /api/network, /api/timer). Each of those spawns several gcloud/SSH
# subprocesses, so without caching every refresh is expensive and spammable.
#
# IMPORTANT: we deliberately do NOT serialize gcloud calls globally. The
# panel's initial page load fires /api/status, /api/timer, /api/logs and
# /api/network at the same time; a global lock made the fast /api/status
# wait behind slow SSH commands (each can take up to its 60s timeout when
# the VM is stopped), which froze the panel on "Checking...". Instead,
# coalescing is per cache key: parallel requests for the SAME endpoint share
# one computation, while different endpoints still run concurrently.
_cache = {}
_key_locks = {}
_cache_guard = threading.Lock()


def _cached(key, ttl_seconds, fn):
    with _cache_guard:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:
        now = time.time()
        entry = _cache.get(key)
        if entry is not None and now - entry[0] < ttl_seconds:
            return entry[1]
        value = fn()
        _cache[key] = (now, value)
        return value


def run_gcloud(args, timeout=120):
    """Run a gcloud command and return (success, stdout, stderr)."""
    gcloud = _find_gcloud()
    if not gcloud:
        return False, "", "gcloud not found. Install the Google Cloud CLI."
    cmd = [gcloud] + args
    _log_panel(f"gcloud {' '.join(args[:5])} (timeout {timeout}s)")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout, proc.stderr
    except FileNotFoundError:
        return False, "", "gcloud not found. Install the Google Cloud CLI."
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out."


def get_project():
    """Return the current gcloud project id, auto-selecting one if none is set."""
    ok, out, _ = run_gcloud(["config", "get-value", "project", "--quiet"])
    if ok and out.strip() and out.strip() != "(unset)":
        return out.strip()
    # No active project - try to pick the first available one automatically.
    ok2, out2, _ = run_gcloud(["projects", "list", "--format=value(projectId)",
                               "--limit=1", "--quiet"], timeout=60)
    if ok2 and out2.strip():
        project = out2.strip().splitlines()[0].strip()
        run_gcloud(["config", "set", "project", project, "--quiet"], timeout=60)
        return project
    return None


def auth_status():
    """Return auth info: is authenticated + which account."""
    if not gcloud_available():
        return {"installed": False, "authed": False, "account": None}
    ok, out, _ = run_gcloud(["auth", "list", "--filter=status:ACTIVE",
                             "--format=value(account)", "--quiet"], timeout=60)
    account = out.strip() if ok and out.strip() else None
    return {"installed": True, "authed": bool(account), "account": account}


def find_vm_zone():
    """Return the zone where the VM actually lives, or None if it doesn't exist."""
    project = get_project()
    if not project:
        return None
    # Single fast call: list instances across all zones and find this VM's zone.
    ok, out, _ = run_gcloud(
        ["compute", "instances", "list",
         "--filter=name=" + VM_NAME,
         "--format=value(zone)", "--quiet"], timeout=60)
    if ok and out.strip():
        # zone comes back as a full URL; extract just the zone name.
        zone = out.strip().splitlines()[0].strip()
        if "/" in zone:
            zone = zone.rstrip("/").split("/")[-1]
        return zone
    return None


def vm_status():
    """Return the VM's status, or None if it doesn't exist."""
    zone = find_vm_zone()
    if not zone:
        return None
    ok, out, _ = run_gcloud(
        ["compute", "instances", "describe", VM_NAME, "--zone", zone,
         "--format=value(status)", "--quiet"], timeout=60)
    if ok and out.strip():
        return out.strip()
    return None


def get_vm_home(zone):
    """
    Return the home directory of the SSH user on the VM (e.g. /home/<user>).
    This is resolved dynamically so the app works for any Google account /
    VM, not just the original author's. Falls back to /home/<current OS user>
    if the SSH command fails.
    """
    ok, home, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "echo $HOME", "--quiet"], timeout=60)
    if ok and home.strip():
        return home.strip()
    # Fallback: use the local OS username (matches the default SSH username
    # gcloud uses for the VM's owner).
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"/home/{user}"


def fetch_vm_run_history():
    """
    Read the monitor's run_history.json from the VM. Returns a list of run
    records (newest first), or [] if the VM/file isn't reachable yet.
    """
    zone = find_vm_zone()
    if not zone:
        return []
    home = get_vm_home(zone)
    ok, out, err = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", f"cat {home}/run_history.json 2>/dev/null || echo '[]'",
        "--quiet"], timeout=60)
    if not ok:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_vm_network_usage():
    """
    Read the monitor's cumulative network_usage.json from the VM. Returns a
    dict with the monthly + per-day egress tally, or {} if not reachable yet.
    """
    zone = find_vm_zone()
    if not zone:
        return {}
    home = get_vm_home(zone)
    ok, out, err = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", f"cat {home}/network_usage.json 2>/dev/null || echo '{{}}'",
        "--quiet"], timeout=60)
    if not ok:
        return {}
    try:
        data = json.loads(out)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def fetch_vm_timer_status():
    """
    Check the monitor's cron setup on the VM: whether the cron daemon is
    running, whether the cron job is installed, and when it next fires (UTC).
    Returns a dict, or None if the VM isn't reachable.
    """
    zone = find_vm_zone()
    if not zone:
        return None
    # Is the cron daemon running and enabled at boot?
    ok_active, active_out, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "systemctl is-active cron 2>/dev/null || echo 'inactive'",
        "--quiet"], timeout=60)
    cron_active = (active_out.strip() if ok_active else "unknown")
    ok_enabled, enabled_out, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "systemctl is-enabled cron 2>/dev/null || echo 'disabled'",
        "--quiet"], timeout=60)
    cron_enabled = (enabled_out.strip() if ok_enabled else "unknown")

    # Is the cron job installed? The cron line contains "monitor.py" (the
    # marker unique to our job); the schedule never contains the word
    # "portfolioismoving", so we MUST grep for "monitor.py". Grepping the
    # wrong marker made the panel always report "Schedule is NOT installed"
    # even when the job was actually present.
    ok, out, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "crontab -l 2>/dev/null | grep 'monitor.py' || true",
        "--quiet"], timeout=60)
    cron_line = out.strip() if ok else ""
    installed = bool(cron_line)

    # When did the monitor last run? (from run_history.json, newest first)
    last_run = None
    history = fetch_vm_run_history()
    if history and isinstance(history, list) and history:
        last_run = history[0].get("timestamp")

    return {
        "active": "active" if installed else "inactive",
        "cron_daemon_active": cron_active,
        "cron_daemon_enabled": cron_enabled,
        "cron_line": cron_line,
        "next_fire_utc": compute_next_cron_utc(cron_line) if installed else None,
        "last_run": last_run,
    }


def _expand_cron_field(field, low, high):
    """
    Expand a single cron field (e.g. "*", "*/10", "13-21", "1,5") into a set
    of allowed integer values in [low, high].
    """
    result = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, step_str = part.partition("/")
            try:
                step = int(step_str)
            except ValueError:
                step = 1
        if part == "*":
            base = range(low, high + 1)
        elif "-" in part:
            try:
                a, b = part.split("-")
                base = range(int(a), int(b) + 1)
            except ValueError:
                base = []
        else:
            try:
                base = [int(part)]
            except ValueError:
                base = []
        for v in base:
            if low <= v <= high and (v - low) % step == 0:
                result.add(v)
    return result


def compute_next_cron_utc(cron_line, now=None):
    """
    Compute the next time a cron line will fire, in UTC. Takes the first 5
    fields of the cron line (minute hour dom month dow) and returns a string
    like "2026-08-12 13:10:00 UTC", or None if it can't be determined.
    """
    parts = (cron_line or "").split()
    if len(parts) < 5:
        return None
    min_field, hour_field, dom_field, mon_field, dow_field = parts[:5]
    try:
        minutes = _expand_cron_field(min_field, 0, 59)
        hours = _expand_cron_field(hour_field, 0, 23)
        doms = _expand_cron_field(dom_field, 1, 31)
        months = _expand_cron_field(mon_field, 1, 12)
        dows = _expand_cron_field(dow_field, 0, 6)  # cron: 0=Sun..6=Sat
    except Exception:
        return None
    if not minutes or not hours or not doms or not months or not dows:
        return None
    dom_star = dom_field.strip() in ("*", "")
    dow_star = dow_field.strip() in ("*", "")
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    for day_offset in range(0, 366):
        d = now + timedelta(days=day_offset)
        if d.month not in months:
            continue
        py_dow = d.weekday()          # Mon=0..Sun=6
        cron_dow = (py_dow + 1) % 7  # Sun=0..Sat=6
        # Standard cron day matching: when BOTH dom and dow are restricted
        # the day matches if EITHER matches; when only one is restricted it
        # rules alone. (This mirrors Vixie cron's behaviour.)
        if dom_star and dow_star:
            day_ok = True
        elif dom_star:
            day_ok = cron_dow in dows
        elif dow_star:
            day_ok = d.day in doms
        else:
            day_ok = (d.day in doms) or (cron_dow in dows)
        if not day_ok:
            continue
        for h in sorted(hours):
            if day_offset == 0 and h < now.hour:
                continue
            for m in sorted(minutes):
                candidate = d.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate > now:
                    return candidate.strftime("%Y-%m-%d %H:%M:%S UTC")
    return None


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PortfolioIsMoving — Cloud</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --border:#2a2e38; --text:#e8eaf0;
          --muted:#8b90a0; --accent:#3b82f6; --ok:#22c55e; --err:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--text); padding:16px; }
  h1 { font-size:22px; margin:4px 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:12px; padding:16px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 12px; }
  label { display:block; color:var(--muted); font-size:12px; margin:10px 0 4px; }
  input, select { width:100%; padding:12px; border-radius:8px;
                  border:1px solid var(--border); background:#12151c;
                  color:var(--text); font-size:15px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; }
  .chip { display:inline-flex; align-items:center; gap:6px;
          background:#232733; border:1px solid var(--border);
          border-radius:20px; padding:6px 12px; font-size:14px; margin:4px; }
  .chip button { background:none; border:none; color:var(--err); font-size:16px; cursor:pointer; }
  button { border:none; border-radius:8px; padding:12px 16px; font-size:15px;
           font-weight:600; cursor:pointer; margin-top:8px; }
  .btn-primary { background:var(--accent); color:#fff; width:100%; }
  .btn-ghost { background:#232733; color:var(--text); }
  .btn-ok { background:#14532d; color:#fff; }
  .btn-warn { background:#7c2d12; color:#fff; }
  .msg { display:none; padding:12px; border-radius:8px; margin-top:12px; font-size:14px; }
  .msg.ok { display:block; background:#13281a; color:var(--ok); }
  .msg.err { display:block; background:#2a1416; color:var(--err); }
  .status { padding:12px; border-radius:8px; background:#12151c; font-size:14px; margin-bottom:8px; }
  .status b { color:var(--text); }
  .status .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }
  .dot.green { background:var(--ok); } .dot.red { background:var(--err); } .dot.gray { background:var(--muted); }
  .toggle { display:flex; align-items:center; justify-content:space-between; }
  .switch { position:relative; width:52px; height:30px; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; inset:0; background:#2a2e38; border-radius:30px; transition:.3s; }
  .slider:before { content:""; position:absolute; height:22px; width:22px; left:4px; top:4px;
                   background:#fff; border-radius:50%; transition:.3s; }
  input:checked + .slider { background:var(--accent); }
  input:checked + .slider:before { transform:translateX(22px); }
  .hint { color:var(--muted); font-size:12px; margin-top:4px; }
  .log { background:#0a0c10; border:1px solid var(--border); border-radius:8px;
         padding:10px; font-family:monospace; font-size:12px; color:#9fe8a0;
         max-height:200px; overflow:auto; white-space:pre-wrap; }
  .tablewrap { overflow:auto; max-height:420px; border:1px solid var(--border);
               border-radius:8px; margin-top:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border);
           white-space:nowrap; }
  th { position:sticky; top:0; background:#12151c; color:var(--muted); font-size:12px;
       text-transform:uppercase; letter-spacing:.03em; }
  td.pct { text-align:right; font-variant-numeric:tabular-nums; }
  .pos { color:var(--ok); } .neg { color:var(--err); }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px;
           font-weight:600; }
  .badge.ok { background:#14532d; color:#a7f3d0; }
  .badge.err { background:#7c2d12; color:#fecaca; }
  .badge.warn { background:#713f12; color:#fde68a; }
  .badge.gray { background:#2a2e38; color:#cbd5e1; }
  .empty { color:var(--muted); text-align:center; padding:24px; font-size:13px; }
  details { font-size:12px; color:var(--muted); }
  summary { cursor:pointer; color:var(--accent); }
</style>
</head>
<body>
  <h1>☁️ PortfolioIsMoving — Cloud</h1>
  <div class="sub">Manage your free Google Cloud server from here. No SSH needed.</div>
  <div class="msg" id="msg"></div>

  <!-- Step 1: Google auth -->
  <div class="card">
    <h2>1. Connect to Google</h2>
    <div class="status" id="authStatus">Checking...</div>
    <button class="btn-ghost" onclick="authGoogle()">🔑 Authenticate to Google</button>
    <div class="hint">Opens Google's own login page in your browser. Your password is never given to this app.</div>
  </div>

  <!-- Step 2: Create server -->
  <div class="card">
    <h2>2. Create your free server</h2>
    <div class="status" id="vmStatus">—</div>
    <button class="btn-ok" onclick="createVM()">🖥️ Create free server (e2-micro)</button>
    <button class="btn-ghost" onclick="refreshStatus()">↻ Refresh status</button>
    <div class="hint">Creates the free Google Cloud VM (e2-micro, $0/month).</div>
  </div>

  <!-- Step 3: Config -->
  <div class="card">
    <h2>3. Your portfolio</h2>
    <label>Stocks</label>
    <div class="row" id="chips"></div>
    <div class="row" style="margin-top:8px">
      <input id="tickerInput" placeholder="e.g. HUIZ, AAPL" style="flex:1">
      <button class="btn-ghost" onclick="addTicker()">Add</button>
    </div>
    <label>Alert threshold (%)</label>
    <input id="threshold" type="number" step="0.5" min="0.1">
    <label>Re-trigger step (%)</label>
    <input id="retriggerStep" type="number" step="0.5" min="0.1">
    <div class="hint">Once a stock alerts, it alerts again only when the move intensifies by this many % in the same direction (resets daily). Example: threshold 5% + step 3% → alerts at 5%, then 8%, then 11%.</div>
    <label>Provider</label>
    <select id="provider" onchange="updateProviderUI()">
      <option value="finnhub">Finnhub — real-time</option>
      <option value="twelvedata">Twelve Data — real-time</option>
      <option value="yahoo">Yahoo — ~15 min delayed, no key</option>
    </select>
    <div id="apikeyRow">
      <label id="apikeyLabel">Finnhub API key</label>
      <input id="apikey" type="text" placeholder="paste your key">
    </div>
    <label>Telegram bot token</label>
    <input id="token" type="text" placeholder="123456789:AAH...">
    <label>Telegram chat id</label>
    <input id="chatid" type="text" placeholder="e.g. 123456789">
    <div class="toggle" style="margin-top:14px">
      <span>Run the monitor</span>
      <label class="switch"><input type="checkbox" id="enabledToggle"><span class="slider"></span></label>
    </div>
    <button class="btn-primary" onclick="uploadConfig()">🚀 Upload config to server</button>
    <div class="hint">Sends your stocks/keys/Telegram to the cloud server. No restart needed.</div>
  </div>

  <!-- Step 4: Cost safety -->
  <div class="card">
    <h2>4. Cost safety (free-tier protection)</h2>
    <div class="status" id="budgetStatus">—</div>
    <button class="btn-ghost" onclick="setBudget()">💲 Set $1 monthly budget alert</button>
    <div class="hint">Emails you the moment anything would cost money. Protects your card.</div>
  </div>

  <!-- Step 5: Usage -->
  <div class="card">
    <h2>5. Usage & health</h2>
    <div class="status" id="usageStatus">—</div>
    <button class="btn-ghost" onclick="checkUsage()">📊 Open my real Google billing page</button>
    <div class="hint">Opens Google's official billing console in a new tab — the real source of truth for your cost.</div>
    <button class="btn-ghost" onclick="testAlert()">📲 Send test Telegram alert</button>
    <div class="log" id="log">Command output will appear here.</div>
    <label style="margin-top:16px">Network egress — this monitor only</label>
    <div class="status" id="netStatus">—</div>
    <button class="btn-ghost" onclick="loadNetwork()">↻ Refresh network usage</button>
    <div class="hint">This shows the outbound traffic <b>this monitor</b> used (measured on the server during its runs). It is <b>NOT the whole VM's usage</b> — other apps on the same server are not counted. Your free tier allows 1 GB of outbound traffic per month (from N. America).</div>
  </div>

  <!-- Step 6: Run history -->
  <div class="card">
    <h2>6. What the monitor saw (run history)</h2>
    <div class="status" id="timerStatus">—</div>
    <button class="btn-ghost" onclick="loadTimer()">↻ Check schedule</button>
    <button class="btn-ok" onclick="runNow()">▶ Run now (test)</button>
    <div class="hint">The monitor is triggered by a cron job on the server every 10 min, Mon–Fri. This shows whether that schedule is installed and when it next fires (UTC).</div>
    <div class="status" id="logStatus">—</div>
    <button class="btn-ghost" onclick="loadLogs()">↻ Refresh run history</button>
    <div class="hint">Shows every run from the cloud server: when it ran, how long it took, what prices it saw (even when no alert was sent), and whether alerts went out. The newest run is on top.</div>
    <div id="logTableWrap"></div>
  </div>

<script>
let tickers = [];
function $(id){ return document.getElementById(id); }
function showMsg(t, type){ const m=$('msg'); m.textContent=t; m.className='msg '+type;
  setTimeout(()=>{ m.className='msg'; }, 8000); }

function renderChips(){ const el=$('chips'); el.innerHTML='';
  tickers.forEach(t=>{ const c=document.createElement('span'); c.className='chip';
    c.innerHTML=t+' <button onclick="removeTicker(\\''+t+'\\')">&times;</button>'; el.appendChild(c); }); }
function addTicker(){ const v=$('tickerInput').value.trim().toUpperCase();
  if(v && !tickers.includes(v)){ tickers.push(v); renderChips(); } $('tickerInput').value=''; }
function removeTicker(t){ tickers=tickers.filter(x=>x!==t); renderChips(); }
function updateProviderUI(){ const p=$('provider').value;
  $('apikeyRow').style.display=(p==='yahoo')?'none':'block';
  $('apikeyLabel').textContent=(p==='twelvedata')?'Twelve Data API key':'Finnhub API key'; }

async function api(path, body){
  const opts = body ? {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)} : {};
  const r = await fetch(path, opts); return r.json();
}

async function load(){
  const d = await api('/api/config');
  tickers = d.config.tickers || [];
  $('threshold').value = d.config.threshold_pct;
  $('retriggerStep').value = d.config.retrigger_step_pct || 3.0;
  $('provider').value = d.config.provider || 'finnhub';
  $('enabledToggle').checked = !!d.config.enabled;
  const p = d.config.provider || 'finnhub';
  $('apikey').value = d.secrets[p+'_key'] || d.secrets.price_api_key || '';
  $('token').value = d.secrets.telegram_bot_token || '';
  $('chatid').value = d.secrets.telegram_chat_id || '';
  renderChips(); updateProviderUI();
  refreshStatus();
  loadTimer();
  loadLogs();
  loadNetwork();
}

async function refreshStatus(){
  const d = await api('/api/status');
  // Auth
  const a = $('authStatus');
  if(!d.gcloud){ a.innerHTML='<span class="dot red"></span><b>Google CLI not installed.</b> Install it first — see GUIDE-CLOUD.md.'; }
  else if(d.auth.authed){ a.innerHTML='<span class="dot green"></span><b>Connected as:</b> '+(d.auth.account||'?'); }
  else { a.innerHTML='<span class="dot gray"></span><b>Not connected.</b> Click "Authenticate to Google".'; }
  // VM
  const v = $('vmStatus');
  if(d.vm === null){ v.textContent = '— (create your free server below)'; }
  else { v.innerHTML = '<span class="dot '+(d.vm==='RUNNING'?'green':'gray')+'"></span><b>Server status:</b> '+d.vm; }
}

async function authGoogle(){ showMsg('Opening Google login in your browser...','ok');
  const d = await api('/api/auth'); showMsg(d.ok ? '✅ Connected to Google!' : '❌ '+d.error, d.ok?'ok':'err');
  refreshStatus(); }

async function createVM(){
  showMsg('Creating your free server... this takes a minute.','ok');
  const d = await api('/api/create_vm');
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Server created!' : '❌ '+d.error, d.ok?'ok':'err');
  refreshStatus(); }

async function uploadConfig(){
  const p = $('provider').value;
  const apiKey = $('apikey').value.trim();
  const d = await api('/api/upload', {
    tickers, threshold_pct: parseFloat($('threshold').value)||5.0,
    retrigger_step_pct: parseFloat($('retriggerStep').value)||3.0,
    enabled: $('enabledToggle').checked, provider: p,
    // Send the key for the ACTIVE provider only; null leaves the other
    // provider's stored key untouched so you can switch back later.
    finnhub_key: (p === 'finnhub') ? apiKey : null,
    twelvedata_key: (p === 'twelvedata') ? apiKey : null,
    telegram_bot_token: $('token').value.trim(), telegram_chat_id: $('chatid').value.trim(),
  });
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Config uploaded to server!' : '❌ '+d.error, d.ok?'ok':'err'); }

async function setBudget(){
  showMsg('Setting $1 budget alert...','ok');
  const d = await api('/api/budget');
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ $1 budget alert set!' : '❌ '+d.error, d.ok?'ok':'err');
  $('budgetStatus').textContent = d.ok ? 'Budget alert: $1/month' : 'Not set'; }

async function checkUsage(){
  showMsg('Opening your real Google billing page...','ok');
  const d = await api('/api/usage');
  if(d.ok && d.billing_url){
    window.open(d.billing_url, '_blank');
    $('usageStatus').textContent = 'Opened your real Google Cloud billing page in a new tab.';
    $('log').textContent = d.output || '';
  } else {
    $('log').textContent = d.output || '';
    $('usageStatus').textContent = d.error || 'Could not open billing page.';
  } }

async function testAlert(){
  showMsg('Sending test alert...','ok');
  const d = await api('/api/test_alert');
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Test alert sent (check Telegram)' : '❌ '+d.error, d.ok?'ok':'err'); }

function badgeFor(status){
  switch(status){
    case 'ran': return '<span class="badge ok">ran</span>';
    case 'disabled': return '<span class="badge warn">disabled</span>';
    case 'outside_market_hours': return '<span class="badge gray">outside hours</span>';
    case 'error': return '<span class="badge err">error</span>';
    default: return '<span class="badge gray">'+(status||'?')+'</span>';
  }
}

async function loadLogs(){
  $('logStatus').textContent = 'Fetching run history from the server...';
  const d = await api('/api/logs');
  renderLogs(d.logs || []);
}

async function loadTimer(){
  $('timerStatus').textContent = 'Checking the schedule on the server...';
  const d = await api('/api/timer');
  const t = d.timer;
  const el = $('timerStatus');
  if(!t){
    el.innerHTML = '<span class="dot gray"></span><b>Could not reach the server.</b> Make sure the VM is running.';
    return;
  }
  const active = String(t.active || '').trim();
  const daemon = String(t.cron_daemon_active || 'unknown').trim();
  const enabled = String(t.cron_daemon_enabled || 'unknown').trim();

  // Show the cron daemon health first - if cron isn't running, nothing fires.
  let daemonHtml;
  if(daemon === 'active'){
    daemonHtml = 'cron daemon: <span class="badge ok">running</span> · boot: <span class="badge '+(enabled==='enabled'?'ok':'warn')+'">'+(enabled==='enabled'?'enabled':'not enabled')+'</span>';
  } else {
    daemonHtml = 'cron daemon: <span class="badge err">NOT running ('+daemon+')</span> · boot: '+(enabled==='enabled'?'enabled':'not enabled');
  }

  if(active === 'active'){
    el.innerHTML = '<span class="dot green"></span><b>Schedule armed (cron).</b> Next run (UTC): '+(t.next_fire_utc || 'unknown')+
      (t.last_run ? '<br>Last run: '+escapeHtml(t.last_run) : '')+
      '<br>'+daemonHtml+
      (t.cron_line ? '<br><span style="color:var(--muted)">'+escapeHtml(t.cron_line)+'</span>' : '');
  } else {
    el.innerHTML = '<span class="dot red"></span><b>Schedule is NOT installed.</b> Re-upload your config (Step 3) to install it.<br>'+daemonHtml;
  }
}

async function runNow(){
  showMsg('Running monitor.py on the server now...','ok');
  const d = await api('/api/run_now');
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Monitor ran successfully on the server.' : '❌ '+d.error, d.ok?'ok':'err');
  // Refresh the schedule + run history so the new run shows up.
  loadTimer();
  loadLogs();
}

function formatBytes(n){
  if(n==null) return '—';
  const units=['B','KB','MB','GB'];
  let v=n, i=0;
  while(v>=1024 && i<units.length-1){ v/=1024; i++; }
  return (i===0 ? Math.round(v) : v.toFixed(1))+' '+units[i];
}

async function loadNetwork(){
  $('netStatus').textContent = 'Fetching network usage from the server...';
  const d = await api('/api/network');
  renderNetwork(d.network || {}, d.monthly_limit_bytes);
}

function renderNetwork(net, limitBytes){
  const el = $('netStatus');
  if(!net || !net.monthly_bytes){
    el.innerHTML = '<span class="dot gray"></span><b>No network data yet.</b> It appears after the next run of the monitor on the server.';
    return;
  }
  const monthly = net.monthly_bytes || 0;
  const pct = limitBytes ? (monthly/limitBytes*100) : 0;
  const dot = pct >= 80 ? 'red' : (pct >= 50 ? 'gray' : 'green');
  let html = '<span class="dot '+dot+'"></span><b>This monitor, this month ('+net.month+'):</b> '+formatBytes(monthly)+
    ' of '+formatBytes(limitBytes)+' free ('+pct.toFixed(2)+'%)';
  // Show ONLY today's usage, not a growing list of every day in the month.
  const dayKeys = net.days ? Object.keys(net.days).sort() : [];
  if(dayKeys.length){
    const todayKey = dayKeys[dayKeys.length-1];
    html += '<br><b>This monitor, today ('+todayKey+'):</b> '+formatBytes(net.days[todayKey]);
  }
  el.innerHTML = html;
}

function renderLogs(logs){
  const wrap = $('logTableWrap');
  const status = $('logStatus');
  if(!logs.length){
    status.textContent = 'No runs recorded yet.';
    wrap.innerHTML = '<div class="empty">No run history found. If the server is running, it records a row here every 10 minutes during market hours.</div>';
    return;
  }
  status.textContent = logs.length + (logs.length===1?' run':' runs') + ' recorded (newest first).';
  // Show only the 10 most recent runs to keep the table readable.
  const shown = logs.slice(0, 10);
  if(logs.length > shown.length){
    status.textContent += ' Showing the latest '+shown.length+'.';
  }
  let rows = '';
  for(const r of shown){
    const dur = (r.duration_sec!=null) ? r.duration_sec+'s' : '—';
    const alerts = (r.alerts_sent||[]).length;
    const failed = (r.alerts_failed||[]).length;
    let alertCell = (alerts||failed) ? (alerts+' sent'+(failed?' / '+failed+' failed':'')) : '—';
    // Roll up the per-ticker prices into a compact summary.
    let detail = '';
    if(r.prices && r.prices.length){
      const items = r.prices.map(p=>{
        const egress = (p.egress_bytes!=null) ? ' · '+formatBytes(p.egress_bytes) : '';
        if(p.pct==null) return '<li><b>'+p.symbol+'</b>: '+ (p.note||'no data') + egress +'</li>';
        const cls = p.pct>0?'pos':(p.pct<0?'neg':'');
        return '<li><b>'+p.symbol+'</b>: $'+p.current+' (prev $'+p.prev_close+') = <span class="'+cls+'">'+
          (p.pct>0?'+':'')+p.pct+'%</span>'+(p.alert?' <span class="badge ok">ALERT</span>':'')+egress+'</li>';
      }).join('');
      detail = '<details><summary>Prices seen ('+r.prices.length+')</summary><ul style="margin:6px 0 0;padding-left:18px">'+items+'</ul></details>';
    }
    rows += '<tr>'+ 
      '<td>'+escapeHtml(r.timestamp||'—')+'</td>'+
      '<td>'+badgeFor(r.status)+'</td>'+
      '<td class="pct">'+dur+'</td>'+
      '<td>'+escapeHtml(r.provider||'—')+'</td>'+
      '<td class="pct">'+escapeHtml(r.tickers_checked||0)+'</td>'+
      '<td class="pct">'+formatBytes(r.egress_bytes)+'</td>'+
      '<td>'+alertCell+'</td>'+
      '<td>'+(r.error?escapeHtml(r.error):(detail||'—'))+'</td>'+
      '</tr>';
  }
  wrap.innerHTML = '<div class="tablewrap"><table>'+
    '<thead><tr><th>Time (ET)</th><th>Status</th><th>Duration</th><th>Provider</th><th>Checked</th><th>Egress</th><th>Alerts</th><th>Details</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>';
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Log requests to the bounded panel.log instead of stderr, so a
        # broken panel is debuggable without spamming the console.
        code = args[0] if args else "?"
        _log_panel(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                   f"{self.client_address[0]} {self.command} {self.path} -> {code}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_html(HTML)
        elif parsed.path == "/api/config":
            self._send_json({"config": load_config(), "secrets": load_secrets()})
        elif parsed.path == "/api/status":
            # Cached 10s: every panel refresh hits this and it runs 3+ gcloud
            # subprocesses; the VM status changes slowly, so fresh-ish is fine.
            self._send_json(_cached("status", 10, lambda: {
                "gcloud": gcloud_available(),
                "auth": auth_status(),
                "vm": vm_status(),
            }))
        elif parsed.path == "/api/logs":
            self._send_json(_cached("logs", 20, lambda: {
                "ok": True, "logs": fetch_vm_run_history()}))
        elif parsed.path == "/api/network":
            self._send_json(_cached("network", 20, lambda: {
                "ok": True, "network": fetch_vm_network_usage(),
                "monthly_limit_bytes": 1 * 1024 * 1024 * 1024}))
        elif parsed.path == "/api/timer":
            self._send_json(_cached("timer", 20, lambda: {
                "ok": True, "timer": fetch_vm_timer_status()}))
        elif parsed.path == "/api/auth":
            ok, out, err = run_gcloud(["auth", "login", "--no-launch-browser",
                                       "--brief"], timeout=300)
            self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})
        elif parsed.path == "/api/create_vm":
            self._handle_create_vm()
        elif parsed.path == "/api/budget":
            self._handle_budget()
        elif parsed.path == "/api/usage":
            self._handle_usage()
        elif parsed.path == "/api/test_alert":
            self._handle_test_alert()
        elif parsed.path == "/api/run_now":
            self._handle_run_now()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_create_vm(self):
        project = get_project()
        if not project:
            self._send_json({"ok": False, "error": "Not authenticated. Click 'Authenticate to Google' first."})
            return

        # Check if the VM already exists. If it does, don't create a duplicate.
        existing = vm_status()
        if existing:
            # VM already there - just (re)deploy the config so it's up to date.
            ok, out, err = self._deploy_to_vm(project)
            msg = (f"Server already exists (status: {existing}). "
                   f"Config re-uploaded - no duplicate was created.")
            self._send_json({"ok": ok, "error": (err or "") if not ok else "",
                             "output": msg + "\n" + out + err})
            return

        # Try each zone in order until one has capacity for the free e2-micro.
        ok, out, err = False, "", ""
        created_zone = None
        for zone in VM_ZONES:
            ok, out, err = run_gcloud([
                "compute", "instances", "create", VM_NAME,
                "--zone", zone, "--machine-type", VM_MACHINE,
                "--image-family", VM_IMAGE, "--image-project", "debian-cloud",
                "--boot-disk-size", "10GB", "--tags", "http-server",
                "--quiet",
            ], timeout=300)
            if ok:
                created_zone = zone
                break
            # If it's not a capacity error, stop trying other zones.
            if "ZONE_RESOURCE_POOL_EXHAUSTED" not in err and "resource_availability" not in err:
                break

        # Then copy the project files up and run setup
        if ok:
            ok2, out2, err2 = self._deploy_to_vm(project)
            ok = ok2; out += out2; err += err2
            if created_zone:
                out = f"(created in zone {created_zone})\n" + out
        self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})

    def _deploy_to_vm(self, project):
        """Copy monitor files to the VM and run setup_cloud.sh."""
        zone = find_vm_zone()
        if not zone:
            return False, "", "VM not found."
        # Resolve the VM's home directory (pscp can't use ~/ as a target).
        home = get_vm_home(zone)
        out, err = "", ""
        # scp the needed files to the absolute home path
        files = ["monitor.py", "requirements.txt", "setup_cloud.sh",
                 "config_local.json", "secrets_local.json"]
        for f in files:
            src = os.path.join(BASE_DIR, f)
            if os.path.exists(src):
                ok, o, e = run_gcloud([
                    "compute", "scp", "--zone", zone, src,
                    f"{VM_NAME}:{home}/", "--quiet"], timeout=120)
                out += o; err += e
                if not ok:
                    return False, out, err
        # Run setup on the VM
        ok, o, e = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", "cd ~ && bash setup_cloud.sh", "--quiet"], timeout=300)
        out += o; err += e
        return ok, out, err

    def _handle_budget(self):
        project = get_project()
        if not project:
            self._send_json({"ok": False, "error": "Not authenticated."})
            return
        # Find the actual billing account ID
        ok, out, _ = run_gcloud(["billing", "accounts", "list",
                                 "--format=value(ACCOUNT_ID)", "--quiet"], timeout=60)
        acct = out.strip().splitlines()[0].strip() if ok and out.strip() else None
        if not acct:
            self._send_json({"ok": False, "error": "No billing account found. Link a billing account in Google Cloud first."})
            return
        # Check if a budget with our name already exists. If so, don't create a duplicate.
        ok2, out2, _ = run_gcloud([
            "billing", "budgets", "list", "--billing-account", acct,
            "--format=value(displayName)", "--quiet"], timeout=60)
        if ok2 and "portfolioismoving-alert" in out2:
            self._send_json({"ok": True, "error": "",
                             "output": "Budget alert already set ($1/month). No duplicate was created."})
            return
        # Create a $1 budget alert (percent-based thresholds only).
        ok, out, err = run_gcloud([
            "billing", "budgets", "create",
            "--billing-account", acct,
            "--display-name", "portfolioismoving-alert",
            "--budget-amount", "1",
            "--threshold-rule", "percent=50",
            "--threshold-rule", "percent=90",
            "--quiet"], timeout=120)
        self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})

    def _handle_usage(self):
        project = get_project()
        if not project:
            self._send_json({"ok": False, "error": "Not authenticated."})
            return
        # Real check: is billing enabled on the project?
        ok, out, err = run_gcloud([
            "billing", "projects", "describe", project,
            "--format=value(billingEnabled)", "--quiet"], timeout=60)
        billing_enabled = (ok and out.strip().lower() == "true")

        # Real check: is the $1 budget alert active?
        budget_status = "not set"
        ok2, out2, _ = run_gcloud([
            "billing", "accounts", "list", "--format=value(ACCOUNT_ID)",
            "--quiet"], timeout=60)
        acct = out2.strip().splitlines()[0].strip() if ok2 and out2.strip() else None
        if acct:
            ok3, out3, _ = run_gcloud([
                "billing", "budgets", "list", "--billing-account", acct,
                "--format=value(displayName)", "--quiet"], timeout=60)
            if ok3 and "portfolioismoving-alert" in out3:
                budget_status = "active ($1/month)"

        # Real dollar cost requires BigQuery Billing Export (heavy setup) or
        # the new Cost Management API. We report honestly instead of faking it.
        note = (f"Billing is {'ENABLED' if billing_enabled else 'NOT enabled'}. "
                f"Budget alert: {budget_status}. "
                f"Your VM is the free e2-micro tier = $0/month.")
        # The real source of truth is Google's own billing console. Build the
        # URL to that page so the panel can open it.
        billing_url = None
        if acct:
            billing_url = f"https://console.cloud.google.com/billing/{acct}"
        self._send_json({"ok": True, "error": "",
                         "output": note,
                         "billing_url": billing_url})

    def _handle_test_alert(self):
        secrets = load_secrets()
        token = secrets.get("telegram_bot_token", "")
        chat_id = secrets.get("telegram_chat_id", "")
        if not token or not chat_id:
            self._send_json({"ok": False, "error": "Telegram token/chat id not set. Fill them in step 3 and upload."})
            return
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        # Write a tiny test script locally, scp it up, and run it. This avoids
        # the fragile nested-quoting of inline python3 -c through SSH.
        script = (
            "import monitor\n"
            "ok = monitor.send_telegram(\n"
            "    '" + token + "',\n"
            "    '" + chat_id + "',\n"
            "    'PortfolioIsMoving test alert - server is running!',\n"
            ")\n"
            "print('SENT_OK' if ok else 'SENT_FAIL')\n"
        )
        script_path = os.path.join(BASE_DIR, "_test_alert.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        home = get_vm_home(zone)
        # Upload the script
        ok, out, err = run_gcloud([
            "compute", "scp", "--zone", zone, script_path,
            f"{VM_NAME}:{home}/", "--quiet"], timeout=120)
        # Run it. Prefer the venv python installed by setup_cloud.sh (new
        # VMs); fall back to python3 for VMs set up by older scripts.
        if ok:
            ok2, out2, err2 = run_gcloud([
                "compute", "ssh", "--zone", zone, VM_NAME,
                "--command", f"cd {home} && (./.venv/bin/python _test_alert.py 2>&1 || python3 _test_alert.py 2>&1)",
                "--quiet"], timeout=120)
            ok = ok2; out += out2; err += err2
            # Clean up the script on the VM
            run_gcloud([
                "compute", "ssh", "--zone", zone, VM_NAME,
                "--command", f"rm -f {home}/_test_alert.py",
                "--quiet"], timeout=60)
        # Clean up locally
        try:
            os.remove(script_path)
        except OSError:
            pass
        self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})

    def _handle_run_now(self):
        """
        Run monitor.py once on the VM right now and return its output. This is
        a real end-to-end test: it proves the monitor itself works, so if the
        scheduled cron runs don't appear afterwards, the problem is the
        scheduler, not the monitor.
        """
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        home = get_vm_home(zone)
        # Prefer the venv python installed by setup_cloud.sh; fall back to
        # python3 for VMs set up by older scripts.
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && (./.venv/bin/python monitor.py 2>&1 || python3 monitor.py 2>&1)",
            "--quiet"], timeout=120)
        # The monitor writes a run_history record; refresh happens on next load.
        self._send_json({"ok": ok, "error": (err or "") if not ok else "",
                         "output": out + err})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload()
        else:
            self._send_json({"error": "not found"}, 404)

    # ---- /api/upload input validation ----
    MAX_BODY_BYTES = 512 * 1024
    VALID_PROVIDERS = ("finnhub", "twelvedata", "yahoo")
    MAX_TICKERS = 50
    TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")

    @staticmethod
    def _clean_str(value, max_len=200, default=""):
        if not isinstance(value, str):
            return default
        return value.strip()[:max_len]

    @staticmethod
    def _clean_pct(value, default, low=0.1, high=100.0):
        """Parse a percentage; reject garbage and NaN/Inf, clamp to a sane range."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(v):
            return default
        return min(max(v, low), high)

    def _handle_upload(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length > self.MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "Request body too large."}, 413)
            return
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
        except Exception:
            self._send_json({"ok": False, "error": "Invalid JSON body."}, 400)
            return
        if not isinstance(data, dict):
            self._send_json({"ok": False, "error": "Body must be a JSON object."}, 400)
            return

        cfg = load_config()
        secrets = load_secrets()

        # Tickers: must be a list of short stock symbols; anything that isn't
        # a valid symbol is dropped rather than stored (stops junk/code from
        # ever reaching the dashboard or the VM).
        raw_tickers = data.get("tickers", [])
        if not isinstance(raw_tickers, list):
            raw_tickers = []
        tickers = []
        for t in raw_tickers:
            if not isinstance(t, str):
                continue
            t = t.strip().upper()
            if t and self.TICKER_RE.match(t) and t not in tickers:
                tickers.append(t)
            if len(tickers) >= self.MAX_TICKERS:
                break
        cfg["tickers"] = tickers

        cfg["threshold_pct"] = self._clean_pct(data.get("threshold_pct"), 5.0)
        cfg["retrigger_step_pct"] = self._clean_pct(data.get("retrigger_step_pct"), 3.0)

        raw_enabled = data.get("enabled", True)
        cfg["enabled"] = raw_enabled if isinstance(raw_enabled, bool) else True

        provider = self._clean_str(data.get("provider"), max_len=20, default="finnhub")
        if provider not in self.VALID_PROVIDERS:
            provider = "finnhub"
        cfg["provider"] = provider

        if data.get("finnhub_key") is not None:
            secrets["finnhub_key"] = self._clean_str(data["finnhub_key"], max_len=200)
        if data.get("twelvedata_key") is not None:
            secrets["twelvedata_key"] = self._clean_str(data["twelvedata_key"], max_len=200)
        secrets["telegram_bot_token"] = self._clean_str(data.get("telegram_bot_token"), max_len=200)
        secrets["telegram_chat_id"] = self._clean_str(data.get("telegram_chat_id"), max_len=100)
        save_config(cfg)
        save_secrets(secrets)
        # Now upload to VM
        project = get_project()
        if not project:
            self._send_json({"ok": False, "error": "Not authenticated to Google."})
            return
        ok, out, err = self._deploy_to_vm(project)
        self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})


def main():
    if not gcloud_available():
        print("=" * 50)
        print(" gcloud is not installed.")
        print(" The app needs the Google Cloud CLI to manage your server.")
        print("")
        print(" Install it here (free):")
        print("   https://cloud.google.com/sdk/docs/install")
        print("")
        print(" After installing, close & reopen this terminal, then run:")
        print("   python cloud_manager.py")
        print("=" * 50)
        return
    port = PORT
    try:
        server = ThreadingHTTPServer(("", port), Handler)
    except OSError:
        # Fail loudly instead of silently moving to another port: the launcher
        # (start_cloud.bat) opens http://localhost:8000, so a silent fallback
        # would open the browser to the wrong page.
        print(f"Could not start the panel on port {port} - it is probably in use.")
        print("Close the other panel (or anything using port 8000) and try again.")
        print("To use a different port, change PORT in cloud_manager.py.")
        return
    _log_panel(f"panel started on http://localhost:{port}")
    print(f"PortfolioIsMoving Cloud panel: http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
