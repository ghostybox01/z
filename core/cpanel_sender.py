"""
core/cpanel_sender.py
─────────────────────
cPanel rotating email sender.

Flow per send:
  1. Pick cPanel account (round-robin)
  2. Generate unique local-part: prefix + 6 random alphanum chars
     e.g. prefix="support" → support4xk2mq@example.com
  3. Create the email account via cPanel UAPI (Email/add_pop)
  4. Auto-detect SMTP port on mail.{domain}: 587 STARTTLS → 465 SSL → 25 plain
  5. Login and send via SMTP using the new account credentials
  6. Track all created accounts; delete them (Email/delete_pop) after campaign

cPanel UAPI reference:
  POST https://host:2083/execute/Email/add_pop
    email, domain, password, quota (0 = unlimited)
  POST https://host:2083/execute/Email/delete_pop
    email, domain
  GET  https://host:2083/execute/Email/list_pops
    domain
  Auth: Authorization: Basic base64(username:password)
"""

import ssl
import smtplib
import socket
import logging
import random
import string
import base64
import json
import urllib.request
import urllib.parse
import time

log = logging.getLogger("synthtel.cpanel")

_CHARS = string.ascii_lowercase + string.digits

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE


# ─────────────────────────────────────────────────────
# Local-part generation
# ─────────────────────────────────────────────────────

def _random_suffix(n: int = 6) -> str:
    return "".join(random.choices(_CHARS, k=n))


def make_local_part(prefix: str) -> str:
    """
    Generate a local-part: (prefix stripped) + 6 random alphanum chars.
    If no prefix, returns 8 random chars.
    Examples:
      prefix="support"  → "supporta4k2mx"
      prefix=""         → "mx9ka2qp"
    """
    p = (prefix or "").strip().lower().replace(" ", "")
    return f"{p}{_random_suffix(6)}" if p else _random_suffix(8)


def make_smtp_password() -> str:
    """Generate a random password that satisfies common cPanel complexity rules."""
    upper   = random.choices(string.ascii_uppercase, k=2)
    lower   = random.choices(string.ascii_lowercase, k=6)
    digits  = random.choices(string.digits, k=3)
    special = random.choices("!@#$", k=1)
    chars   = upper + lower + digits + special
    random.shuffle(chars)
    return "".join(chars)


# ─────────────────────────────────────────────────────
# cPanel UAPI
# ─────────────────────────────────────────────────────

def _cpanel_api(cpanel: dict, endpoint: str, params: dict, method: str = "POST") -> dict:
    """Call a cPanel UAPI endpoint. Returns parsed JSON or error dict."""
    host = (cpanel.get("host") or "").strip().rstrip("/")
    port = int(cpanel.get("port") or 2083)
    user = (cpanel.get("username") or cpanel.get("user") or "").strip()
    pwd  = (cpanel.get("password") or cpanel.get("pass") or "").strip()
    url  = f"https://{host}:{port}/execute/{endpoint}"

    creds = base64.b64encode(f"{user}:{pwd}".encode()).decode()

    if method == "POST":
        data = urllib.parse.urlencode(params).encode()
        req  = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    else:
        qs  = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{url}?{qs}", method="GET")

    req.add_header("Authorization", f"Basic {creds}")

    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"status": 0, "errors": [str(e)[:200]]}


def create_email_account(cpanel: dict, local_part: str, password: str) -> dict:
    """
    Create local_part@domain via cPanel UAPI.
    Returns {"ok": bool, "email": str, "error": str}
    """
    domain = (cpanel.get("domain") or "").strip()
    r = _cpanel_api(cpanel, "Email/add_pop", {
        "email":    local_part,
        "domain":   domain,
        "password": password,
        "quota":    0,
    })
    ok  = r.get("status", 0) == 1
    err = "; ".join(r.get("errors") or []) if not ok else ""
    return {"ok": ok, "email": f"{local_part}@{domain}", "error": err}


