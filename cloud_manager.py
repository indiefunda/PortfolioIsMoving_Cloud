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
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_local.json")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets_local.json")
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


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
GCLOUD_CANDIDATES = [
    "gcloud",
    r"C:\Users\Achilles\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"C:\Users\Achilles\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud",
    r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"$LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
]


def _find_gcloud():
    """Return the gcloud command to use, or None if not found."""
    # 1. On PATH
    found = shutil.which("gcloud")
    if found:
        return found
    # 2. Known install locations
    localappdata = os.environ.get("LOCALAPPDATA", "")
    for cand in GCLOUD_CANDIDATES:
        if cand.startswith("$LOCALAPPDATA") and localappdata:
            cand = cand.replace("$LOCALAPPDATA", localappdata)
        if cand and os.path.exists(cand):
            return cand
    return None


def gcloud_available():
    return _find_gcloud() is not None


def run_gcloud(args, timeout=120):
    """Run a gcloud command and return (success, stdout, stderr)."""
    gcloud = _find_gcloud()
    if not gcloud:
        return False, "", "gcloud not found. Install the Google Cloud CLI."
    cmd = [gcloud] + args
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
    # Search all candidate zones for the VM.
    for zone in VM_ZONES:
        ok, out, _ = run_gcloud(
            ["compute", "instances", "describe", VM_NAME, "--zone", zone,
             "--format=value(status)", "--quiet"], timeout=60)
        if ok and out.strip():
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
    <button class="btn-ghost" onclick="checkUsage()">📊 Check usage so far</button>
    <button class="btn-ghost" onclick="testAlert()">📲 Send test Telegram alert</button>
    <div class="log" id="log">Command output will appear here.</div>
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
  $('provider').value = d.config.provider || 'finnhub';
  $('enabledToggle').checked = !!d.config.enabled;
  const p = d.config.provider || 'finnhub';
  $('apikey').value = d.secrets[p+'_key'] || d.secrets.price_api_key || '';
  $('token').value = d.secrets.telegram_bot_token || '';
  $('chatid').value = d.secrets.telegram_chat_id || '';
  renderChips(); updateProviderUI();
  refreshStatus();
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
  const d = await api('/api/upload', {
    tickers, threshold_pct: parseFloat($('threshold').value)||5.0,
    enabled: $('enabledToggle').checked, provider: $('provider').value,
    finnhub_key: $('apikey').value.trim(), twelvedata_key: null,
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
  showMsg('Checking usage...','ok');
  const d = await api('/api/usage');
  $('log').textContent = d.output || '';
  if(d.ok){ const u=d.usage||{}; $('usageStatus').textContent = 'Cost so far: '+(u.cost||'$0.00'); }
  else { $('usageStatus').textContent = d.error||'Could not check.'; } }

async function testAlert(){
  showMsg('Sending test alert...','ok');
  const d = await api('/api/test_alert');
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Test alert sent (check Telegram)' : '❌ '+d.error, d.ok?'ok':'err'); }

load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

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
            self._send_json({
                "gcloud": gcloud_available(),
                "auth": auth_status(),
                "vm": vm_status(),
            })
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
        ok, home, _ = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", "echo $HOME", "--quiet"], timeout=60)
        home = home.strip() if ok and home.strip() else "/home/Achilles"
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
        ok, out, err = run_gcloud([
            "billing", "projects", "describe", project,
            "--format=value(billingEnabled)", "--quiet"], timeout=60)
        self._send_json({"ok": ok, "error": err or ("" if ok else out),
                         "output": out + err, "usage": {"cost": "$0.00 (free tier)"}})

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
        # Upload the script
        ok, out, err = run_gcloud([
            "compute", "scp", "--zone", zone, script_path,
            f"{VM_NAME}:/home/Achilles/", "--quiet"], timeout=120)
        # Run it
        if ok:
            ok2, out2, err2 = run_gcloud([
                "compute", "ssh", "--zone", zone, VM_NAME,
                "--command", "cd /home/Achilles && python3 _test_alert.py",
                "--quiet"], timeout=120)
            ok = ok2; out += out2; err += err2
            # Clean up the script on the VM
            run_gcloud([
                "compute", "ssh", "--zone", zone, VM_NAME,
                "--command", "rm -f /home/Achilles/_test_alert.py",
                "--quiet"], timeout=60)
        # Clean up locally
        try:
            os.remove(script_path)
        except OSError:
            pass
        self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            cfg = load_config(); secrets = load_secrets()
            cfg["tickers"] = [t.strip().upper() for t in data.get("tickers", []) if t.strip()]
            cfg["threshold_pct"] = float(data.get("threshold_pct", 5.0))
            cfg["enabled"] = bool(data.get("enabled", True))
            cfg["provider"] = data.get("provider", "finnhub")
            provider = cfg["provider"]
            if data.get("finnhub_key") is not None:
                secrets["finnhub_key"] = data["finnhub_key"]
            if data.get("twelvedata_key") is not None:
                secrets["twelvedata_key"] = data["twelvedata_key"]
            secrets["telegram_bot_token"] = data.get("telegram_bot_token", "")
            secrets["telegram_chat_id"] = data.get("telegram_chat_id", "")
            save_config(cfg); save_secrets(secrets)
            # Now upload to VM
            project = get_project()
            if not project:
                self._send_json({"ok": False, "error": "Not authenticated to Google."})
                return
            ok, out, err = self._deploy_to_vm(project)
            self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})
        else:
            self._send_json({"error": "not found"}, 404)


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
        port = 8001
        server = ThreadingHTTPServer(("", port), Handler)
    print(f"PortfolioIsMoving Cloud panel: http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
