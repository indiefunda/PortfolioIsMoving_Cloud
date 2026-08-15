#!/usr/bin/env bash
# ============================================================
# PortfolioIsMoving (Cloud) - one-time VM setup
# Run this ONCE on your Google Cloud e2-micro VM.
#
#   bash setup_cloud.sh
#
# What it does:
#   1. Installs Python 3, pip, python3-venv, and cron
#   2. Installs the Python packages (requests, tzdata) - in a venv
#   3. Installs a cron job that runs monitor.py every 10
#      minutes during US market hours (Mon-Fri)
#   4. Runs monitor.py once to confirm it works
#
# We use cron (not a systemd timer) because its schedule syntax is simple
# and unambiguous, making it reliable to install and verify.
# ============================================================

set -e

echo "=============================================="
echo " PortfolioIsMoving - Cloud setup"
echo "=============================================="

# --- 1. Install Python + cron
echo "[1/4] Installing Python and system tools..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv cron || true

# Make sure the cron daemon is actually running AND enabled at boot. On some
# minimal Debian images, installing cron doesn't start or enable it, which is
# a common silent cause of "the monitor never runs unattended".
sudo systemctl enable cron 2>/dev/null || true
sudo systemctl start cron 2>/dev/null || true
echo "  cron daemon: $(systemctl is-active cron 2>/dev/null || echo 'not running')"
echo "  cron at boot: $(systemctl is-enabled cron 2>/dev/null || echo 'not enabled')"

# --- 2. Install Python dependencies
echo "[2/4] Installing Python packages (requests, tzdata)..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer a dedicated virtualenv: no sudo, no --break-system-packages, and the
# cron job below uses the venv's interpreter so imports always resolve.
# If python3-venv is missing we fall back to a system-wide install (the old
# behaviour), which is why the import check runs with whichever interpreter
# ends up in $PYTHON.
if python3 -m venv "$PROJECT_DIR/.venv" 2>/dev/null; then
  echo "  Using a dedicated virtualenv at $PROJECT_DIR/.venv"
  "$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip
  "$PROJECT_DIR/.venv/bin/python" -m pip install -r requirements.txt
  PYTHON="$PROJECT_DIR/.venv/bin/python"
else
  echo "  python3-venv not available - falling back to a system-wide install."
  echo "  (If cron later reports 'No module named requests', install python3-venv and re-run this script.)"
  sudo python3 -m pip install --break-system-packages --upgrade pip 2>/dev/null \
    || sudo python3 -m pip install --upgrade pip
  sudo python3 -m pip install --break-system-packages -r requirements.txt 2>/dev/null \
    || sudo python3 -m pip install -r requirements.txt
  PYTHON="$(command -v python3)"
fi

# Dependency check as cron would run it. monitor.py needs zoneinfo to resolve
# the America/New_York database (tzdata on the system) and the requests
# package. NOTE: do NOT check for pytz here - the code migrated to zoneinfo
# and pytz is no longer a dependency (a stale pytz check aborted the whole
# script on fresh VMs via set -e, so cron never got installed).
echo "  dependency check (as cron would):"
"$PYTHON" -c "from zoneinfo import ZoneInfo; ZoneInfo('America/New_York'); import requests; print('  OK - timezone data and requests importable')"

# --- 3. Set up the 10-minute schedule using cron
echo "[3/4] Setting up the 10-minute schedule (cron)..."
MONITOR="$PROJECT_DIR/monitor.py"
LOG="$PROJECT_DIR/monitor.log"

# We use cron instead of a systemd timer because cron's schedule syntax is
# simple and unambiguous, and it has proven far more reliable to install and
# verify. We install ONE cron job whose window covers both DST seasons;
# monitor.py does the precise market-hours check.

# Remove any old systemd timer/service from earlier versions so they don't
# conflict or confuse things.
sudo systemctl disable --now portfolioismoving.timer 2>/dev/null || true
sudo rm -f /etc/systemd/system/portfolioismoving.timer
sudo rm -f /etc/systemd/system/portfolioismoving.service
sudo systemctl daemon-reload 2>/dev/null || true

