"""
core/api_sender.py — SynthTel Email API Sender
===============================================
Replaces send_api() and build_api_headers() in synthtel_server.py.

Improvements over the original:
  • Mailgun properly supported (was stubbed with YOUR_DOMAIN placeholder)
  • Postmark added as a new provider
  • SparkPost added as a new provider
  • Amazon SES (v2) added as a new provider
  • plain-text body passed to all APIs that support it (better deliverability)
  • Retry on 429 / 503 with Retry-After header parsing
  • Per-provider error messages with actionable fix hints
  • build_api_headers() now delegates to mime_builder for consistency —
    produces the same complete deliverability header set as SMTP/MX sends
  • Provider key validated before making any network request

Usage:
    from core.api_sender import send_api

    status = send_api(
        api_cfg        = {"provider": "brevo", "apiKey": "..."},
        sender         = {"fromEmail": "...", "fromName": "..."},
        lead           = {"email": "...", "name": "..."},
        resolved_html  = html_string,
        resolved_plain = plain_string,
        resolved_subject = subject_string,
        dlv            = dlv_dict,
        custom_headers = [],
    )
"""

import json
import time
import logging
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from typing import Optional
import base64
import mimetypes
import os

log = logging.getLogger(__name__)


def _resolve_attachments(attachments: dict, sender: dict, lead: dict, resolved_subject: str) -> list:
    """
    Convert the attachments dict (same format mime_builder receives) into a flat list:
        [{"filename": str, "content_b64": str, "content_type": str}, ...]
    Handles user-uploaded files and generated types (ICS, QR, PDF, ZIP).
    """
    if not attachments:
        return []
    result = []

    # ── User-uploaded files from disk ──────────────────────────
    for fa in (attachments.get("files") or []):
        path  = fa.get("path") or ""
        name  = fa.get("name") or os.path.basename(path) or "attachment"
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            ctype, _ = mimetypes.guess_type(name)
            if not ctype:
                ctype = "application/octet-stream"
            result.append({"filename": name, "content_b64": base64.b64encode(data).decode(), "content_type": ctype})
        except Exception:
            pass

    # ── Generated attachments via mime_builder ─────────────────
    try:
        from core.mime_builder import (
            _build_ics_attachment, _build_qr_attachment,
            _build_pdf_attachment, _build_zip_attachment,
        )

        ics_cfg = attachments.get("ics")
        if ics_cfg:
            part = _build_ics_attachment(ics_cfg, lead, sender, resolved_subject)
            if part:
                payload = part.get_payload(decode=True) or b""
                result.append({
                    "filename":     ics_cfg.get("name") or "invite.ics",
                    "content_b64":  base64.b64encode(payload).decode(),
                    "content_type": "text/calendar; method=REQUEST; charset=utf-8",
                })

        qr_cfg = attachments.get("qr")
        if qr_cfg:
            _qr_email = (lead.get("email","") if isinstance(lead, dict) else str(lead))
            part, _cid, _bytes, _fmt = _build_qr_attachment(qr_cfg, _qr_email, "")
            if part:
                payload = part.get_payload(decode=True) or b""
                result.append({
                    "filename":     qr_cfg.get("name") or "qr.png",
                    "content_b64":  base64.b64encode(payload).decode(),
                    "content_type": "image/png",
                })

        pdf_cfg = attachments.get("pdf")
        if pdf_cfg:
            part = _build_pdf_attachment(pdf_cfg, "", lead, resolved_subject)
            if part:
                payload = part.get_payload(decode=True) or b""
                result.append({
                    "filename":     pdf_cfg.get("name") or "document.pdf",
                    "content_b64":  base64.b64encode(payload).decode(),
                    "content_type": "application/pdf",
                })

        zip_cfg = attachments.get("zip")
        if zip_cfg:
            part = _build_zip_attachment(zip_cfg, lead, sender, "")
            if part:
                payload = part.get_payload(decode=True) or b""
                result.append({
                    "filename":     zip_cfg.get("name") or "archive.zip",
                    "content_b64":  base64.b64encode(payload).decode(),
                    "content_type": "application/zip",
                })
    except Exception:
        pass

    return result


def _build_multipart_form(fields: list, file_parts: list) -> tuple:
    """
    Build a multipart/form-data body.
    fields:     [(name, value), ...]
    file_parts: [(field_name, filename, content_type, data_bytes), ...]
    Returns (body_bytes, content_type_header_value).
    """
    boundary = uuid.uuid4().hex
    buf = b""
    for name, value in fields:
        buf += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
    for fname, filename, ctype, data in file_parts:
        buf += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{fname}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8") + data + b"\r\n"
    buf += f"--{boundary}--\r\n".encode("utf-8")
    return buf, f"multipart/form-data; boundary={boundary}"


def _norm(html: str, plain: str, subject: str):
    """
    Ensure html, plain, and subject are never empty strings.
    API providers (SendGrid, Brevo, etc.) reject empty content with 400 errors.
    Returns (html, plain, subject) — all guaranteed non-empty.
    """
    subject = (subject or "").strip() or "(no subject)"
    html    = (html or "").strip()
    plain   = (plain or "").strip()

    # If plain is empty, derive it from html by stripping tags
    if not plain and html:
        import re as _re
        plain = _re.sub(r'<[^>]+>', ' ', html)
        plain = _re.sub(r'\s+', ' ', plain).strip()

    # If html is empty but we have plain, wrap it
    if not html and plain:
        html = f"<p>{plain}</p>"

    # Absolute fallback — should never happen in practice
    if not plain:
        plain = subject
    if not html:
        html = f"<p>{subject}</p>"

    return html, plain, subject


# ═══════════════════════════════════════════════════════════════
# PROVIDER REGISTRY
# ═══════════════════════════════════════════════════════════════

