# SynthTel Sender v4

SynthTel is a multi-tenant email campaign platform with a Python backend and a single-page React frontend. It runs as a systemd service on a VPS, proxies its API through nginx, and stores all state in SQLite.

---

## Requirements

| Component | Minimum |
|-----------|---------|
| OS | Debian 11+ or Ubuntu 22.04+ (64-bit) |
| Python | 3.10+ |
| Web server | nginx (configured by install script) |
| Init system | systemd |
| Outbound ports | 80, 443 open; port 25 optional (needed for MX/office methods) |
| RAM | 512 MB minimum; 1 GB recommended for concurrent campaigns |

The install script handles all system-level setup including Python packages, nginx, ufw, and fail2ban.

---

## Fresh Install

Run once as root on a clean Debian/Ubuntu VPS:

```bash
bash install.sh
```

What this does (in order):

1. Installs system packages: `nginx`, `python3`, `python3-pip`, `openssh-client`, `sshpass`, `curl`, `wget`, `git`, `ufw`, `fail2ban`
2. Installs Python packages: `bcrypt`, `msal`, `requests`, `dnspython`, `pysocks`, `paramiko`, `fpdf2`, `Pillow`, `qrcode`, `boto3`, `impacket`
3. Creates `/opt/synthtel/core/` and `/var/www/html/`
4. Writes `/etc/systemd/system/synthtel.service` and enables it
5. Writes the nginx vhost (`/etc/nginx/sites-available/synthtel`) and restarts nginx
6. Configures ufw: allow 22, 80, 443; deny everything else
7. Configures fail2ban for SSH and nginx
8. Installs the 9proxy client daemon (optional residential proxy support)
9. Writes a placeholder `index.html`

After `install.sh` finishes, deploy the application files:

**Option A — deploy from GitHub (recommended for a fresh VPS):**

```bash
curl -fsSL https://raw.githubusercontent.com/ghostybox01/z/main/bootstrap.sh | bash
```

This pulls the latest commit, deploys all backend modules and the frontend, pins the installed SHA for the in-app updater, and restarts the service.

**Option B — deploy from your local machine:**

```bash
# Edit deploy.sh and set VPS_IP to your server's IP, then:
bash deploy.sh
```

---

## Accessing the UI

Once deployed, open a browser to:

```
http://<your-vps-ip>/
```

nginx serves the React SPA on port 80 and reverse-proxies `/api/*` to the Python backend on `127.0.0.1:5001`. The backend never listens on a public port directly.

**First-time login:**

On first run a default `admin` user is created with a random password, which is printed in the application log. Retrieve it with:

```bash
sudo grep -E 'Generated admin password|admin password' /opt/synthtel/synthtel.log
```

You must change this password immediately after first login.

Sessions last 24 hours by default. Failed login attempts are rate-limited; 10 failures from one IP triggers a 15-minute lockout.

---

## Runtime Paths

| Item | Path |
|------|------|
| Install directory | `/opt/synthtel/` |
| Backend entry point | `/opt/synthtel/core/server.py` |
| Database | `/opt/synthtel/synthtel.db` |
| File uploads | `/opt/synthtel/files/<user_id>/<category>/` |
| Log file | `/opt/synthtel/synthtel.log` (2 MB rotating, 3 backups) |
| Frontend | `/var/www/html/index.html` |
| Environment overrides | `/opt/synthtel/.env` |

Environment variables that override defaults:

| Variable | Default |
|----------|---------|
| `SYNTHTEL_DB` | `/opt/synthtel/synthtel.db` |
| `SYNTHTEL_FILES` | `/opt/synthtel/files` |
| `SYNTHTEL_INSTALL_DIR` | `/opt/synthtel` |
| `SYNTHTEL_LOG` | `/opt/synthtel/synthtel.log` |

---

## Campaign Sending Methods

SynthTel supports eight sending methods. Select the method in the **Method** tab before starting a campaign.

### SMTP Relay (`smtp`)

Sends through one or more SMTP relay servers using standard AUTH SMTP. Supports SSL (port 465), STARTTLS (port 587), and plain (port 25).

**Configuration (Method → SMTP tab):**

- Add servers individually or bulk-paste in any of these formats:
  - `smtps://user@domain.com:password@mail.host.com:465`
  - `user:pass@host:port`
  - `host:port:user:pass`
  - `host|port|user|pass`
  - JSON: `{"host":"...","port":587,"username":"...","password":"..."}`
- Use **Parse + Validate** to test connectivity before the campaign
- Rotation modes: random, sequential, or round-robin across servers