# Build the cron line. We install ONE job whose window is the UNION of both
# US-market DST seasons, so it fires in summer AND winter without overlap:
#   - Summer (EDT, UTC-4): market ~13:30-20:00 UTC
#   - Winter (EST, UTC-5): market ~14:30-21:00 UTC
#   - Union window: 13:00-21:59 UTC  ->  single job "*/10 13-21"
# monitor.py's own DST-aware is_market_hours() check (9:25-16:05 ET) does the
# precise filtering, so the extra fires just before/after market hours are
# skipped harmlessly. Using ONE job (instead of the old two-job approach)
# guarantees monitor.py NEVER runs twice at the same minute, which previously
# caused duplicate API calls, wasted free-tier egress, and race conditions
# where concurrent runs overwrote each other's run-history records (the
# "it runs very rarely" bug).
CRON_LINE="*/10 13-21 * * 1-5 cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"

# Resolve the absolute path to crontab. When this script runs through
# "gcloud compute ssh --command", the non-interactive PATH may not include
# /usr/sbin or /usr/bin, so "crontab" alone can silently fail.
CRONTAB="$(command -v crontab || echo /usr/bin/crontab)"
MKTEMP="$(command -v mktemp || echo /usr/bin/mktemp)"
GREP="$(command -v grep || echo /bin/grep)"

# Install the cron job using a temp file. We deliberately do NOT pipe to
# "crontab -" (stdin) because that has proven to fail silently in this SSH
# environment. Writing a temp file and calling "crontab <file>" is the
# reliable, standard way and lets us check the result explicitly.
#
# NOTE: we filter out old copies by matching "monitor.py" (the marker unique
# to our job). The old code filtered on "portfolioismoving", but the cron line
# itself never contains that string, so old jobs were never removed and every
# setup run added a duplicate. This was why multiple identical cron jobs
# accumulated on the VM.
TMPCRON="$("$MKTEMP")"
"$CRONTAB" -l 2>/dev/null | "$GREP" -v 'monitor.py' > "$TMPCRON" || true
echo "$CRON_LINE" >> "$TMPCRON"
if "$CRONTAB" "$TMPCRON"; then
  echo "  ✅ Cron job installed (user crontab)."
else
  echo "  ⚠️  User crontab failed - trying /etc/cron.d/ instead..."
  # Fallback: install a system cron file (needs root). Format is the same
  # as a crontab but with the username field inserted after the schedule.
  USERNAME="$(id -un 2>/dev/null || echo root)"
  CRON_SYS="*/10 13-21 * * 1-5 $USERNAME cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"
  echo "$CRON_SYS" > /tmp/portfolioismoving-cron
  if sudo mv /tmp/portfolioismoving-cron /etc/cron.d/portfolioismoving; then
    echo "  ✅ Cron job installed (/etc/cron.d/portfolioismoving)."
    rm -f "$TMPCRON"
  else
    echo "  ❌ FAILED to install the cron job (both methods returned an error)."
    echo "     crontab path: $CRONTAB"
    echo "     Try manually: $CRONTAB $TMPCRON"
    exit 1
  fi
fi
rm -f "$TMPCRON"

echo "  Cron installed: $CRON_LINE"
echo "  Installed job(s):"
"$CRONTAB" -l 2>/dev/null | "$GREP" 'monitor.py' || echo "  (not in user crontab - check /etc/cron.d/)"
echo "  cron daemon active: $(systemctl is-active cron 2>/dev/null || echo 'no')"
if [ "$(systemctl is-active cron 2>/dev/null)" != "active" ]; then
  echo "  ⚠️  WARNING: the cron daemon is not running. Try: sudo systemctl start cron"
fi

# --- 4. Run once to confirm it works
echo "[4/4] Running monitor.py once to test..."
python3 "$MONITOR"

echo ""
echo "=============================================="
echo " DONE! The monitor will now run every 10 min."
echo ""
echo " To check it's working:"
echo "   cat $LOG"
echo ""
echo " To see the schedule:"
echo "   crontab -l | grep monitor.py"
echo ""
echo " To stop it (if you ever need to):"
echo "   crontab -l | grep -v monitor.py | crontab -"
echo "=============================================="
