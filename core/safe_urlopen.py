"""
core/safe_urlopen.py — drop-in replacement for urllib.request.urlopen

Blocks the most common SSRF vectors by validating the URL scheme and
the resolved hostname before opening the connection.
"""

import ipaddress
import logging
import socket
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger(__name__)


PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_private_host(hostname: str) -> bool:
    """Return True if hostname resolves to a private/internal IP."""
    if not hostname:
        return False
    # Fast path for literal loopback names
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    seen = set()
    for info in infos:
        ip_str = info[4][0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
        for net in PRIVATE_NETWORKS:
            if ip in net:
                return True
    return False


def _is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if _is_private_host(hostname):
        return False
    return True


def urlopen(
    url,
    *args,
    allow_private: bool = False,
    **kwargs,
):
    """Safe wrapper around urllib.request.urlopen.

    - Only ``http`` and ``https`` schemes are allowed.
    - Private/loopback/internal hosts are blocked unless
      ``allow_private=True`` is passed.
    """
    if isinstance(url, str):
        if not _is_safe_url(url):
            raise ValueError(f"Refusing to fetch unsafe URL: {url}")
    elif isinstance(url, urllib.request.Request):
        full_url = url.get_full_url()
        if not _is_safe_url(full_url):
            raise ValueError(f"Refusing to fetch unsafe URL: {full_url}")
        if not allow_private and full_url.startswith("file://"):
            raise ValueError("Refusing to fetch file:// URL")
    else:
        raise TypeError("url must be a string or urllib.request.Request")

    return urllib.request.urlopen(url, *args, **kwargs)  # nosec B310
