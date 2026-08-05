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
    try:
        from core.ssh_helper import create_ssh_client
    except ImportError:
        try:
            from ssh_helper import create_ssh_client
        except ImportError:
            import paramiko

            def create_ssh_client():
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                return c

    ssh = create_ssh_client()
    ssh.connect(
        relay_ssh["host"],
        port=int(relay_ssh.get("port", 22)),
        username=relay_ssh.get("user", "root"),
        password=relay_ssh.get("pass") or None,
        key_filename=relay_ssh.get("key") or None,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
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
    relay: dict,
    msg_from: str,
    msg_to: str,
    raw_msg: bytes,
    relay_ssh: dict = None,
    timeout: int = 30,
) -> dict:
    """
    Send a single message via O365 anonymous relay / inbound connector.

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
    tenant = relay.get("tenantDomain", "")
    mx_host = relay.get("mxHost") or _derive_mx(tenant)
    port = int(relay.get("port", 25))
    mail_from = (relay.get("fromEmail") or msg_from or "").strip()
    msg_to = (msg_to or "").strip()

    if not mx_host:
        return {"ok": False, "error": "No tenant domain or mx_host configured"}
    if not mail_from or "@" not in mail_from:
        return {"ok": False, "error": "From email (MAIL FROM) is required and must be in your M365 accepted domain"}
    if not msg_to or "@" not in msg_to:
        return {"ok": False, "error": "Recipient email is required"}

    # Normalize to CRLF — email.as_bytes() (compat32) emits bare \n which causes
    # O365 to produce an empty body.
    raw_msg = (
        raw_msg.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    )
    if not raw_msg.endswith(b"\r\n"):
        raw_msg += b"\r\n"

    # Strip any client-forged Exchange organization headers (expert-mode toggles
    # or older builds). Exchange Online stamps these itself after connector auth;
    # forging them can cause silent post-accept drops.
    _hdr_end = raw_msg.find(b"\r\n\r\n")
    if _hdr_end > 0:
        _hdrs = raw_msg[:_hdr_end].split(b"\r\n")
        _body = raw_msg[_hdr_end:]
        _kept = [
            h
            for h in _hdrs
            if not h.lower().startswith(b"x-ms-exchange-organization-")
        ]
        raw_msg = b"\r\n".join(_kept) + _body

    # Do NOT forge X-MS-Exchange-Organization-* headers here.
    # Exchange Online stamps those after the inbound connector authenticates the
    # connecting IP. Client-injected org headers are stripped or can make EOP
    # treat the message as tampered / drop it after a 250 accept.

    ehlo_domain = mail_from.split("@")[-1] if "@" in mail_from else "mail.local"
    t0 = time.time()
    ssh_client = None
    smtp_detail = ""

    try:
        if relay_ssh and relay_ssh.get("host"):
            # ── Route through external relay VPS via SSH TCP-forwarding ──
            ssh_client, channel = _open_ssh_channel(
                relay_ssh, mx_host, port, timeout=20
            )
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
            code, resp = conn.ehlo(ehlo_domain)
            smtp_detail = f"EHLO {code}"
            try:
                if conn.has_extn("STARTTLS"):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    conn.starttls(context=ctx)
                    code, resp = conn.ehlo(ehlo_domain)
                    smtp_detail += f"; STARTTLS+EHLO {code}"
            except Exception as tls_err:
                log.debug("STARTTLS optional — skipping: %s", tls_err)
                smtp_detail += f"; STARTTLS skipped ({tls_err})"

            # sendmail returns {} if all recipients accepted; non-empty = partial refuse
            refused = conn.sendmail(mail_from, [msg_to], raw_msg)
            if refused:
                detail = "; ".join(
                    f"{addr}: {err}" for addr, err in refused.items()
                )
                return {
                    "ok": False,
                    "error": f"Recipient refused by M365: {detail[:300]}",
                    "via": via_label,
                }

        latency = round((time.time() - t0) * 1000)
        log.info(
            "O365 relay OK  %s → %s via %s (%dms) %s",
            mail_from,
            msg_to,
            via_label,
            latency,
            smtp_detail,
        )
        return {
            "ok": True,
            "message": (
                f"M365 accepted {mail_from} → {msg_to} via {via_label} ({latency}ms). "
                f"If it is not in Inbox/Junk, check Microsoft 365 Defender → Quarantine "
                f"and Exchange Message Trace for this recipient."
            ),
            "via": via_label,
            "latency_ms": latency,
            "mail_from": mail_from,
            "mail_to": msg_to,
        }

    except smtplib.SMTPRecipientsRefused as e:
        err = str(e)
        if "550" in err and ("5.7" in err or "5.7.64" in err or "relay" in err.lower()):
            return {
                "ok": False,
                "error": (
                    f"Relay denied — inbound connector IP mismatch or recipient not allowed: "
                    f"{err[:220]}"
                ),
            }
        return {"ok": False, "error": f"Recipient refused: {err[:200]}"}
    except smtplib.SMTPSenderRefused as e:
        return {
            "ok": False,
            "error": (
                f"Sender refused — MAIL FROM must be an accepted domain in your tenant "
                f"(e.g. joseph@orazama.com): {str(e)[:200]}"
            ),
        }
    except smtplib.SMTPDataError as e:
        return {
            "ok": False,
            "error": f"M365 rejected message DATA: {str(e)[:250]}",
        }
    except smtplib.SMTPException as e:
        return {"ok": False, "error": f"SMTP error: {str(e)[:200]}"}
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return {
            "ok": False,
            "error": f"Connection failed to {mx_host}:{port}: {str(e)[:200]}",
        }
    except Exception as e:
        return {"ok": False, "error": f"Unexpected error: {str(e)[:200]}"}
    finally:
        if ssh_client:
            try:
                ssh_client.close()
            except Exception:
                pass


def probe_relay_ssh(relay_ssh: dict, timeout: int = 20) -> dict:
    """
    SSH into relay_ssh (Linux VPS or Windows OpenSSH), discover its public IP
    and check outbound port 25.

    Returns {"ok": bool, "publicIp": str, "port25": bool, "latency_ms": int,
             "error": str, "os": "windows"|"linux"|""}
    """
    try:
        from core.ssh_helper import create_ssh_client
    except ImportError:
        try:
            from ssh_helper import create_ssh_client
        except ImportError:
            import paramiko

            def create_ssh_client():
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                return c

    host = (relay_ssh.get("host") or "").strip()
    user = (relay_ssh.get("user") or "root").strip()
    port = int(relay_ssh.get("port") or 22)
    password = relay_ssh.get("pass") or None
    key = relay_ssh.get("key") or None

    if not host:
        return {"ok": False, "error": "SSH host is required", "publicIp": "", "port25": False}
    if not password and not key:
        return {
            "ok": False,
            "error": "SSH password is required",
            "publicIp": "",
            "port25": False,
        }

    def _run(ssh, cmd, cmd_timeout=10):
        _, stdout, stderr = ssh.exec_command(cmd, timeout=cmd_timeout)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        return out, err

    def _looks_like_ip(s: str) -> bool:
        if not s or len(s) > 45 or " " in s or "\n" in s:
            return False
        # IPv4
        parts = s.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return True
        # crude IPv6
        return ":" in s and all(c in "0123456789abcdefABCDEF:" for c in s)

    t0 = time.time()
    ssh = create_ssh_client()
    try:
        try:
            ssh.connect(
                host,
                port=port,
                username=user,
                password=password,
                key_filename=key,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as e:
            err = str(e).strip() or e.__class__.__name__
            low = err.lower()
            hint = ""
            if "authentication" in low or "auth" in low:
                hint = (
                    " Check username/password. On Windows OpenSSH, Administrator "
                    "password login often needs PasswordAuthentication enabled "
                    "(re-run the Copy PowerShell script)."
                )
            elif "timed out" in low or "timeout" in low:
                hint = (
                    f" Cannot reach {host}:{port}. Confirm sshd is Running, "
                    "Windows firewall allows 22, and the cloud/security group allows 22."
                )
            elif "refused" in low:
                hint = (
                    f" Nothing listening on {host}:{port}. Start OpenSSH "
                    "(Start-Service sshd) or check the port."
                )
            elif "no route" in low or "unreachable" in low:
                hint = f" Network unreachable to {host}."
            return {
                "ok": False,
                "error": (err + hint)[:400],
                "publicIp": "",
                "port25": False,
                "os": "",
            }

        # Detect OS (uname works on Linux; PowerShell marker on Windows)
        os_kind = "linux"
        uname_out, _ = _run(ssh, "uname -s 2>/dev/null", 5)
        ulow = uname_out.lower()
        if "linux" in ulow or "darwin" in ulow:
            os_kind = "linux"
        else:
            ps_mark, _ = _run(
                ssh, 'powershell -NoProfile -Command "Write-Output WINDOWS"', 8
            )
            if "WINDOWS" in ps_mark or not uname_out:
                os_kind = "windows"

        # Public IP — Linux then Windows commands
        public_ip = ""
        linux_ip_cmds = [
            "curl -4 -s --max-time 5 ifconfig.me 2>/dev/null",
            "curl -4 -s --max-time 5 icanhazip.com 2>/dev/null",
            "curl -4 -s --max-time 5 api.ipify.org 2>/dev/null",
            "wget -qO- --timeout=5 ifconfig.me 2>/dev/null",
        ]
        win_ip_cmds = [
            'powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; (Invoke-RestMethod -Uri https://api.ipify.org -TimeoutSec 8)"',
            'powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; (Invoke-WebRequest -Uri https://ifconfig.me/ip -UseBasicParsing -TimeoutSec 8).Content.Trim()"',
            'powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; (Invoke-RestMethod -Uri https://icanhazip.com -TimeoutSec 8).Trim()"',
        ]
        cmds = win_ip_cmds + linux_ip_cmds if os_kind == "windows" else linux_ip_cmds + win_ip_cmds
        for cmd in cmds:
            try:
                out, _ = _run(ssh, cmd, 12)
                # Take first line only
                candidate = (out.splitlines()[0] if out else "").strip().strip('"').strip("'")
                if _looks_like_ip(candidate):
                    public_ip = candidate
                    break
            except Exception:
                continue

        # Port 25 outbound check
        port25 = False
        try:
            if os_kind == "windows":
                p25_cmd = (
                    'powershell -NoProfile -Command '
                    '"try { $c = New-Object Net.Sockets.TcpClient; '
                    '$c.ReceiveTimeout=4000; $c.SendTimeout=4000; '
                    "$c.Connect('gmail-smtp-in.l.google.com',25); $c.Close(); 'ok' } "
                    "catch { 'fail' }\""
                )
            else:
                p25_cmd = (
                    "timeout 5 bash -c 'echo QUIT | nc -w3 gmail-smtp-in.l.google.com 25 "
                    "2>/dev/null' && echo ok || "
                    "timeout 5 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25 "
                    "&& echo ok || echo fail' 2>/dev/null || echo fail"
                )
            p25out, _ = _run(ssh, p25_cmd, 12)
            port25 = "ok" in p25out.lower() or "220" in p25out
        except Exception:
            pass

        latency = round((time.time() - t0) * 1000)
        if not public_ip:
            return {
                "ok": False,
                "error": (
                    f"SSH login to {user}@{host}:{port} worked ({os_kind}), "
                    "but could not detect the relay public IP "
                    "(curl/Invoke-WebRequest failed). Check outbound HTTPS on the relay."
                ),
                "publicIp": "",
                "port25": port25,
                "latency_ms": latency,
                "os": os_kind,
            }

        return {
            "ok": True,
            "publicIp": public_ip,
            "port25": port25,
            "latency_ms": latency,
            "os": os_kind,
        }
    except Exception as e:
        err = str(e).strip() or e.__class__.__name__
        return {"ok": False, "error": err[:300], "publicIp": "", "port25": False, "os": ""}
    finally:
        try:
            ssh.close()
        except Exception:
            pass


def test_relay_connectivity(
    tenant_domain: str, port: int = 25, timeout: int = 10
) -> dict:
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