**Optional SOCKS5 proxy:** You can route SMTP traffic through a SOCKS5 proxy by adding proxies in the **Proxies** tab. SSL connections through SOCKS5 are auto-downgraded to STARTTLS.

**Common SMTP presets available in the UI:**

| Provider | Host | Port |
|----------|------|------|
| SendGrid | smtp.sendgrid.net | 587 |
| Mailgun | smtp.mailgun.org | 587 |
| AWS SES | email-smtp.\<region\>.amazonaws.com | 587 |
| Brevo | smtp-relay.brevo.com | 587 |
| Zoho | smtp.zoho.com | 587 |

---

### Direct MX (`mx`)

Resolves each recipient's MX records and connects directly on port 25, bypassing all relays. Requires a SOCKS5 proxy with port 25 outbound access — the proxy IP is the sending IP, so it must not be blacklisted.

**Configuration (Method → Direct MX tab):**

1. Add SOCKS5 proxies in the **Proxies** tab (format: `socks5://host:port` or `user:pass@host:port`)
2. Select **Direct MX** as the method
3. Optionally click **Test All (port 25)** to verify each proxy can reach an MX server before sending

The sender rotates through all configured SOCKS5 proxies per email. Greylisted recipients are automatically retried after 90 seconds. Microsoft domains (`hotmail.com`, `outlook.com`, etc.) get an enforced minimum 8-second inter-send delay.

---

### SSH Tunnel (`tunnel`)

Opens an SSH connection to a remote server and creates a local SOCKS5 tunnel (`127.0.0.1:1080`). Campaign traffic is then routed through that tunnel — either to an SMTP relay or directly to MX (port 25).

**Configuration (Method → Tunnel tab):**

| Field | Description |
|-------|-------------|
| SSH Host | IP or hostname of the SSH server |
| SSH Port | Default 22 |
| SSH User | Default `root` |
| SSH Key / Password | PEM private key string, path to key file, or password for sshpass |
| Local Port | Local SOCKS5 port (default 1080) |
| EHLO Domain | Override for the EHLO handshake (defaults to From domain) |

Multiple tunnels can be configured; they rotate per email. The tunnel manager monitors tunnel health and auto-restarts crashed SSH processes (up to 5 restarts before disabling a tunnel).

**ISP variant (legacy):** If `tunnelType` is set to `isp`, the VPS connects to an RDP-hosted 3proxy instance instead of opening an SSH tunnel. See the Proxies section below.

---

### API / ESP (`api`)

Sends via a transactional email provider's HTTP API. No SMTP credentials required — only an API key.

**Supported providers:**

| Provider | Key format |
|----------|-----------|
| SendGrid | `SG.…` |
| Brevo | `xkeysib-…` or `xsmtpsib-…` |
| Resend | `re_…` |
| Postmark | `server_…` |
| Mailgun | `key-…` (requires sending domain + US/EU region) |
| SparkPost | US or EU endpoint |
| AWS SES | IAM Access Key ID + Secret (API mode, not SMTP) |

The provider is auto-detected from the key prefix when you paste it. For AWS SES, use the **Auto-detect region** button to scan all SES regions and find the one that is authorized for your IAM key.

**Configuration (Method → API tab):**

1. Select provider from the dropdown
2. Paste the API key (auto-detection will correct the provider if needed)
3. For Mailgun: enter your sending domain and select US or EU
4. For AWS SES: enter Access Key ID + Secret, then click Auto-detect region
5. Save and test

---

### OWA / Exchange Web Services (`owa`)

Authenticates to an Exchange server using Exchange Web Services (EWS) and sends through the user's mailbox. Supports NTLM and Basic auth. Works with on-premise Exchange and Office 365 OWA endpoints.

**Configuration (Method → OWA tab):**

| Field | Example |
|-------|---------|
| EWS URL | `https://mail.company.com/EWS/Exchange.asmx` |
| Username | `user@company.com` or `DOMAIN\user` |
| Password | Account password |

Provider auto-detection can discover the EWS URL from the email domain via autodiscover. Use the **Detect Provider** step in the B2B tab for guided autodiscovery.

---

### CRM Integration (`crm`)

Sends through a CRM platform's native email API, useful for tracking sends inside CRM activity feeds.

**Supported platforms:**

- Salesforce
- HubSpot
- Zoho CRM
- Pipedrive
- Microsoft Dynamics
- Custom (generic REST endpoint)

**Configuration (Method → CRM tab):**

| Field | Description |
|-------|-------------|
| Provider | Select from the dropdown |
| URL | CRM instance URL or API endpoint |
| Username | CRM login |
| Password / Token | API token or password |

---

