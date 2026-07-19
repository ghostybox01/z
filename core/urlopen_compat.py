"""Drop-in replacement imports for urllib.request.

Usage:
    from core.urlopen_compat import urlopen, Request

`urlopen` is the safe wrapper from core.safe_urlopen; `Request` is the standard
urllib.request.Request class.
"""
__all__ = ["Request", "urlopen"]
from urllib.request import Request
from core.safe_urlopen import urlopen