# Base API endpoints — Mailgun domain is injected at send time
_API_URLS = {
    "brevo":      "https://api.brevo.com/v3/smtp/email",
    "sendgrid":   "https://api.sendgrid.com/v3/mail/send",
    "resend":     "https://api.resend.com/emails",
    "postmark":   "https://api.postmarkapp.com/email",
    "sparkpost":  "https://api.sparkpost.com/api/v1/transmissions",
    "ses":        "https://email.{region}.amazonaws.com/v2/email/outbound-emails",
    "mailjet":    "https://api.mailjet.com/v3.1/send",
    "smtp2go":    "https://api.smtp2go.com/v3/email/send",
    # mailgun: endpoint built dynamically from domain field
}

SUPPORTED_PROVIDERS = frozenset(_API_URLS.keys()) | {"mailgun"}

# HTTP status codes that are retryable
_RETRY_STATUSES = {429, 503, 502, 504}
_MAX_RETRIES    = 2
_RETRY_DELAY    = 5   # seconds (overridden by Retry-After header if present)


# ═══════════════════════════════════════════════════════════════
# DELIVERABILITY HEADER BUILDER
# ═══════════════════════════════════════════════════════════════

def build_api_headers(
    dlv:            dict,
    lead:           dict,
    custom_headers: list,
    sender:         Optional[dict] = None,
) -> dict:
    """
    Build the deliverability + custom header dict to pass to API providers.
    Uses the same logic as mime_builder._apply_deliverability_headers()
    so SMTP, MX, and API sends all produce identical header sets.

    Returns a flat {header_name: value} dict ready for provider payloads.
    """
    import random
    from core.mime_builder import X_MAILERS

    dlv    = dlv or {}
    lead   = lead or {}
    sender = sender or {}
    hdrs   = {}

    lead_email  = lead.get("email", "")
    from_email  = sender.get("fromEmail", "")
    from_domain = from_email.split("@")[-1] if "@" in from_email else ""

    # List-Unsubscribe
    if dlv.get("listUnsub"):
        parts = []
        unsub_url   = (dlv.get("unsubUrl") or "").replace("#EMAIL", lead_email)
        unsub_email = dlv.get("unsubEmail") or ""
        if unsub_url:
            parts.append(f"<{unsub_url}>")
        if unsub_email:
            parts.append(f"<mailto:{unsub_email}?subject=Unsubscribe&body={lead_email}>")
        if parts:
            hdrs["List-Unsubscribe"] = ", ".join(parts)
    if dlv.get("oneClickUnsub") and dlv.get("listUnsub"):
        hdrs["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # X-Mailer
    xm = dlv.get("xMailer", "none")
    if xm and xm != "none":
        if xm == "random":
            hdrs["X-Mailer"] = random.choice(list(X_MAILERS.values()))
        elif xm == "custom":
            cust = dlv.get("customMailer") or ""
            if cust:
                hdrs["X-Mailer"] = cust
        elif xm in X_MAILERS:
            hdrs["X-Mailer"] = X_MAILERS[xm]

    # Precedence
    prec = dlv.get("precedence", "none")
    if prec and prec != "none":
        hdrs["Precedence"] = prec

    # Feedback-ID
    if dlv.get("feedbackId"):
        hdrs["Feedback-ID"] = dlv["feedbackId"]
    elif dlv.get("feedbackIdAuto") and from_domain:
        hdrs["Feedback-ID"] = f"{uuid.uuid4().hex[:8]}:synthtel:api:{from_domain}"

    # Organization
    if dlv.get("organization"):
        hdrs["Organization"] = dlv["organization"]

    # Priority
    pri = dlv.get("priority", "normal")
    if pri == "high":
        hdrs["X-Priority"] = "1"
        hdrs["Importance"] = "High"
    elif pri == "low":
        hdrs["X-Priority"] = "5"
        hdrs["Importance"] = "Low"

    # Entity ref
    if dlv.get("entityRef"):
        hdrs["X-Entity-Ref-ID"] = str(uuid.uuid4())

    # List-ID
    if dlv.get("listId"):
        hdrs["List-ID"] = dlv["listId"]
    elif dlv.get("listIdAuto") and from_domain:
        slug = from_domain.split(".")[0].lower()
        hdrs["List-ID"] = f"<{slug}.{from_domain}>"

    # Custom headers (protected header check)
    _PROTECTED = frozenset({"from", "to", "subject", "date", "message-id", "mime-version"})
    for ch in (custom_headers or []):
        k = (ch.get("key") or "").strip()
        v = (ch.get("value") or "").strip()
        if k and v and k.lower() not in _PROTECTED:
            hdrs[k] = v

    return hdrs


# ═══════════════════════════════════════════════════════════════
# HTTP REQUEST HELPER
# ═══════════════════════════════════════════════════════════════

def _api_request(
    url:      str,
    payload:  dict,
    headers:  dict,
    provider: str,
    method:   str = "POST",
    retries:  int = _MAX_RETRIES,
    uid              = None,        # campaign owner — pass through from send_api()
) -> int:
    """
    Make a JSON API request with retry logic.
    Returns HTTP status code on success.
    Raises descriptive Exception on failure.

    If uid is provided, only THAT user's campaign-abort flag aborts
    the request — User A's Stop never breaks User B's send.
    """
    raw    = json.dumps(payload).encode("utf-8")
    delay  = _RETRY_DELAY

    # Best-effort campaign-abort hook so retry loops bail out quickly when
    # the user presses Stop instead of waiting through 4×30s of timeouts.
    # CRITICAL: when a uid is supplied we check ONLY that user's
    # CAMPAIGN_CONTROLS entry.  Pre-fix this scanned every user's
    # abort flag, so user A pressing Stop killed user B's API sends.
    def _aborted() -> bool:
        try:
            from core.server import (active_campaigns_lock, CAMPAIGN_CONTROLS)
            with active_campaigns_lock:
                if uid is not None:
                    ctrl = CAMPAIGN_CONTROLS.get(uid) or {}
                    return bool(ctrl.get("abort"))
                # No uid = caller is a one-off (test send, etc.); fall
                # back to the legacy any-user check so explicit aborts
                # of the global pool still surface.
                for ctrl in CAMPAIGN_CONTROLS.values():
                    if ctrl and ctrl.get("abort"):
                        return True
        except Exception:
            pass
        return False

    for attempt in range(retries + 1):
        if _aborted():
            raise Exception(f"API {provider}: aborted by user")
        req = Request(url, data=raw if method == "POST" else None, headers=headers,
                      method=method)
        try:
            # Tighter timeout — 15s instead of 30s.  Most providers respond
            # in well under a second; 15s is plenty for a worst-case TLS
            # handshake + slow region.  Cuts max stop-time roughly in half.
            resp = urlopen(req, timeout=15)
            return resp.status

        except HTTPError as exc:
            if exc.code in _RETRY_STATUSES and attempt < retries:
                # Respect Retry-After if present (capped so a 60s response
                # doesn't pin the worker indefinitely).
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(float(retry_after), 10.0)
                    except (ValueError, TypeError):
                        delay = _RETRY_DELAY
                log.warning("[ApiSender] %s HTTP %d — retrying in %.0fs (attempt %d/%d)",
                            provider, exc.code, delay, attempt + 1, retries)
                # Sleep in 0.5s slices so we react to abort within ~0.5s.
                _slept = 0.0
                while _slept < delay:
                    if _aborted():
                        raise Exception(f"API {provider}: aborted by user during retry")
                    time.sleep(min(0.5, delay - _slept))
                    _slept += 0.5
                continue

            # Parse error body for actionable message
            body   = ""
            detail = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:600]
                err_data = json.loads(body)
                _nested = err_data.get("data") if isinstance(err_data.get("data"), dict) else {}
                detail = (
                    err_data.get("message")
                    or err_data.get("error")
                    or _nested.get("error")
                    or _nested.get("error_code")
                    or err_data.get("detail")
                    or (err_data.get("errors") or [{}])[0].get("message", "")
                    or body
                )
            except Exception:
                detail = body or str(exc)

            if exc.code == 401:
                raise Exception(
                    f"API {provider} 401 Unauthorized — invalid API key. "
                    f"Check your {provider} API key is correct and has send permissions."
                )
            if exc.code == 403:
                raise Exception(
                    f"API {provider} 403 Forbidden — {detail}. "
                    f"Check: 1) API key has send permission, "
                    f"2) sender domain is verified in {provider} dashboard."
                )
            if exc.code == 400:
                raise Exception(f"API {provider} 400 Bad Request — {detail}")
            if exc.code == 422:
                raise Exception(f"API {provider} 422 Unprocessable — {detail}")
            if exc.code == 429:
                raise Exception(
                    f"API {provider} 429 Rate Limited — {detail}. "
                    f"Slow down sends or upgrade your plan."
                )
            raise Exception(f"API {provider} HTTP {exc.code} — {detail}")

        except URLError as exc:
            if attempt < retries:
                log.warning("[ApiSender] %s network error — retrying: %s", provider, exc)
                _slept = 0.0
                while _slept < delay:
                    if _aborted():
                        raise Exception(f"API {provider}: aborted by user during retry")
                    time.sleep(min(0.5, delay - _slept))
                    _slept += 0.5
                continue
            raise Exception(f"API {provider} network error: {exc.reason}")

    raise Exception(f"API {provider} failed after {retries + 1} attempts")