### B2B — Office 365 Thread Simulation (`b2b`)

Logs into one of your own email accounts (Office 365, Google Workspace, Gmail, Yahoo, etc.), extracts real contact addresses from received mail, and replies to those existing threads — or sends fresh messages — using the account's own outbound path. Since the email originates from a real authenticated session, it bypasses most bulk-mail filters.

**The B2B tab is a four-step wizard:**

**Step 1 — Detect Provider**

Enter the sender email address. SynthTel probes MX records and autodiscover to identify whether it is Office 365, Google Workspace, or another IMAP provider.

**Step 2 — Authenticate**

Three login methods are available, tried in order:

| Method | How it works |
|--------|-------------|
| Username + Password | Direct ROPC auth (Microsoft); IMAP SSL/STARTTLS (others). For Microsoft, tries 5 app IDs across 3 authority endpoints automatically. |
| Browser / Device Code | Microsoft OAuth device-code flow — opens `microsoft.com/devicelogin`. Works with MFA and conditional access policies. |
| Bearer Token | Paste a raw access token grabbed from browser cookies or any OAuth flow. |

**Step 3 — Extract Leads**

Pulls the `From:` addresses of recent received mail from the authenticated inbox. Filters out no-reply addresses, mailing lists, and ESP senders. Deduplicates by domain. The resulting list can be loaded directly into the main Leads tab.

**Step 4 — Send**

Sends the campaign through the authenticated account using:
- Microsoft Graph API (for Office 365 / Outlook.com accounts)
- SMTP AUTH (for IMAP-based accounts)

Reply mode adds `In-Reply-To` and `Re:` prefix to thread correctly with the original message.

---

### Office Admin — M365 Inbound Connector (`office`)

Connects to port 25 on your Microsoft 365 tenant's inbound connector smart host and delivers mail without credentials. This requires a one-time setup in Exchange Admin Center to whitelist the VPS's public IP.

**Setup (Method → Office Admin tab):**

**Step 1 — Probe this VPS**

Click the probe button. SynthTel checks whether outbound port 25 is open from the VPS and displays the public IP that must be whitelisted.

**Step 2 — Add the VPS IP to an M365 Inbound Connector**

In Exchange Admin Center:

1. Go to **Mail flow → Connectors**
2. Create a new inbound connector (type: **From the Internet**)
3. Under **Sender IP addresses**, add the VPS public IP from Step 1
4. Note the smart host hostname (format: `yourtenant-com.mail.protection.outlook.com`)

**Step 3 — Configure the connector hostname in SynthTel**

Paste the smart host hostname into the **M365 Smart Host** field. Campaigns will then send via `hostname:25` with no auth.

---

## Setting Up Proxies (3proxy on Windows RDP)

The **Direct MX** and **SMTP** methods can route through SOCKS5 proxies. A common source is a Windows RDP instance running 3proxy. Run the following in **PowerShell as Administrator** on the RDP:

**Step 1 — Download and extract 3proxy (one-time):**

```powershell
Invoke-WebRequest 'https://github.com/3proxy/3proxy/releases/download/0.9.5/3proxy-0.9.5-win64.zip' -OutFile 3p.zip
Expand-Archive 3p.zip -Force
Copy-Item (Get-ChildItem -Recurse -Filter 3proxy.exe | Select -First 1).FullName 3proxy.exe
```

**Step 2 — Open the Windows Firewall port (one-time):**

```powershell
netsh advfirewall firewall add rule name="3proxy" dir=in action=allow protocol=tcp localport=1080
```

> Without this step, Windows Firewall silently blocks the VPS connection even when 3proxy is running. This is the most common cause of test failures.

**Step 3 — Write the config and start 3proxy (each session):**

```powershell
"socks -p1080 -i0.0.0.0" | Out-File 3p.cfg -Encoding ascii
Start-Process -FilePath .\3proxy.exe -ArgumentList .\3p.cfg -WindowStyle Hidden
```

After 3proxy is running, add the proxy in SynthTel's **Proxies** tab as `socks5://RDP-IP:1080`.

SynthTel can also auto-restart 3proxy via SSH if the SOCKS5 check fails: provide the RDP's SSH host, user, password, and port in the tunnel config and SynthTel will reconnect and restart the process automatically.

**Alternative: residential ISP proxies via 9proxy or niceproxy.io**

For the **ISP** variant of the tunnel method, you need SOCKS5 proxies tied to a real ISP (Shaw, Rogers, Bell, Cogeco) whose SMTP servers accept mail from their own residential IP ranges.

