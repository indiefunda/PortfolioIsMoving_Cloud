# ☁️ PortfolioIsMoving — Google Cloud setup (free, 24/7)

This runs your stock monitor on a **free Google Cloud server** that stays on
24/7. Unlike GitHub Actions (which drops scheduled runs) and unlike a phone
(which has to stay awake), a Google Cloud server runs reliably and never
sleeps. Your iPhone just receives the Telegram alerts.

**Cost: $0.00/month forever** (Google's "Always Free" e2-micro VM).

> You'll need a **credit card** to sign up for Google Cloud (they use it to
> verify you're a real person). They do **not** charge it as long as you stay
> within the free limits. This setup stays well within them.

---

## 🟢 PART 1 — Create your free Google Cloud VM (one time)

Do this on your iPhone (or any computer). Takes ~15 minutes.

1. Open your browser and go to **`https://console.cloud.google.com`**
2. Sign in with your Google account (the one you use for Gmail etc.).
3. It will ask you to agree to terms. Click **Agree and continue**.
4. It will ask for your **country** and a **credit card**. Enter them.
   > This is just to verify you. **You will not be charged** as long as you
   > stay in the free tier.
5. Click **Create** / **Start free**.

### Create the VM (server)
6. In the top search bar, type **`Compute Engine`** and tap it.
7. Tap **Create Instance** (or "Create VM instance").
8. Give it a name, e.g. **`stock-monitor`**.
9. Under **Region**, pick one near you (e.g. `us-east1`, `us-central1`).
   > ⚠️ The **free e2-micro** machine is only available in certain regions.
   > Use `us-east1` or `us-central1` to be safe.
10. Under **Machine type**, click **"e2-micro"** (it should say "Free" or
    "$0.00"). This is the free one. If you don't see it, click
    **"Advanced"** or scroll the list.
11. Under **Boot disk**, leave it as **Debian** (or Ubuntu). Default is fine.
12. Click **Create**.

### Connect to the VM
13. Wait ~30 seconds for it to start. You'll see a green checkmark and a
    **"SSH"** button next to your `stock-monitor` instance.
14. Tap the **SSH** button. A black terminal window opens (this is your
    server's command line). **Keep this open.**

---

## 🟢 PART 2 — Put the app files on the server

You need to get the app files onto the server. The easiest way is to download
them from GitHub.

In the SSH terminal, copy-paste this one command (tap the terminal, then paste),
then press **Enter**:

```bash
cd ~ && wget -q https://github.com/indiefunda/PortfolioIsMoving_Cloud/archive/refs/heads/master.zip && unzip -q master.zip && mv PortfolioIsMoving_Cloud-master PortfolioIsMoving && cd PortfolioIsMoving && ls
```

You should see the files listed:
```
config_local.json  monitor.py  requirements.txt  setup_cloud.sh
```

> If `unzip` isn't installed, run `sudo apt-get install -y unzip` first, then
> run the command again.

---

## 🟢 PART 3 — Run the one-time setup (this does everything)

In the same SSH terminal, run:

```bash
bash setup_cloud.sh
```

This takes a couple of minutes. It:
1. Installs Python + the needed packages
2. Sets up a **timer** that runs the monitor **every 10 minutes** during US
   market hours (Mon–Fri)
3. Runs the monitor once to test it

At the end you should see something like:
```
[2026-08-11 09:35 EDT] Checking 6 ticker(s) via finnhub...
  - HUIZ: $1.38 (prev $1.42) = -2.82%
```

If it says `Skipping: outside market hours`, that's normal — it means the
market is closed right now. The timer will run it when the market opens.

---

## 🟢 PART 4 — That's it! ✅

Your monitor is now running 24/7 on a free Google server. It:
- Checks your 6 stocks every 10 minutes, Mon–Fri, during US market hours
- Sends you a **Telegram alert** when a stock moves 5% or more
- Never sleeps, never needs a phone on, never gets dropped by GitHub

You can close the SSH window. The server keeps running on its own.

---

## 🔄 Changing your stocks or API keys later

You don't need to redo the setup. Just edit the files on the server and the
next run picks them up.

1. Open the SSH terminal again (Console → Compute Engine → SSH).
2. Edit the stocks:
   ```bash
   cd ~/PortfolioIsMoving
   nano config_local.json
   ```
   Change the `tickers` list (e.g. `["AAPL", "MSFT"]`), then press
   **Ctrl+X**, then **Y**, then **Enter** to save.
3. Or edit API keys / Telegram:
   ```bash
   nano secrets_local.json
   ```
   Same save steps (Ctrl+X, Y, Enter).

No restart needed — the timer reads the files fresh every 10 minutes.

> 💡 If `nano` isn't installed, run `sudo apt-get install -y nano` first.

---

## 🔍 Checking it's working

```bash
cd ~/PortfolioIsMoving
cat monitor.log
```

You should see a new line every 10 minutes during market hours.

To see the schedule:
```bash
systemctl list-timers | grep portfolio
```

---

## 🛑 Stopping it (if you ever need to)

```bash
sudo systemctl disable --now portfolioismoving.timer
```

---

## 🔴 If something goes wrong

| Problem | What to do |
|---------|------------|
| "command not found: wget / unzip" | Run `sudo apt-get install -y unzip` then retry Part 2. |
| The free e2-micro isn't showing | Change the **Region** to `us-east1` or `us-central1`. |
| No alerts but market is open | SSH in, run `cat ~/PortfolioIsMoving/monitor.log` and check for errors. |
| You get charged | You shouldn't — but set a billing alert in Google Cloud (Billing → Budgets) to warn you if you ever approach a limit. |
| Timer stopped after reboot | `sudo systemctl enable portfolioismoving.timer` (should already be enabled, but re-run to be safe). |

---

## 🧠 The short version (if you ever need to redo it)

```
# Create the free e2-micro VM in us-east1/us-central1, click SSH, then:
cd ~ && wget -q https://github.com/indiefunda/PortfolioIsMoving_Cloud/archive/refs/heads/master.zip && unzip -q master.zip && mv PortfolioIsMoving_Cloud-master PortfolioIsMoving && cd PortfolioIsMoving
bash setup_cloud.sh
```
Done. ✅