# ═══════════════════════════════════════════════════════════════
# PROVIDER IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════

_BREVO_MISSING_TO_NAME = "name is missing in to"


def _brevo_recipient_display_name(email: str, name: str) -> str:
    """
    Brevo returns 400 with 'name is missing in to' when the name is absent,
    blank, or otherwise not accepted. Prefer the lead name; else the local
    part of the address; else the full address.
    """
    n = (name or "").strip()
    if n:
        return n
    e = (email or "").strip()
    if "@" in e:
        local = e.split("@", 1)[0].strip()
        if local:
            return local
    return e or "Recipient"


def _send_brevo(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    key        = api_cfg.get("apiKey", "")
    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = (lead.get("email") or "").strip()
    lead_name  = lead.get("name", "") or ""

    def _build_payload(to_name: str) -> dict:
        p = {
            "sender":      {"name": from_name, "email": from_email},
            "to":          [{"email": lead_email, "name": to_name}],
            "subject":     subject,
            "htmlContent": html,
            "textContent": plain,
        }
        if reply_to:
            p["replyTo"] = {"email": reply_to}
        if extra_hdrs:
            p["headers"] = extra_hdrs
        if atts:
            p["attachment"] = [{"name": a["filename"], "content": a["content_b64"]} for a in atts]
        return p

    to_display = _brevo_recipient_display_name(lead_email, lead_name)
    payload = _build_payload(to_display)
    hdrs = {"api-key": key, "Content-Type": "application/json"}

    try:
        return _api_request(
            _API_URLS["brevo"], payload,
            hdrs,
            "brevo",
            uid=api_cfg.get("_uid"),
        )
    except Exception as exc:
        if _BREVO_MISSING_TO_NAME not in str(exc).lower():
            raise
        # Retry once with the full address as display name (always non-empty).
        fallback = lead_email or "Recipient"
        if to_display == fallback:
            raise
        payload = _build_payload(fallback)
        return _api_request(
            _API_URLS["brevo"], payload,
            hdrs,
            "brevo",
            uid=api_cfg.get("_uid"),
        )


def _send_sendgrid(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    key        = api_cfg.get("apiKey", "")
    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = lead.get("email", "")
    lead_name  = lead.get("name", "")

    # SendGrid requires:
    # 1. text/plain MUST come before text/html in the content array
    # 2. Both values must be non-empty strings (400 error if empty)
    # _norm() in send_api already guarantees both are non-empty before we get here.
    content = [
        {"type": "text/plain", "value": plain},
        {"type": "text/html",  "value": html},
    ]

    payload = {
        "personalizations": [{"to": [{"email": lead_email, "name": lead_name}]}],
        "from":    {"email": from_email, "name": from_name},
        "subject": subject,
        "content": content,
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}
    if extra_hdrs:
        payload["headers"] = extra_hdrs
    if atts:
        payload["attachments"] = [
            {"content": a["content_b64"], "type": a["content_type"], "filename": a["filename"]}
            for a in atts
        ]

    return _api_request(
        _API_URLS["sendgrid"], payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "sendgrid",
        uid=api_cfg.get("_uid"),
    )


def _send_resend(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    key        = api_cfg.get("apiKey", "")
    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = lead.get("email", "")

    from_str = f"{from_name} <{from_email}>" if from_name else from_email

    payload = {
        "from":    from_str,
        "to":      [lead_email],
        "subject": subject,
        "html":    html,
    }
    if plain:
        payload["text"] = plain
    if reply_to:
        payload["reply_to"] = reply_to
    if extra_hdrs:
        payload["headers"] = extra_hdrs
    if atts:
        payload["attachments"] = [{"filename": a["filename"], "content": a["content_b64"]} for a in atts]

    return _api_request(
        _API_URLS["resend"], payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "resend",
        uid=api_cfg.get("_uid"),
    )


def _send_mailgun(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    """
    Mailgun v3 API — uses multipart/form-data, not JSON.
    Requires api_cfg.mailgunDomain to be set (e.g. "mg.yourco.com").
    """
    from urllib.parse import urlencode

    key           = api_cfg.get("apiKey", "")
    mailgun_domain = api_cfg.get("mailgunDomain") or api_cfg.get("domain") or ""
    if not mailgun_domain:
        raise Exception(
            "Mailgun: mailgunDomain not configured. "
            "Set it to your Mailgun sending domain (e.g. mg.yourco.com)."
        )

    region = api_cfg.get("mailgunRegion", "us").lower()
    base   = "https://api.eu.mailgun.net" if region == "eu" else "https://api.mailgun.net"
    url    = f"{base}/v3/{mailgun_domain}/messages"

    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = lead.get("email", "")
    lead_name  = lead.get("name", "")

    from_str = f"{from_name} <{from_email}>" if from_name else from_email
    to_str   = f"{lead_name} <{lead_email}>" if lead_name else lead_email

    fields = [
        ("from",    from_str),
        ("to",      to_str),
        ("subject", subject),
        ("html",    html),
    ]
    if plain:
        fields.append(("text", plain))
    if reply_to:
        fields.append(("h:Reply-To", reply_to))
    for hname, hval in (extra_hdrs or {}).items():
        fields.append((f"h:{hname}", hval))

    cred = base64.b64encode(f"api:{key}".encode()).decode()

    if atts:
        # Mailgun requires multipart/form-data to carry binary attachments
        file_parts = [
            ("attachment", a["filename"], a["content_type"], base64.b64decode(a["content_b64"]))
            for a in atts
        ]
        body, ct = _build_multipart_form(fields, file_parts)
        req_hdrs = {"Authorization": f"Basic {cred}", "Content-Type": ct}
    else:
        body     = urlencode(fields).encode("utf-8")
        req_hdrs = {"Authorization": f"Basic {cred}", "Content-Type": "application/x-www-form-urlencoded"}

    req  = Request(url, data=body, headers=req_hdrs, method="POST")
    try:
        resp = urlopen(req, timeout=30)
        return resp.status
    except HTTPError as exc:
        body_str = ""
        try:
            body_str = exc.read().decode(errors="replace")[:400]
            detail   = json.loads(body_str).get("message", body_str)
        except Exception:
            detail = body_str or str(exc)
        if exc.code == 401:
            raise Exception("Mailgun 401 — invalid API key or wrong region (try eu/us toggle).")
        if exc.code == 400:
            raise Exception(f"Mailgun 400 Bad Request — {detail}")
        raise Exception(f"Mailgun HTTP {exc.code} — {detail}")


def _smtp2go_request(api_key: str, path: str, body: Optional[dict] = None) -> tuple:
    """
    POST to SMTP2GO v3 API. Returns (parsed_json_or_None, error_string_or_None).
    """
    key = (api_key or "").strip()
    if not key:
        return None, "missing API key"
    url = f"https://api.smtp2go.com/v3/{path.lstrip('/')}"
    payload = {"api_key": key}
    if body:
        payload.update(body)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Smtp2go-Api-Key": key,
    }
    raw = json.dumps(payload).encode("utf-8")
    try:
        resp = urlopen(Request(url, data=raw, headers=headers, method="POST"), timeout=15)
        txt = resp.read().decode("utf-8", errors="replace")
        try:
            return (json.loads(txt) if txt.strip() else {}), None
        except Exception:
            return {"raw": txt}, None
    except HTTPError as exc:
        detail = ""
        try:
            body_s = exc.read().decode("utf-8", errors="replace")[:500]
            err = json.loads(body_s)
            nested = err.get("data") if isinstance(err.get("data"), dict) else {}
            detail = nested.get("error") or nested.get("error_code") or err.get("error") or body_s
        except Exception:
            detail = str(exc)
        return None, f"HTTP {exc.code}: {detail}"
    except URLError as exc:
        return None, f"network: {exc.reason}"
    except Exception as exc:
        return None, str(exc)


# Per-process cache: API keys for which we've already disabled Restrict Recipients
_SMTP2GO_RECIPIENT_RESTRICTION_OFF = set()
# Keys that already had campaign leads bulk-added to the allow list
_SMTP2GO_ALLOWLIST_BULK_DONE = set()
# Keys that lack /allowed_recipients permission — skip further management calls
_SMTP2GO_ALLOWLIST_NO_PERM = set()


def _smtp2go_is_allowlist_error(msg: str) -> bool:
    m = (msg or "").lower()
    if "not on your allow list" in m or "not on your allowlist" in m:
        return True
    return ("allow list" in m or "allowlist" in m) and ("recipient" in m or "restrict" in m)


def smtp2go_disable_recipient_restriction(api_key: str) -> dict:
    """
    Turn OFF SMTP2GO Settings → Sending Options → Restrict Recipients.
    Preserves any existing allowlist entries; sets enabled=false via /allowed_recipients/update.
    """
    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "error": "API key required"}

    viewed, err = _smtp2go_request(key, "allowed_recipients/view", {})
    recipients = []
    if viewed and isinstance(viewed.get("data"), dict):
        recipients = list(viewed["data"].get("allowed_recipients") or [])
        if viewed["data"].get("enabled") is False:
            _SMTP2GO_RECIPIENT_RESTRICTION_OFF.add(key)
            return {
                "ok": True,
                "enabled": False,
                "allowed_recipients": recipients,
                "msg": "Recipient restriction already disabled",
            }
    elif err and "permission" in err.lower():
        return {
            "ok": False,
            "error": "permission",
            "hint": (
                "This API key cannot manage Allowed Recipients. "
                "Fix in app.smtp2go.com: (1) Settings → Sending Options → Restrictions → turn OFF Restrict Recipients, "
                "OR (2) Sending → API Keys → open this key → Permissions → enable Allowed Recipients (/allowed_recipients/*) → Save, then re-add the key here."
            ),
        }

    data, err2 = _smtp2go_request(
        key,
        "allowed_recipients/update",
        {"allowed_recipients": recipients, "enabled": False},
    )
    if err2:
        if "permission" in err2.lower():
            return {
                "ok": False,
                "error": "permission",
                "hint": (
                    "This API key cannot manage Allowed Recipients. "
                    "Fix in app.smtp2go.com: (1) Settings → Sending Options → Restrictions → turn OFF Restrict Recipients, "
                    "OR (2) Sending → API Keys → open this key → Permissions → enable Allowed Recipients (/allowed_recipients/*) → Save."
                ),
            }
        return {"ok": False, "error": err2}
    nested = data.get("data") if isinstance((data or {}).get("data"), dict) else {}
    _SMTP2GO_RECIPIENT_RESTRICTION_OFF.add(key)
    return {
        "ok": True,
        "enabled": bool(nested.get("enabled")) if nested else False,
        "allowed_recipients": nested.get("allowed_recipients", recipients) if nested else recipients,
        "msg": "Recipient restriction disabled — you can send to any address",
    }


def smtp2go_allow_recipients(api_key: str, recipients: list) -> dict:
    """
    Add email addresses and/or domains to SMTP2GO Allowed Recipients list.
    Domains may be passed as 'gmail.com' or '@gmail.com'.
    Only needs /allowed_recipients/add permission on the API key.
    """
    key = (api_key or "").strip()
    items = []
    seen = set()
    for r in recipients or []:
        s = str(r or "").strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        items.append(s)
    if not key:
        return {"ok": False, "error": "API key required"}
    if not items:
        return {"ok": False, "error": "No recipients to add"}

    data, err = _smtp2go_request(
        key,
        "allowed_recipients/add",
        {"allowed_recipients": items},
    )
    if err:
        if "permission" in err.lower():
            return {
                "ok": False,
                "error": "permission",
                "hint": (
                    "Enable `/allowed_recipients/add` on this API key "
                    "(Sending → API Keys → Permissions), or turn OFF Restrict Recipients."
                ),
            }
        return {"ok": False, "error": err}
    nested = data.get("data") if isinstance((data or {}).get("data"), dict) else {}
    return {
        "ok": True,
        "enabled": nested.get("enabled") if nested else None,
        "allowed_recipients": nested.get("allowed_recipients", items) if nested else items,
        "added": items,
        "msg": f"Added {len(items)} address(es)/domain(s) to allow list",
    }


def _smtp2go_heal_allowlist(api_key: str, recipient_email: str) -> tuple:
    """
    Heal Restrict Recipients blocks.
    Prefer adding this recipient email + domain (needs /allowed_recipients/add).
    Fallback: disable the restriction entirely (needs view/update).
    Returns (ok: bool, message: str).
    """
    key = (api_key or "").strip()
    email = (recipient_email or "").strip().lower()

    items = []
    if email and "@" in email:
        items.append(email)
        dom = email.split("@", 1)[1]
        if dom:
            items.append(dom)

    add_err = ""
    if items:
        add = smtp2go_allow_recipients(key, items)
        if add.get("ok"):
            return True, add.get("msg") or "recipient added to allow list"
        add_err = add.get("hint") or add.get("error") or "add failed"

    if key not in _SMTP2GO_RECIPIENT_RESTRICTION_OFF:
        dis = smtp2go_disable_recipient_restriction(key)
        if dis.get("ok") and dis.get("enabled") is False:
            return True, dis.get("msg") or "recipient restriction disabled"
        return False, add_err or dis.get("hint") or dis.get("error") or "failed"

    return False, add_err or "could not heal SMTP2GO recipient allow list"


def _send_smtp2go(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    """
    SMTP2GO v3 JSON API — POST https://api.smtp2go.com/v3/email/send
    Auth: X-Smtp2go-Api-Key header (and api_key in body for compatibility).
    Keys look like: api-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

    If Restrict Recipients blocks the send, we auto-disable the restriction
    (or add the recipient/domain) and retry once.
    """
    key        = (api_cfg.get("apiKey") or "").strip()
    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = lead.get("email", "")
    lead_name  = lead.get("name", "")

    sender_str = f"{from_name} <{from_email}>" if from_name else from_email
    to_str     = f"{lead_name} <{lead_email}>" if lead_name else lead_email

    payload = {
        "api_key":   key,
        "sender":    sender_str,
        "to":        [to_str],
        "subject":   subject,
        "html_body": html,
        "text_body": plain or subject,
    }
    hdrs = []
    if reply_to:
        hdrs.append({"header": "Reply-To", "value": reply_to})
    if extra_hdrs:
        for k, v in extra_hdrs.items():
            if k:
                hdrs.append({"header": str(k), "value": str(v)})
    if hdrs:
        payload["custom_headers"] = hdrs
    if atts:
        payload["attachments"] = [
            {
                "filename": a["filename"],
                "fileblob": a["content_b64"],
                "mimetype": a["content_type"].split(";", 1)[0].strip() or "application/octet-stream",
            }
            for a in atts
        ]

    # Default: ensure this recipient is allow-listed before send
    # (campaign preflight also bulk-adds; this covers test/one-off sends).
    if key and lead_email:
        try:
            items = [lead_email.lower()]
            if "@" in lead_email:
                items.append(lead_email.lower().split("@", 1)[1])
            add = smtp2go_allow_recipients(key, items)
            if add.get("ok"):
                log.info("[ApiSender] smtp2go pre-send allow: %s", add.get("msg"))
            elif key not in _SMTP2GO_RECIPIENT_RESTRICTION_OFF:
                dis = smtp2go_disable_recipient_restriction(key)
                if dis.get("ok"):
                    log.info("[ApiSender] smtp2go pre-send: %s", dis.get("msg") or "restriction disabled")
                else:
                    log.warning(
                        "[ApiSender] smtp2go pre-send allow failed: %s",
                        add.get("error") or dis.get("error"),
                    )
        except Exception as exc:
            log.warning("[ApiSender] smtp2go pre-send allow error: %s", exc)

    url = _API_URLS["smtp2go"]
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Smtp2go-Api-Key": key,
    }

    def _do_send():
        raw = json.dumps(payload).encode("utf-8")
        req = Request(url, data=raw, headers=headers, method="POST")
        try:
            resp = urlopen(req, timeout=15)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body) if body.strip() else {}
            except Exception:
                data = {}
            result = data.get("data") if isinstance(data.get("data"), dict) else {}
            failed = int(result.get("failed") or 0)
            if failed:
                failures = result.get("failures") or []
                detail = failures[0] if failures else (result.get("error") or "send failed")
                if isinstance(detail, dict):
                    detail = detail.get("error") or detail.get("message") or str(detail)
                raise Exception(f"API smtp2go — {detail}")
            return resp.status
        except HTTPError as exc:
            body = ""
            detail = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:600]
                err_data = json.loads(body)
                nested = err_data.get("data") if isinstance(err_data.get("data"), dict) else {}
                detail = (
                    nested.get("error")
                    or nested.get("error_code")
                    or err_data.get("error")
                    or err_data.get("message")
                    or body
                )
            except Exception:
                detail = body or str(exc)
            if exc.code == 401:
                raise Exception(
                    "API smtp2go 401 Unauthorized — invalid API key. "
                    "Check the key in SMTP2GO → Sending → API Keys."
                )
            if exc.code == 403:
                raise Exception(
                    f"API smtp2go 403 Forbidden — {detail}. "
                    "Check API key permissions and verified sender domain."
                )
            raise Exception(f"API smtp2go HTTP {exc.code} — {detail}")
        except URLError as exc:
            raise Exception(f"API smtp2go network error: {exc.reason}")

    try:
        return _do_send()
    except Exception as first_exc:
        msg = str(first_exc)
        if not _smtp2go_is_allowlist_error(msg):
            raise
        ok, heal_msg = _smtp2go_heal_allowlist(key, lead_email)
        if not ok:
            raise Exception(
                f"{msg} — auto-allow failed ({heal_msg}). "
                "Disable Restrict Recipients in SMTP2GO → Settings → Sending Options → Restrictions, "
                "or use the 'Disable recipient restriction' button under Method → API."
            )
        log.info("[ApiSender] smtp2go allow-list heal: %s — retrying send to %s", heal_msg, lead_email)
        try:
            return _do_send()
        except Exception as second_exc:
            raise Exception(
                f"{second_exc} — after allow-list heal ({heal_msg})"
            )