def delete_email_account(cpanel: dict, local_part: str) -> dict:
    """Delete local_part@domain via cPanel UAPI. Returns {"ok": bool}."""
    domain = (cpanel.get("domain") or "").strip()
    r = _cpanel_api(cpanel, "Email/delete_pop", {
        "email":  local_part,
        "domain": domain,
    })
    return {"ok": r.get("status", 0) == 1}


# ─────────────────────────────────────────────────────
# SMTP detection + sending
# ─────────────────────────────────────────────────────

def detect_smtp_port(smtp_host: str, timeout: int = 6) -> tuple:
    """
    Probe smtp_host on 587 (STARTTLS) → 465 (SSL) → 25 (plain).
    Returns (port, mode). Falls back to (587, 'starttls') if all fail.
    """
    for port, mode in [(587, "starttls"), (465, "ssl"), (25, "plain")]:
        try:
            s = socket.create_connection((smtp_host, port), timeout=timeout)
            s.close()
            return port, mode
        except Exception:
            continue
    return 587, "starttls"


def send_smtp_cpanel(
    smtp_host: str,
    smtp_port: int,
    smtp_mode: str,
    msg_from:  str,
    smtp_pass: str,
    msg_to:    str,
    raw_msg:   bytes,
    timeout:   int = 30,
) -> dict:
    """
    Authenticate as msg_from and send raw_msg to msg_to via cPanel SMTP.
    Returns {"ok": bool, "error": str}
    """
    raw_msg = raw_msg.replace(b'\r\n', b'\n').replace(b'\r', b'\n').replace(b'\n', b'\r\n')
    domain  = msg_from.split("@")[-1] if "@" in msg_from else "mail.local"

    try:
        if smtp_mode == "ssl" or smtp_port == 465:
            conn = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout, context=_SSL_CTX)
        else:
            conn = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)

        with conn:
            conn.ehlo(domain)
            if smtp_mode == "starttls":
                try:
                    conn.starttls(context=_SSL_CTX)
                    conn.ehlo()
                except Exception:
                    pass
            conn.login(msg_from, smtp_pass)
            conn.sendmail(msg_from, [msg_to], raw_msg)

        return {"ok": True}
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "error": f"SMTP auth failed: {str(e)[:120]}"}
    except smtplib.SMTPRecipientsRefused as e:
        return {"ok": False, "error": f"Recipient refused: {str(e)[:120]}"}
    except smtplib.SMTPException as e:
        return {"ok": False, "error": f"SMTP error: {str(e)[:200]}"}
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return {"ok": False, "error": f"Connection to {smtp_host}:{smtp_port}: {str(e)[:160]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ─────────────────────────────────────────────────────
# Probe (used by test endpoint)
# ─────────────────────────────────────────────────────

def probe_cpanel(cpanel: dict, timeout: int = 15) -> dict:
    """
    Test a cPanel account:
      1. Call Email/list_pops to verify API credentials
      2. Detect SMTP port on mail.{domain} (or smtpHost override)

    Returns:
      {"ok": bool, "domain": str, "smtp_host": str, "smtp_port": int,
       "smtp_mode": str, "account_count": int, "latency_ms": int, "error": str}
    """
    t0     = time.time()
    domain = (cpanel.get("domain") or "").strip()

    r = _cpanel_api(cpanel, "Email/list_pops", {"domain": domain}, method="GET")
    api_ok = r.get("status", 0) == 1
    if not api_ok:
        err = "; ".join(r.get("errors") or ["API auth failed — check host/username/password"])
        return {"ok": False, "error": f"cPanel API: {err[:200]}"}

    smtp_host = (cpanel.get("smtpHost") or f"mail.{domain}").strip()
    smtp_port, smtp_mode = detect_smtp_port(smtp_host, timeout=timeout)

    data  = r.get("data") or []
    count = len(data) if isinstance(data, list) else 0

    return {
        "ok":            True,
        "domain":        domain,
        "smtp_host":     smtp_host,
        "smtp_port":     smtp_port,
        "smtp_mode":     smtp_mode,
        "account_count": count,
        "latency_ms":    round((time.time() - t0) * 1000),
    }
