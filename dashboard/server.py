#!/usr/bin/env python3
"""Stdlib-only local dashboard server for the grid bot.

  python3 /workspace/grid-bot/dashboard/server.py --port 8787

Serves:
  GET /           → index.html
  GET /api/status → aggregated JSON (files + optional KIS read-only quote)

No live orders. Never prints secrets/tokens.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from build_status import build_status  # noqa: E402

INDEX = HERE / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "GridBotDashboard/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # Quiet, no secrets — path only
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path or "/"

        if path in ("/", "/index.html"):
            if not INDEX.exists():
                self._send(404, b"index.html missing", "text/plain; charset=utf-8")
                return
            body = INDEX.read_bytes()
            self._send(200, body, "text/html; charset=utf-8")
            return

        if path == "/api/status":
            qs = parse_qs(parsed.query or "")
            skip = (qs.get("skip_kis") or [""])[0].lower() in ("1", "true", "yes")
            try:
                status = build_status(try_kis=not skip)
                body = json.dumps(status, ensure_ascii=False, default=str).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                err = {
                    "error": type(e).__name__,
                    "message": str(e)[:200],
                    "traceback": traceback.format_exc()[-800:],
                }
                body = json.dumps(err, ensure_ascii=False).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8")
            return

        if path == "/health":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Grid bot local dashboard")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (127.0.0.1 or 0.0.0.0)")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Grid bot dashboard listening on {url}", flush=True)
    print(f"  API: {url}api/status", flush=True)
    print("  Ctrl+C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