def _send_postmark(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    key        = api_cfg.get("apiKey", "")
    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = lead.get("email", "")
    lead_name  = lead.get("name", "")

    from_str = f"{from_name} <{from_email}>" if from_name else from_email
    to_str   = f"{lead_name} <{lead_email}>" if lead_name else lead_email

    payload = {
        "From":        from_str,
        "To":          to_str,
        "Subject":     subject,
        "HtmlBody":    html,
        "MessageStream": api_cfg.get("messageStream") or "outbound",
    }
    if plain:
        payload["TextBody"] = plain
    if reply_to:
        payload["ReplyTo"] = reply_to
    if extra_hdrs:
        payload["Headers"] = [{"Name": k, "Value": v} for k, v in extra_hdrs.items()]
    if atts:
        payload["Attachments"] = [
            {"Name": a["filename"], "Content": a["content_b64"], "ContentType": a["content_type"]}
            for a in atts
        ]

    return _api_request(
        _API_URLS["postmark"], payload,
        {
            "Accept":              "application/json",
            "Content-Type":        "application/json",
            "X-Postmark-Server-Token": key,
        },
        "postmark",
        uid=api_cfg.get("_uid"),
    )


def _send_sparkpost(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    key        = api_cfg.get("apiKey", "")
    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = lead.get("email", "")
    lead_name  = lead.get("name", "")

    from_str = {"email": from_email, "name": from_name} if from_name else {"email": from_email}
    to_obj   = {"address": {"email": lead_email, "name": lead_name}} if lead_name else {"address": {"email": lead_email}}

    content  = {
        "from":    from_str,
        "subject": subject,
        "html":    html,
    }
    if plain:
        content["text"] = plain
    if reply_to:
        content["reply_to"] = reply_to
    if extra_hdrs:
        content["headers"] = extra_hdrs
    if atts:
        content["attachments"] = [
            {"name": a["filename"], "type": a["content_type"], "data": a["content_b64"]}
            for a in atts
        ]

    payload = {
        "recipients": [to_obj],
        "content":    content,
    }

    # SparkPost EU endpoint
    region = api_cfg.get("sparkpostRegion", "us").lower()
    url    = "https://api.eu.sparkpost.com/api/v1/transmissions" if region == "eu" else _API_URLS["sparkpost"]

    return _api_request(
        url, payload,
        {"Authorization": key, "Content-Type": "application/json"},
        "sparkpost",
        uid=api_cfg.get("_uid"),
    )


def _send_ses(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    """
    Amazon SES v2 REST API (no boto3 required — uses raw HTTP with AWS Signature V4).
    Requires: apiKey = "ACCESS_KEY_ID:SECRET_ACCESS_KEY", region field.
    """
    import hmac, hashlib, datetime
    from urllib.parse import quote

    creds  = (api_cfg.get("apiKey", "") or "").strip()
    region = api_cfg.get("sesRegion") or api_cfg.get("region") or "us-east-1"

    if ":" in creds:
        access_key, secret_key = creds.split(":", 1)
    else:
        # Backward compatibility for config shapes that store secret separately.
        access_key = creds
        secret_key = (api_cfg.get("secret") or api_cfg.get("secretKey") or "").strip()
        if not access_key or not secret_key:
            raise Exception(
                "Amazon SES: apiKey must be 'ACCESS_KEY_ID:SECRET_ACCESS_KEY' format."
            )

    from_name  = sender.get("fromName", "")
    from_email = sender.get("fromEmail", "")
    reply_to   = sender.get("replyTo", "")
    lead_email = lead.get("email", "")
    lead_name  = lead.get("name", "")
    from_str   = f"{from_name} <{from_email}>" if from_name else from_email

    to_addr = f"{lead_name} <{lead_email}>" if lead_name else lead_email
    url     = f"https://email.{region}.amazonaws.com/v2/email/outbound-emails"

    if atts:
        # SES Simple content type has no attachment support — switch to Raw MIME
        from email.mime.multipart import MIMEMultipart as _MM
        from email.mime.text import MIMEText as _MT
        from email.mime.base import MIMEBase as _MB
        from email import encoders as _enc
        msg = _MM("mixed")
        msg["From"]    = from_str
        msg["To"]      = to_addr
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        alt = _MM("alternative")
        alt.attach(_MT(plain or "", "plain", "utf-8"))
        alt.attach(_MT(html,        "html",  "utf-8"))
        msg.attach(alt)
        for a in atts:
            ct = a["content_type"].split(";", 1)[0].strip()
            main_t, sub_t = (ct.split("/", 1) + ["octet-stream"])[:2]
            part = _MB(main_t, sub_t)
            part.set_payload(base64.b64decode(a["content_b64"]))
            _enc.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=a["filename"])
            msg.attach(part)
        payload = {
            "FromEmailAddress": from_str,
            "Destination": {"ToAddresses": [to_addr]},
            "Content": {"Raw": {"Data": base64.b64encode(msg.as_bytes()).decode()}},
        }
        if reply_to:
            payload["ReplyToAddresses"] = [reply_to]
    else:
        payload = {
            "FromEmailAddress": from_str,
            "Destination": {"ToAddresses": [to_addr]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": html,  "Charset": "UTF-8"},
                        "Text": {"Data": plain or "", "Charset": "UTF-8"},
                    },
                }
            },
        }
        if reply_to:
            payload["ReplyToAddresses"] = [reply_to]

    body = json.dumps(payload).encode("utf-8")

    # ── AWS Signature V4 ──────────────────────────────────
    now   = datetime.datetime.utcnow()
    date  = now.strftime("%Y%m%d")
    dtime = now.strftime("%Y%m%dT%H%M%SZ")

    def _sign(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    body_hash = hashlib.sha256(body).hexdigest()
    host      = f"email.{region}.amazonaws.com"

    canonical = (
        f"POST\n/v2/email/outbound-emails\n\n"
        f"content-type:application/json\n"
        f"host:{host}\n"
        f"x-amz-date:{dtime}\n\n"
        f"content-type;host;x-amz-date\n"
        f"{body_hash}"
    )
    str_to_sign = (
        f"AWS4-HMAC-SHA256\n{dtime}\n{date}/{region}/ses/aws4_request\n"
        + hashlib.sha256(canonical.encode()).hexdigest()
    )
    signing_key = _sign(
        _sign(_sign(_sign(f"AWS4{secret_key}".encode("utf-8"), date), region), "ses"),
        "aws4_request",
    )
    signature = hmac.new(signing_key, str_to_sign.encode(), hashlib.sha256).hexdigest()
    auth_hdr  = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{date}/{region}/ses/aws4_request, "
        f"SignedHeaders=content-type;host;x-amz-date, Signature={signature}"
    )

    req_hdrs = {
        "Content-Type":  "application/json",
        "X-Amz-Date":    dtime,
        "Authorization": auth_hdr,
        "Host":          host,
    }
    if extra_hdrs:
        # SES doesn't support arbitrary headers in v2 REST API; log and skip
        log.debug("[ApiSender] SES: extra headers not supported in v2 REST API — skipped")

    req = Request(url, data=body, headers=req_hdrs, method="POST")
    try:
        resp = urlopen(req, timeout=30)
        return resp.status
    except HTTPError as exc:
        body_str = exc.read().decode(errors="replace")[:400]
        try:
            detail = json.loads(body_str).get("message", body_str)
        except Exception:
            detail = body_str
        if exc.code == 403:
            raise Exception(f"Amazon SES 403 — {detail}. Check access key, secret, and IAM permissions.")
        if exc.code == 400:
            raise Exception(f"Amazon SES 400 — {detail}. Sender email must be verified in SES.")
        raise Exception(f"Amazon SES HTTP {exc.code} — {detail}")



