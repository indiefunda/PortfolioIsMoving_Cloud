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
#   3. Installs a systemd timer that runs monitor.py every 10
#      minutes during US market hours (Mon-Fri)
#   4. Runs monitor.py once to confirm it works
#
# The systemd timer is MORE reliable than cron - it survives
# reboots and fires on time every time.
# ============================================================

set -e

echo "=============================================="
echo " PortfolioIsMoving - Cloud setup"
echo "=============================================="

# --- 1. Install Python + cron
echo "[1/4] Installing Python and system tools..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip cron || true

# --- 2. Install Python dependencies
echo "[2/4] Installing Python packages (requests, pytz)..."
pip3 install --break-system-packages --upgrade pip 2>/dev/null || pip3 install --upgrade pip
pip3 install --break-system-packages -r requirements.txt 2>/dev/null || pip3 install -r requirements.txt

# --- 3. Set up the 10-minute timer
echo "[3/4] Setting up the 10-minute schedule..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONITOR="$PROJECT_DIR/monitor.py"
LOG="$PROJECT_DIR/monitor.log"
PYTHON="$(command -v python3)"

# Create a systemd service that runs the monitor
SERVICE_FILE="/etc/systemd/system/portfolioismoving.service"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=PortfolioIsMoving stock monitor
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON $MONITOR
StandardOutput=append:$LOG
StandardError=append:$LOG
EOF

# Create a systemd timer that fires every 10 min, Mon-Fri, during market hours (13-21 UTC)
#
# IMPORTANT: the calendar expression below is deliberately written as an hour
# range with a minute step ("13..21:00/10:00"). The alternative
# "Mon-Fri *-*-* 13:00/10:00" is a well-known systemd gotcha: combining a
# day-of-week filter with a "/STEP" time makes the timer fire only ONCE (at
# 13:00) instead of every 10 minutes. The range form below repeats reliably.
TIMER_FILE="/etc/systemd/system/portfolioismoving.timer"
sudo tee "$TIMER_FILE" > /dev/null <<EOF
[Unit]
Description=Run PortfolioIsMoving every 10 min during market hours

[Timer]
OnCalendar=Mon-Fri *-*-* 13..21:00/10:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Enable and start the timer
sudo systemctl daemon-reload
sudo systemctl enable portfolioismoving.timer
sudo systemctl start portfolioismoving.timer

echo "  Timer installed: every 10 min, Mon-Fri, 13:00-21:00 UTC"

# Verify the timer is actually armed and show its next fire time. If this
# prints nothing for "Next elapse", the schedule did not take effect.
echo "  Timer status:"
systemctl list-timers portfolioismoving.timer --no-pager || true
echo "  Active state: $(systemctl is-active portfolioismoving.timer 2>/dev/null || echo 'unknown')"

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
echo "   systemctl list-timers | grep portfolio"
echo ""
echo " To stop it (if you ever need to):"
echo "   sudo systemctl disable --now portfolioismoving.timer"
echo "=============================================="
