"""
SynthTel Sender — desktop wrapper.

Boots server.py in a background thread and opens a native window
(WKWebView on macOS, Edge WebView2 on Windows) pointed at the local
HTTP server. The bundled .app/.exe registers as a real GUI app
instead of a headless process, so macOS doesn't flag it as
"not responding" and Windows shows a proper window.
"""
import os
import socket
import sys
import threading
import time

os.environ.setdefault("SYNTHTEL_OPEN_BROWSER", "0")

import server  # noqa: E402

PORT = int(os.environ.get("SYNTHTEL_PORT", "5001"))


def _start_server():
    sys.argv = ["server.py", str(PORT)]
    try:
        server.main()
    except SystemExit:
        pass


def _wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    threading.Thread(target=_start_server, daemon=True).start()

    if not _wait_for_server("127.0.0.1", PORT):
        print(f"SynthTel server didn't start on port {PORT} in 20s", file=sys.stderr)
        sys.exit(1)

    import webview

    webview.create_window(
        "SynthTel Sender",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=860,
        resizable=True,
    )
    webview.start()


if __name__ == "__main__":
    main()
