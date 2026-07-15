#!/usr/bin/env python3
"""
BQE Core Archive Builder — local server
Run: python3 server.py
Then open http://127.0.0.1:8080 (opens automatically)
"""
import webbrowser
from http.server import HTTPServer
from socketserver import ThreadingMixIn

import auth
from handler import Handler


class _Server(ThreadingMixIn, HTTPServer):
    """Multi-threaded so the SSE fetch stream doesn't block other UI requests."""
    daemon_threads = True


def main():
    auth.load_tokens()
    # Bind to 127.0.0.1 (not "localhost") so the address matches the OAuth
    # redirect URI registered with BQE exactly — see auth.REDIRECT_URI.
    server = _Server(("127.0.0.1", auth.PORT), Handler)
    url = f"http://127.0.0.1:{auth.PORT}"
    print(f"BQE Archive Builder → {url}")
    print("Press Ctrl+C to stop.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
