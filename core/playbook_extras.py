"""
core/playbook_extras.py — Optional Playbook Extras (OFF by default)
===================================================================
• Automation hooks — Zapier/Make/n8n webhooks on campaign start / fail / done
• Analytics beacons — GA4 / Segment / Mixpanel / custom open pixels (HTML only)
• finalize_outbound_html() — last-pass HTML polish before send
• Link helpers for #LINK1/#LINK2/#LINK3/#REDIRECT/#PIXEL_URL (used by tags.py)

These are independent of Redirects / Telegram click tracking (use_tracking_links).
Analytics beacons are useless when convert_html_to_image / HTML→Image is on —
prefer SynthTel Redirects open pixels in that case.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

_DEFAULTS = {
    "useAutomationHooks": False,
    "automationOnStart": "",
    "automationOnFail": "",
    "automationOnDone": "",
    "useAnalyticsBeacon": False,
    "analyticsProvider": "custom",  # ga4 | segment | mixpanel | custom
    "analyticsId": "",
    "analyticsPixelUrl": "",      # custom img URL
    "pixelTrackingUrl": "",       # resolves #PIXEL_URL
    "browserPoolSize": 3,
    "pdfPoolSize": 2,
}


def normalize_extras(raw: Optional[dict]) -> dict:
    """Merge user payload into defaults. All feature flags default OFF."""
    out = dict(_DEFAULTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out:
                out[k] = v
    # Coerce
    out["useAutomationHooks"] = bool(out.get("useAutomationHooks"))
    out["useAnalyticsBeacon"] = bool(out.get("useAnalyticsBeacon"))
    try:
        out["browserPoolSize"] = max(1, min(16, int(out.get("browserPoolSize") or 3)))
    except (TypeError, ValueError):
        out["browserPoolSize"] = 3
    try:
        out["pdfPoolSize"] = max(1, min(8, int(out.get("pdfPoolSize") or 2)))
    except (TypeError, ValueError):
        out["pdfPoolSize"] = 2
    for k in (
        "automationOnStart", "automationOnFail", "automationOnDone",
        "analyticsId", "analyticsPixelUrl", "pixelTrackingUrl",
    ):
        out[k] = str(out.get(k) or "").strip()
    prov = str(out.get("analyticsProvider") or "custom").lower().strip()
    if prov in ("none", ""):
        prov = "custom"
    if prov not in ("ga4", "segment", "mixpanel", "custom"):
        prov = "custom"
    out["analyticsProvider"] = prov
    return out


def extras_from_campaign_data(data: dict) -> dict:
    """Pull playbook extras from a campaign /api/send payload."""
    raw = data.get("playbookExtras") or data.get("playbook") or {}
    if not isinstance(raw, dict):
        raw = {}
    # Also accept flat top-level keys for convenience
    flat_map = {
        "useAutomationHooks": "useAutomationHooks",
        "use_automation_hooks": "useAutomationHooks",
        "useAnalyticsBeacon": "useAnalyticsBeacon",
        "use_analytics_beacon": "useAnalyticsBeacon",
        "automationOnStart": "automationOnStart",
        "automationOnFail": "automationOnFail",
        "automationOnDone": "automationOnDone",
        "analyticsProvider": "analyticsProvider",
        "analyticsId": "analyticsId",
        "analyticsPixelUrl": "analyticsPixelUrl",
        "pixelTrackingUrl": "pixelTrackingUrl",
        "browserPoolSize": "browserPoolSize",
        "browser_pool_size": "browserPoolSize",
        "pdfPoolSize": "pdfPoolSize",
        "pdf_pool_size": "pdfPoolSize",
    }
    merged = dict(raw)
    for src, dst in flat_map.items():
        if src in data and data[src] not in (None, ""):
            merged[dst] = data[src]
    return normalize_extras(merged)


# ── Automation webhooks ───────────────────────────────────────

def _post_json(url: str, payload: dict, timeout: float = 8.0) -> tuple:
    if not url or not url.startswith(("http://", "https://")):
        return False, "invalid webhook URL"
    try:
        raw = json.dumps(payload, default=str).encode("utf-8")
        req = Request(
            url,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "SynthTel-Playbook/1.0",
            },
            method="POST",
        )
        with urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {getattr(resp, 'status', 200)}"
    except HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        return False, f"network: {exc.reason}"
    except Exception as exc:
        return False, str(exc)[:200]


def fire_automation_hook(
    extras: dict,
    event: str,
    payload: Optional[dict] = None,
    async_: bool = True,
) -> None:
    """
    POST JSON to the configured Zapier/Make/n8n URL for event:
      start | fail | done
    No-op when useAutomationHooks is off or URL empty.
    """
    extras = normalize_extras(extras)
    if not extras.get("useAutomationHooks"):
        return
    url_key = {
        "start": "automationOnStart",
        "fail": "automationOnFail",
        "done": "automationOnDone",
        "error": "automationOnFail",
        "complete": "automationOnDone",
    }.get((event or "").lower())
    if not url_key:
        return
    url = extras.get(url_key) or ""
    if not url:
        return
    body = {
        "source": "synthtel",
        "event": event,
        **(payload or {}),
    }

    def _run():
        ok, msg = _post_json(url, body)
        if ok:
            log.info("[playbook] automation %s → %s", event, msg)
        else:
            log.warning("[playbook] automation %s failed: %s", event, msg)

    if async_:
        threading.Thread(target=_run, name=f"pb-hook-{event}", daemon=True).start()
    else:
        _run()


# ── Analytics beacons ─────────────────────────────────────────

def build_analytics_beacon_html(extras: dict, lead_email: str = "") -> str:
    """
    Return an HTML snippet (img/script) to inject before </body>.
    Empty string when disabled or misconfigured.
    """
    extras = normalize_extras(extras)
    if not extras.get("useAnalyticsBeacon"):
        return ""
    prov = extras.get("analyticsProvider") or "ga4"
    aid = extras.get("analyticsId") or ""
    custom = extras.get("analyticsPixelUrl") or ""
    email_q = (lead_email or "").replace('"', "").replace("<", "")

    if prov == "custom" or custom:
        url = custom or extras.get("pixelTrackingUrl") or ""
        if not url:
            return ""
        # Optional simple substitution
        url = url.replace("{email}", email_q).replace("#EMAIL", email_q)
        return (
            f'<img src="{url}" width="1" height="1" alt="" '
            f'style="display:none!important;width:1px;height:1px;border:0" />'
        )

    if not aid:
        return ""

    if prov == "ga4":
        # Measurement Protocol collect pixel (open approx — not a full GA4 client)
        # Users typically paste a GA4 measurement ID; we also support a full collect URL in analyticsId.
        if aid.startswith("http"):
            src = aid
        else:
            # GA4 measurement IDs need a collect endpoint with tid — keep as transparent pixel
            # pointing at google-analytics collect with a custom cid hash of email when possible.
            import hashlib
            cid = hashlib.md5((email_q or "anon").encode()).hexdigest()
            mid = aid if aid.startswith("G-") else aid
            src = (
                f"https://www.google-analytics.com/collect?v=2&tid={mid}"
                f"&cid={cid}&en=email_open&ep.method=synthtel"
            )
        return (
            f'<img src="{src}" width="1" height="1" alt="" '
            f'style="display:none!important;width:1px;height:1px;border:0" />'
        )

    if prov == "segment":
        # Segment has no public open-pixel; use a 1x1 against api.segment.io if write key provided
        # Prefer custom pixel URL for Segment — fall back to a noscript placeholder comment.
        return (
            f'<!-- synthtel-segment writeKey={aid} email={email_q} -->'
            f'<img src="https://api.segment.io/v1/pixel/track?data=" width="1" height="1" alt="" '
            f'style="display:none!important" />'
        )

    if prov == "mixpanel":
        import base64
        try:
            data = base64.b64encode(json.dumps({
                "event": "Email Open",
                "properties": {
                    "token": aid,
                    "distinct_id": email_q or "unknown",
                    "source": "synthtel",
                },
            }).encode()).decode()
            src = f"https://api.mixpanel.com/track/?data={data}&img=1"
        except Exception:
            return ""
        return (
            f'<img src="{src}" width="1" height="1" alt="" '
            f'style="display:none!important;width:1px;height:1px;border:0" />'
        )

    return ""


def finalize_outbound_html(
    html: str,
    extras: Optional[dict] = None,
    lead_email: str = "",
    skip_analytics: bool = False,
) -> str:
    """
    Last-pass HTML polish before MIME build / API send:
      • inject analytics beacon (unless skip_analytics / HTML→image mode)
      • ensure html/body wrappers when fragment-only
    """
    if not html:
        return html or ""
    extras = normalize_extras(extras or {})
    out = html

    # Light normalize: if no <html>, wrap
    low = out.lower()
    if "<html" not in low and "<body" not in low:
        out = f"<html><body>{out}</body></html>"
        low = out.lower()

    if not skip_analytics and extras.get("useAnalyticsBeacon"):
        beacon = build_analytics_beacon_html(extras, lead_email)
        if beacon:
            if "</body>" in low:
                # Case-preserving insert before last </body>
                idx = out.lower().rfind("</body>")
                out = out[:idx] + beacon + out[idx:]
            else:
                out = out + beacon

    return out


def link_at(links_cfg: Optional[dict], index: int) -> str:
    """1-based index into link rotation list (#LINK1 → index 1)."""
    links = (links_cfg or {}).get("links") or []
    valid = [l.get("url") for l in links if isinstance(l, dict) and l.get("url")]
    if not valid:
        return ""
    i = max(1, int(index or 1)) - 1
    if i >= len(valid):
        return valid[-1]
    return valid[i]


def pixel_url_from_extras(extras: Optional[dict]) -> str:
    extras = normalize_extras(extras or {})
    return extras.get("pixelTrackingUrl") or extras.get("analyticsPixelUrl") or ""
