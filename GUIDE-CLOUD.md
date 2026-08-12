# ☁️ PortfolioIsMoving — Google Cloud setup (free, 24/7)

This runs your stock monitor on a **free Google Cloud server** that stays on
24/7. Unlike GitHub Actions (which drops scheduled runs) and unlike a phone
(which has to stay awake), a Google Cloud server runs reliably and never
sleeps. Your iPhone just receives the Telegram alerts.

**Cost: $0.00/month forever** (Google's "Always Free" e2-micro VM).

> **You control everything from a simple panel on your PC — no SSH, no typing
> commands.** The `cloud_manager.py` app talks to Google for you.

---

## 🟢 PART 0 — Sign up for Google Cloud (one time)

1. Open your browser and go to **`https://console.cloud.google.com`**
2. Sign in with your Google account (the one you use for Gmail etc.).
3. Agree to the terms. Click **Agree and continue**.
4. It will ask for your **country** and a **card**. Enter them.
   > **Which card?** A **Revolut debit card** is a great choice. Keep only a
   > small balance on it (e.g. $10). Even if something went wrong, the most
   > anyone could take is what's on the **Revolut card** — not your main bank
   > account. Google does a ~$1 verification hold that releases after a few
   > days. They **do not charge** you for free-tier usage.
5. Click **Create** / **Start free**.

### Protect your account (important)
- Turn on **2-factor authentication (2FA)** for your Google account →
  Settings → Security. So a stolen password alone isn't enough.
- Use a **strong, unique password** you don't use anywhere else.
- The app's "Set $1 budget alert" button will email you the moment anything
  would cost money — use it (see Part 3).

---

## 🟢 PART 1 — Install the Google Cloud CLI (one time)

The panel needs Google's free command-line tool to talk to your server. You
install it **once**; the panel uses it automatically.

1. Go to **`https://cloud.google.com/sdk/docs/install`**
2. Download the **Windows installer** (a `.exe`).
3. Run it and accept the defaults.
4. When it finishes, **close and reopen** your terminal window.

---

## 🟢 PART 2 — Start the control panel

1. Open a terminal in this folder (`PortfolioIsMoving_Cloud`).
2. Run:
   ```
   python cloud_manager.py
   ```
3. Open **`http://localhost:8000`** in your browser.

You'll see a clean panel with numbered steps. **No SSH, no typing commands.**

---

## 🟢 PART 3 — Use the panel to set everything up

In the panel, do these in order:

1. **Connect to Google** → click **"Authenticate to Google"**.
   - Google's own login page opens in your browser.
   - You sign in there (with your 2FA). **Your password is never given to
     the panel** — it only gets a login token.
   - The panel shows "Connected as: your@email".

2. **Create your free server** → click **"Create free server (e2-micro)"**.
   - This creates the free Google VM and uploads the app to it automatically.
   - Wait ~1 minute. It shows "Server created!" when done.

3. **Your portfolio** → edit your **stocks**, **threshold**, **API keys**,
   and **Telegram** in the form, then click **"Upload config to server"**.
   - This sends your settings to the server. No restart needed.

4. **Cost safety** → click **"Set $1 monthly budget alert"**.
   - This emails you the moment anything would cost money. **Use this** — it
     protects your Revolut card.

5. **Usage & health** → click **"Check usage so far"** and
   **"Send test Telegram alert"** to confirm everything works.

6. **Run history** → the panel shows a table of every run. You can see when
   the monitor last ran, how long it took, what prices it saw, and whether
   alerts were sent — even when nothing crossed the threshold.

---

## 🟢 PART 4 — That's it! ✅

Your monitor is now running 24/7 on a free Google server. It:
- Checks your 6 stocks every 10 minutes, Mon–Fri, during US market hours
- Sends you a **Telegram alert** when a stock moves 5% or more
- Never sleeps, never needs a phone on, never gets dropped by GitHub

You can close the SSH window. The server keeps running on its own.

---

## 🔄 Changing your stocks or API keys later

The easiest way is the **panel**:

1. Run `python cloud_manager.py` and open `http://localhost:8000`.
2. Edit your **stocks**, **threshold**, **API keys**, or **Telegram** in the
   "Your portfolio" section.
3. Click **"Upload config to server"**.

No restart needed — the server reads the files fresh every 10 minutes.

> *(Manual option, if you ever prefer it:)* edit `config_local.json` /
> `secrets_local.json` on the server with `nano`, then save with Ctrl+X → Y →
> Enter.

---

## 🔍 Checking it's working

In the panel, click **"Check usage so far"** and **"Send test Telegram
alert"**. If you get the test message on your phone, everything works.

The **"What the monitor saw (run history)"** section is the best way to see
what's happening. It shows a table of every run from the server:

- **Time (ET)** — when the monitor ran
- **Status** — `ran`, `disabled`, `outside hours`, or `error`
- **Duration** — how long the run took
- **Provider** — which price source was used
- **Checked** — how many tickers it looked at
- **Egress** — how many bytes this run sent out (see "Network egress" below)
- **Alerts** — how many alerts were sent / failed
- **Details** — expandable list of the prices it saw (with the bytes each
  single API call used), even when nothing crossed your threshold

If the table is empty or the **Status** column shows `error`, the **Details**
column will tell you why (e.g. missing Telegram credentials, no config, etc.).

> **"Check schedule"** — the panel also shows whether the server's schedule is
> armed and when it next fires. If it says the schedule is **not running**,
> click **"Upload config to server"** (Step 3) to re-install it.

### 📡 Network egress (staying inside the free tier)

Your free Google Cloud tier includes **1 GB of outbound traffic per month**
(from North America, where this VM lives). The panel's **"Network egress"**
block in the "Usage & health" card shows:

- **This month** — total bytes sent so far, compared against the 1 GB free
  limit, as a percentage (turns red near 80%).
- **Per day** — a breakdown of each day's usage.

The **Egress** column in the run-history table shows how many bytes each run
used, and the expandable price list shows the bytes for each individual API
call. This is measured on the server itself (from the network interface), so
it reflects real bytes the monitor sent — no extra Google setup needed.

> If you ever switch the VM to a different region, the free egress allowance
> can differ (some regions offer more). The panel assumes 1 GB/month, the
> N. America allowance.

> *(Manual check:)* `cat ~/PortfolioIsMoving/monitor.log` on the server shows
> a new line every 10 minutes during market hours, and
> `cat ~/PortfolioIsMoving/run_history.json` holds the structured run records
> the panel displays. `cat ~/PortfolioIsMoving/network_usage.json` holds the
> monthly + per-day egress tally.

---

## 🛑 Stopping it (if you ever need to)

```bash
sudo systemctl disable --now portfolioismoving.timer
```

---

## 🔴 If something goes wrong

| Problem | What to do |
|---------|------------|
| The panel says "gcloud not installed" | Do Part 1 (install the Google Cloud CLI), then restart the panel. |
| "Authenticate" doesn't finish | Make sure you completed the login in the browser that opened. |
| "Server created" but no alerts | In the panel, click **"Upload config to server"**, then **"Send test alert"**. Also check the **"What the monitor saw"** table — if it shows `error`, read the Details column. If it shows `outside hours`, the market simply isn't open yet. |
| Run history stays empty / schedule shows "NOT running" | The server's timer isn't firing. Click **"Upload config to server"** (Step 3) to re-install it, then click **"Check schedule"** — it should show "Schedule armed" with a next-run time. |
| The free e2-micro isn't available | The panel uses `us-central1-a` (a free region). If it fails, see the manual method below. |
| You get charged | You shouldn't — the **$1 budget alert** will email you first. If you see it, stop the server immediately. |
| Card verification fails | Use a different card (a standard bank debit or credit card). Revolut usually works but some banks' cards are rejected. |

---

## 🧠 The short version

1. Sign up at Google Cloud (use a Revolut card with a small balance; enable 2FA).
2. Install the Google Cloud CLI (cloud.google.com/sdk/docs/install).
3. Run `python cloud_manager.py` → open `http://localhost:8000`.
4. Click **Authenticate** → **Create free server** → fill in your portfolio →
   **Upload config** → **Set $1 budget alert** → **Send test alert**.
Done. ✅

---

## 📋 Appendix — manual method (if the panel can't create the server)

If the panel's "Create server" button ever fails, you can create it by hand:

1. In Google Cloud, go to **Compute Engine → Create Instance**.
2. Name it `stock-monitor`, region `us-central1-a`, machine type `e2-micro`.
3. Click **Create**, then click **SSH**.
4. In the SSH terminal, run:
   ```bash
   cd ~ && wget -q https://github.com/indiefunda/PortfolioIsMoving_Cloud/archive/refs/heads/master.zip && unzip -q master.zip && mv PortfolioIsMoving_Cloud-master PortfolioIsMoving && cd PortfolioIsMoving
   bash setup_cloud.sh
   ```
5. Then use the panel's **"Upload config"** button to send your settings.
