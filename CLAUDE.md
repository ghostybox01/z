# SynthTel Sender — Claude Context

## What this project is
SynthTel is a multi-tenant email campaign platform with a Python backend and a single-page React frontend. It runs as a systemd service on a VPS, serves its own HTTP on port 5001 (behind nginx), and stores state in SQLite.

## Git remotes
- **Working repo**: this repo (where all development happens)
- **Deploy mirror**: `https://github.com/ghostybox01/z` — VPS pulls from here via `pull.sh`

### Standard deploy workflow — ALWAYS use this, do not ask
The user prefers force-pushing directly to `ghostybox01/z` main. **Skip PRs entirely.**
After every change, run:

```bash
TOKEN="<partA><partB>"
git remote add ghosty "https://x-access-token:${TOKEN}@github.com/ghostybox01/z.git"
git push --force ghosty HEAD:main
git remote remove ghosty
```

The push uses whatever the current `HEAD` is (feature branch or main — either works because we force).
The CI auto-mirror at `.github/workflows/mirror.yml` is a fallback; the manual force-push is the primary path.

### Token for pushing to ghostybox01/z
Reconstruct by joining these two parts (no space):
- Part A: `ghp_53DDaRzZeMRqdKqCpe`
- Part B: `gOAYmzsmBkj53jsAyX`

(Old `ghp_dVFr…` token was revoked. If the current token ever stops working with `401 Invalid username or token`, ask the user for a fresh PAT — don't try to keep using a dead one.)

## Deploying to VPS
After force-pushing to `ghostybox01/z` main, the VPS auto-pulls if `SYNTHTEL_GITHUB_AUTO_PULL=1` is set in `/opt/synthtel/.env`. Otherwise the user can hit **Account → Updates → Update now** in the UI.

Bootstrap fallback (use only when the in-app updater is itself broken — e.g. server.py crash or a `_gh_request` failure that prevents `/api/update/check` from returning):
```bash
curl -fsSL https://raw.githubusercontent.com/ghostybox01/z/main/pull.sh | bash
```
This pulls the latest files from `ghostybox01/z` into `/opt/synthtel/` and restarts the `synthtel` systemd service.

## Repository layout
```
core/               All backend Python modules
  server.py         HTTP server + routing + auth (entry point)
  campaign.py       Campaign runner — process_campaign(), run_campaign(), _send_one()
  mx_sender.py      Direct-to-MX sending on port 25
  mime_builder.py   MIME message construction
  smtp_sender.py    SMTP relay sending
  api_sender.py     API provider sending (SendGrid, Mailgun, etc.)
  b2b_manager.py    B2B / Office 365 thread sending
  owa_sender.py     OWA webmail sending
  o365_relay.py     Office 365 relay
  crm_sender.py     CRM sending
  tunnel_manager.py SSH tunnel + ISP proxy management
  tags.py           Spintax / tag resolution
  link_encoder.py   Link rotation + tracking
  spam_filter.py    Spam word filter
  suppression_list.py  Unsubscribe / suppression
  email_checker.py  Email validation
  email_sorter.py   Lead sorting utilities
  imap_extractor.py IMAP folder operations
  telegram_bot.py   Telegram campaign notifications

index.html          Entire React frontend (single file, compiled inline)

pull.sh             VPS deploy script
install.sh          Fresh VPS install
bootstrap.sh        Bootstrap helper
deploy.sh           Full deploy helper
update.sh           In-place update helper
_synthtel.service   systemd unit file template
_nginx.conf         nginx vhost config
_nginx_default.conf nginx default config
```

## Runtime paths on VPS
| Item | Path |
|------|------|
| Install dir | `/opt/synthtel/` |
| Entry point | `/opt/synthtel/core/server.py` |
| Database | `/opt/synthtel/synthtel.db` |
| Files/uploads | `/opt/synthtel/files/` |
| Log | `/opt/synthtel/synthtel.log` |
| Frontend | `/var/www/html/index.html` |
| Service | `synthtel.service` |

Environment overrides: `SYNTHTEL_DB`, `SYNTHTEL_FILES`, `SYNTHTEL_INSTALL_DIR`, `SYNTHTEL_LOG`.

## Campaign sending methods
`VALID_METHODS = {"smtp", "api", "owa", "crm", "tunnel", "b2b", "office", "mx"}`

| Method | What it does |
|--------|-------------|
| `smtp` | Standard SMTP relay (with optional SOCKS5 proxy) |
| `mx` | Direct-to-MX on port 25 via SOCKS5 proxy list |
| `tunnel` | SSH tunnel → SOCKS5 → SMTP |
| `isp` | ISP proxy → ISP SMTP (port 25) |
| `office` | Port 25 to an O365 inbound connector (no auth) |
| `api` | SendGrid / Mailgun / Postmark / SES / etc. |
| `owa` | OWA webmail send |
| `crm` | CRM platform send |
| `b2b` | Office 365 thread simulation |

## Key architectural rules
- `_send_one()` in `campaign.py` sends **one email** and returns `(bool, error_str, via_label)` — it must never use `yield` (that makes it a generator and breaks all callers doing `ok, err, via = _send_one(...)`).
- `run_campaign()` and `process_campaign()` are generators — they `yield` event dicts to the HTTP layer.
- The server spawns a background thread per user campaign; the frontend polls `/api/campaign/events?since=N` for progress.
- All file uploads are scoped per user: `FILES_DIR/<user_id>/<category>/<filename>`.
- No references to `aidanbaker812` should appear anywhere in the codebase — all public references point to `ghostybox01/z` only.