def _send_mailjet(api_cfg, sender, lead, html, plain, subject, extra_hdrs, atts=None):
    """
    Mailjet v3.1 Send API — Basic auth (apiKey + secretKey), JSON Messages array.
    Endpoint: https://api.mailjet.com/v3.1/send

    Mailjet requires both a public API key and a secret key for Basic auth.
    apiKey = public key, secret = private key (api_cfg['secret'] / ['secretKey']).
    """
    from urllib.request import Request

    pub  = api_cfg.get("apiKey", "") or ""
    sec  = (api_cfg.get("secret") or api_cfg.get("secretKey") or "").strip()
    if not pub or not sec:
        raise Exception(
            "Mailjet: both API key and Secret key are required (Basic auth). "
            "Set API Key = public key and Secret Key = private key."
        )

    from_name  = sender.get("fromName", "") or ""
    from_email = sender.get("fromEmail", "") or ""
    reply_to   = sender.get("replyTo", "") or ""
    lead_email = (lead.get("email") or "").strip()
    lead_name  = (lead.get("name") or "").strip() or None

    message = {
        "From":     {"Email": from_email, "Name": from_name or None},
        "To":       [{"Email": lead_email, "Name": lead_name}],
        "Subject":  subject,
        "HTMLPart": html,
        "TextPart": plain or None,
    }
    if reply_to:
        message["ReplyTo"] = {"Email": reply_to}
    if extra_hdrs:
        # Mailjet rejects certain standard headers inside the "Headers"
        # collection (error send-0011) because they must use a dedicated
        # property.  Route the well-known ones to their dedicated Mailjet
        # headers and drop the rest that Mailjet reserves.
        _RESERVED = {
            "from","to","cc","bcc","subject","reply-to","date","message-id",
            "mime-version","content-type","content-transfer-encoding",
            "return-path","sender",
        }
        mj_hdrs = {}
        for _k, _v in extra_hdrs.items():
            _lk = (_k or "").strip().lower()
            if _lk in _RESERVED:
                continue  # dedicated property handles it (or it's set elsewhere)
            if _lk == "list-unsubscribe":
                # Mailjet dedicated header for unsub links
                mj_hdrs["Mj-List-Unsubscribe"] = _v
            elif _lk == "list-unsubscribe-post":
                mj_hdrs["Mj-OneClick-Unsubscribe"] = _v
            else:
                mj_hdrs[_k] = _v
        if mj_hdrs:
            message["Headers"] = mj_hdrs
    if atts:
        message["Attachments"] = [
            {
                "ContentType": a["content_type"],
                "Filename":    a["filename"],
                "Base64Content": a["content_b64"],
            }
            for a in atts
        ]

    payload = {"Messages": [message]}
    cred = base64.b64encode(f"{pub}:{sec}".encode()).decode()
    hdrs = {
        "Authorization": f"Basic {cred}",
        "Content-Type":  "application/json",
    }

    return _api_request(
        _API_URLS["mailjet"], payload, hdrs, "mailjet", uid=api_cfg.get("_uid")
    )


