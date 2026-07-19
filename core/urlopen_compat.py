"""Drop-in replacement imports for urllib.request.

Usage:
    from core.urlopen_compat import urlopen, Request

`urlopen` is the safe wrapper from core.safe_urlopen; `Request` is the standard
urllib.request.Request class.
"""
