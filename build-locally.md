# Building SynthTel Sender locally

Builds the web-based sender as a real desktop app: bundled HTTP server +
native window (WKWebView on macOS, Edge WebView2 on Windows). Entry
point is `desktop.py`, which boots `server.py` on `127.0.0.1:5001` in
a background thread and opens a window pointed at it.

## Prereqs

- Python 3.12 (3.11+ should also work)
- A clone of this repo, checked out to the branch/tag you want to ship

```sh
git clone https://github.com/aidanbaker812-prog/zzz.git
cd zzz
```

## macOS — `SynthTel-Sender.dmg`

Build on a Mac (Apple Silicon or Intel — output matches host arch).

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install pyinstaller bcrypt dnspython requests "pywebview[cocoa]"

rm -rf build dist dmg-staging "SynthTel Sender.spec"

pyinstaller --noconfirm --windowed --name "SynthTel Sender" \
  --add-data "core:core" --add-data "index.html:." --add-data "libs:libs" \
  --hidden-import bcrypt --hidden-import dns.resolver \
  --hidden-import webview.platforms.cocoa \
  --collect-submodules core --collect-all webview \
  desktop.py

mkdir -p dmg-staging
cp -R "dist/SynthTel Sender.app" dmg-staging/
ln -s /Applications dmg-staging/Applications
hdiutil create -volname "SynthTel Sender" -srcfolder dmg-staging -ov \
  -format UDZO "dist/SynthTel-Sender.dmg"
```

Result: `dist/SynthTel-Sender.dmg`. Double-click → drag to Applications →
right-click the app → **Open** the first time (unsigned, so Gatekeeper
needs the bypass).

## Windows — `synthtel-sender.exe`

Build on a Windows machine (PowerShell). Edge WebView2 Runtime is
preinstalled on Windows 11 and most Windows 10 builds — if missing, the
app will offer a one-click install.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install pyinstaller bcrypt dnspython requests pywebview

pyinstaller --noconfirm --windowed --onefile --name synthtel-sender `
  --add-data "core;core" --add-data "index.html;." --add-data "libs;libs" `
  --hidden-import bcrypt --hidden-import dns.resolver `
  --hidden-import webview.platforms.edgechromium `
  --collect-submodules core --collect-all webview `
  desktop.py
```

Result: `dist\synthtel-sender.exe`. Double-click to launch.

## Data locations

The bundled app writes user data to a per-OS user directory (no admin
needed):

| OS      | Path                                              |
| ------- | ------------------------------------------------- |
| macOS   | `~/Library/Application Support/SynthTel/`         |
| Windows | `%LOCALAPPDATA%\SynthTel\`                        |
| Linux   | `/opt/synthtel/` (production deploy default)      |

Override with env vars: `SYNTHTEL_DB`, `SYNTHTEL_LOG`, `SYNTHTEL_FILES`,
`SYNTHTEL_INSTALL_DIR`, `SYNTHTEL_PORT`.

## Publishing the binaries

Don't commit them — they're large and bloat git history. Attach to a
GitHub Release instead:

1. Open https://github.com/aidanbaker812-prog/zzz/releases/new
2. Tag `v0.1.0` (or whatever), target `main`
3. Drag `synthtel-sender.exe` and `SynthTel-Sender.dmg` into the assets
4. Publish — testers get direct download links

## Optional integrations

Add to `pip install` and `--hidden-import` only if you need them:

| Feature              | Package      | Hidden import     |
| -------------------- | ------------ | ----------------- |
| SSH SOCKS tunnels    | `paramiko`   | `paramiko`        |
| SOCKS proxy support  | `pysocks`    | `socks`           |
| AWS S3 / SES         | `boto3`      | `boto3`           |
| Microsoft OAuth      | `msal`       | `msal`            |
| Windows admin (WinRM)| `pywinrm`    | `winrm`           |
| Windows admin (RPC)  | `impacket`   | `impacket`        |