# ═══════════════════════════════════════════════════════════════
# MAIN SEND FUNCTION
# ═══════════════════════════════════════════════════════════════

def send_api(
    api_cfg:          dict,
    sender:           dict,
    lead:             dict,
    resolved_html:    str,
    resolved_subject: str,
    extra_headers:    Optional[dict] = None,
    resolved_plain:   str            = "",
    dlv:              Optional[dict] = None,
    custom_headers:   Optional[list] = None,
    uid                              = None,
    attachments:      Optional[dict] = None,
) -> int:
    """
    Send one email via an external API provider.

    Args:
        api_cfg:          Provider config — keys: provider, apiKey, + provider-specific
        sender:           Sender dict (fromEmail, fromName, replyTo)
        lead:             Lead dict (email, name)
        resolved_html:    Resolved HTML body
        resolved_subject: Resolved subject
        extra_headers:    Pre-built header dict (from build_api_headers) — optional.
                          If None and dlv is provided, headers are built automatically.
        resolved_plain:   Resolved plain text (optional, improves deliverability)
        dlv:              Deliverability config (used if extra_headers is None)
        custom_headers:   Custom header list (used if extra_headers is None)

    Returns: HTTP status code (200/201/202 = success)
    Raises:  Exception with actionable message on failure
    """
    provider = (api_cfg.get("provider") or "brevo").lower()
    # Normalize aliases — frontend saves ses-api, we need ses
    _aliases = {"ses-api": "ses", "aws": "ses", "aws-ses": "ses", "sendinblue": "brevo"}
    provider = _aliases.get(provider, provider)

    if provider not in SUPPORTED_PROVIDERS:
        raise Exception(
            f"Unknown API provider '{provider}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )

    if not api_cfg.get("apiKey"):
        raise Exception(f"API {provider}: no apiKey configured")

    # Stash uid into api_cfg so per-provider helpers (and the
    # _api_request retry loop) only honor THIS user's abort flag.
    # Without this, user A pressing Stop aborted user B's API sends.
    if uid is not None:
        api_cfg = {**api_cfg, "_uid": uid}

    # Normalise content — guarantee html, plain, subject are never empty strings
    resolved_html, resolved_plain, resolved_subject = _norm(
        resolved_html, resolved_plain, resolved_subject
    )

    # Build headers if not pre-supplied
    if extra_headers is None:
        extra_headers = build_api_headers(
            dlv            = dlv or {},
            lead           = lead,
            custom_headers = custom_headers or [],
            sender         = sender,
        )

    dispatch = {
        "brevo":     _send_brevo,
        "sendgrid":  _send_sendgrid,
        "resend":    _send_resend,
        "mailgun":   _send_mailgun,
        "postmark":  _send_postmark,
        "sparkpost": _send_sparkpost,
        "ses":       _send_ses,
        "mailjet":   _send_mailjet,
        "smtp2go":   _send_smtp2go,
    }

    fn = dispatch.get(provider)
    if fn is None:
        raise Exception(f"Provider '{provider}' has no send implementation.")

    atts = _resolve_attachments(attachments or {}, sender, lead, resolved_subject)
    return fn(api_cfg, sender, lead, resolved_html, resolved_plain, resolved_subject, extra_headers, atts)