- **9proxy:** Go to **Proxies → 9proxy tab**, enter your API key, pick the ISP, and click **Fetch Proxies**. The ISP SMTP host is filled in automatically.
- **niceproxy.io / manual:** Use **Bulk Import** — paste lines in `user:pass:host:port` format and enter the ISP SMTP host (e.g. `smtp.shaw.ca`).

---

## Updating

### In-app update button (recommended)

1. Log in as an admin
2. Go to **Settings → Updates**
3. Click **Check for Updates** — this compares the current installed SHA against the latest commit on the tracked GitHub branch
4. Click **Apply Update** — SynthTel downloads all tracked files from GitHub and writes them to `/opt/synthtel/`, then schedules a service restart

Tracked files updated in-app:

- All `core/*.py` modules
- `index.html` and static library assets

### Server-side auto-pull

To have the VPS poll GitHub and update automatically without browser interaction, set the following in `/opt/synthtel/.env`:

```bash
SYNTHTEL_GITHUB_AUTO_PULL=1
SYNTHTEL_GITHUB_AUTO_PULL_INTERVAL=300   # seconds; range 60–3600, default 300
```

Then restart the service:

```bash
systemctl restart synthtel
```

### Manual curl fallback

If the service is broken and the UI is inaccessible, run on the VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/ghostybox01/z/main/pull.sh | bash
```

This re-downloads all backend modules and `index.html` from the deploy mirror and restarts the service.

### Update source configuration

By default updates are pulled from `ghostybox01/z` on the `main` branch. Override with environment variables or via **Settings → Updates → Config** (admin only):

| Variable | Default |
|----------|---------|
| `SYNTHTEL_GH_OWNER` | `ghostybox01` |
| `SYNTHTEL_GH_REPO` | `z` |
| `SYNTHTEL_GH_BRANCH` | `main` |
| `SYNTHTEL_GH_TOKEN` | _(empty — set to raise GitHub rate limits)_ |

---

## Multi-Tenant User Management

SynthTel supports multiple users with isolated data. All file uploads and campaign data are scoped per user (`files/<user_id>/`).

### Roles

| Role | Capabilities |
|------|-------------|
| `user` | Run campaigns, manage own files, configs, leads, and senders |
| `admin` | All user capabilities + create/disable/reset users, send Telegram notifications to users |
| `superadmin` | All admin capabilities + change any user's role |
| `moderator` | Can view and reply to support tickets; cannot manage users |

### Managing users (admin UI)

Go to **Account → Admin** (admin role required):

- **Create user:** Enter username, password (minimum 8 characters), role, and optional expiry date
- **Disable/Enable user:** Toggle button — disabling immediately invalidates all active sessions
- **Reset password:** Enter new password for any user
- **Set expiry:** Accounts with an `expires_at` date are locked out after that date. Leave blank for no expiry.

### API

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/admin/users` | Create user (`username`, `password`, `role`, `expires_at`) |
| GET | `/api/admin/users` | List all users (admin) |
| POST | `/api/admin/users/<id>/toggle` | Enable or disable a user |
| POST | `/api/admin/users/<id>/password` | Reset a user's password |
| POST | `/api/admin/users/<id>/expiry` | Set or clear an account expiry date |
| POST | `/api/admin/set-role` | Change a user's role (superadmin only) |

---

## Telegram Notifications

SynthTel uses a Telegram bot for campaign notifications, login alerts, and optional 2FA.

### Bot setup (admin)

1. Open Telegram and message `@BotFather`
2. Send `/newbot` and follow the prompts to get a bot token
3. In SynthTel, go to **Account → Telegram Bot** (admin only)
4. Paste the token and click **Save**
5. Optionally set a notification channel username

### Linking your account (per user)

1. Go to **Account → Telegram**
2. Click **Connect** — SynthTel generates a one-time link code
3. Click the Telegram link shown, or send the code to the bot
4. Once linked, your Telegram account receives:
   - Login alerts on every successful sign-in
   - Ticket/support reply notifications
   - Link-click notifications (if enabled on redirect links)

### 2FA via Telegram

After linking Telegram, go to **Account → Security** and enable **Telegram 2FA**. A 6-digit code is sent to your Telegram on every login; the login completes only after you enter it.

### Campaign notifications

Campaign start, progress updates, and completion messages are sent to the admin Telegram channel (configured via the bot token). Per-user campaign notifications can be toggled in the campaign options.

---

## Service Management

```bash
# Check status
systemctl status synthtel

# View live logs
journalctl -u synthtel -f

# Restart
systemctl restart synthtel

# View rotating log file
tail -f /opt/synthtel/synthtel.log
```

The service is configured to restart automatically on failure (up to 5 times per 60 seconds).
