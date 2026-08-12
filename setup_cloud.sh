#!/usr/bin/env bash
# ============================================================
# PortfolioIsMoving (Cloud) - one-time VM setup
# Run this ONCE on your Google Cloud e2-micro VM.
#
#   bash setup_cloud.sh
#
# What it does:
#   1. Installs Python 3, pip, and cron
#   2. Installs the Python packages (requests, pytz)
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
sudo apt-get install -y python3 python3-pip cron || true

# Make sure the cron daemon is actually running AND enabled at boot. On some
# minimal Debian images, installing cron doesn't start or enable it, which is
# a common silent cause of "the monitor never runs unattended".
sudo systemctl enable cron 2>/dev/null || true
sudo systemctl start cron 2>/dev/null || true
echo "  cron daemon: $(systemctl is-active cron 2>/dev/null || echo 'not running')"
echo "  cron at boot: $(systemctl is-enabled cron 2>/dev/null || echo 'not enabled')"

# --- 2. Install Python dependencies system-wide
echo "[2/4] Installing Python packages (requests, pytz)..."
# IMPORTANT: install with sudo so the packages go into the system site-packages.
# If they land in the USER site-packages (~/.local/...), cron cannot import
# them (cron runs with a minimal environment), and monitor.py crashes with
# "No module named 'pytz'" every time it runs on schedule. This was the root
# cause of "the monitor never fires unattended".
sudo python3 -m pip install --break-system-packages --upgrade pip 2>/dev/null \
  || sudo python3 -m pip install --upgrade pip
sudo python3 -m pip install --break-system-packages -r requirements.txt 2>/dev/null \
  || sudo python3 -m pip install -r requirements.txt
echo "  pytz import check (as cron would):"
python3 -c "import pytz, requests; print('  OK - pytz and requests importable')"

# --- 3. Set up the 10-minute schedule using cron
echo "[3/4] Setting up the 10-minute schedule (cron)..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONITOR="$PROJECT_DIR/monitor.py"
LOG="$PROJECT_DIR/monitor.log"
PYTHON="$(command -v python3)"

# We use cron instead of a systemd timer because cron's schedule syntax is
# simple and unambiguous, and it has proven far more reliable to install and
# verify. We install TWO cron jobs (one per DST season) so cron only fires
# during US market hours; monitor.py does the precise market-hours check.

# Remove any old systemd timer/service from earlier versions so they don't
# conflict or confuse things.
sudo systemctl disable --now portfolioismoving.timer 2>/dev/null || true
sudo rm -f /etc/systemd/system/portfolioismoving.timer
sudo rm -f /etc/systemd/system/portfolioismoving.service
sudo systemctl daemon-reload 2>/dev/null || true

# Build the cron lines. We install TWO jobs so cron only fires during US
# market hours in BOTH daylight-saving seasons (cron can't know about DST):
#   - Summer (EDT, UTC-4): market ~13:30-20:00 UTC  ->  job 13-20 UTC
#   - Winter (EST, UTC-5): market ~14:30-21:00 UTC  ->  job 14-21 UTC
# Both fire year-round; monitor.py's own DST-aware is_market_hours() check
# (9:25-16:05 ET) does the precise filtering. This avoids waking up all day.
CRON_LINE_SUMMER="*/10 13-20 * * 1-5 cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"
CRON_LINE_WINTER="*/10 14-21 * * 1-5 cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"

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
echo "$CRON_LINE_SUMMER" >> "$TMPCRON"
echo "$CRON_LINE_WINTER" >> "$TMPCRON"
if "$CRONTAB" "$TMPCRON"; then
  echo "  ✅ Cron jobs installed (user crontab)."
else
  echo "  ⚠️  User crontab failed - trying /etc/cron.d/ instead..."
  # Fallback: install a system cron file (needs root). Format is the same
  # as a crontab but with the username field inserted after the schedule.
  USERNAME="$(id -un 2>/dev/null || echo root)"
  CRON_SUMMER_SYS="*/10 13-20 * * 1-5 $USERNAME cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"
  CRON_WINTER_SYS="*/10 14-21 * * 1-5 $USERNAME cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"
  echo "$CRON_SUMMER_SYS" > /tmp/portfolioismoving-cron
  echo "$CRON_WINTER_SYS" >> /tmp/portfolioismoving-cron
  if sudo mv /tmp/portfolioismoving-cron /etc/cron.d/portfolioismoving; then
    echo "  ✅ Cron jobs installed (/etc/cron.d/portfolioismoving)."
    rm -f "$TMPCRON"
  else
    echo "  ❌ FAILED to install the cron jobs (both methods returned an error)."
    echo "     crontab path: $CRONTAB"
    echo "     Try manually: $CRONTAB $TMPCRON"
    exit 1
  fi
fi
rm -f "$TMPCRON"

echo "  Cron installed (summer): $CRON_LINE_SUMMER"
echo "  Cron installed (winter): $CRON_LINE_WINTER"
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
echo "   crontab -l | grep portfolioismoving"
echo ""
echo " To stop it (if you ever need to):"
echo "   crontab -l | grep -v portfolioismoving | crontab -"
echo "=============================================="
