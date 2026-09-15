#!/usr/bin/env python3
"""Stdlib-only portfolio dashboard server for two KIS grid bots.

  python3 dashboard/multi/server_multi.py --port 8790

Serves:
  GET /                → portfolio.html
  GET /api/portfolio   → aggregated JSON (files + optional KIS read-only quotes)
  GET /api/charts      → snapshot index; ?refresh=1 rebuilds (read-only KIS)
  GET /charts/<png>    → 1m trade-snapshot PNG
  GET /health          → {"ok":true}

No live orders. Never prints secrets/tokens.
Does not replace the single-bot dashboard on port 8787.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from build_portfolio_status import build_portfolio_status  # noqa: E402
from trade_charts import (  # noqa: E402
    attach_charts,
    charts_dir,
    get_or_build_charts,
    safe_chart_name,
)

INDEX = HERE / "portfolio.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "GridBotPortfolio/1.0"

    def log_message(self, fmt: str, *args) -> None:
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

        if path in ("/", "/index.html", "/portfolio.html", "/portfolio"):
            if not INDEX.exists():
                self._send(404, b"portfolio.html missing", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return

        if path in ("/api/portfolio", "/api/status"):
            qs = parse_qs(parsed.query or "")
            skip = (qs.get("skip_kis") or [""])[0].lower() in ("1", "true", "yes")
            try:
                status = build_portfolio_status(try_kis=not skip)
                # Charts: last-known index only. 15s UI refresh must not hit KIS.
                attach_charts(status)
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

        if path == "/api/charts":
            qs = parse_qs(parsed.query or "")
            skip = (qs.get("skip_kis") or [""])[0].lower() in ("1", "true", "yes")
            refresh = (qs.get("refresh") or [""])[0].lower() in ("1", "true", "yes")
            try:
                index = get_or_build_charts(refresh=refresh, skip_kis=skip)
                body = json.dumps(index, ensure_ascii=False, default=str).encode("utf-8")
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

        if path.startswith("/charts/"):
            raw = path[len("/charts/") :]
            name = safe_chart_name(raw)
            if not name:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            png = charts_dir() / name
            if not png.is_file():
                self._send(404, b"chart missing", "text/plain; charset=utf-8")
                return
            self._send(200, png.read_bytes(), "image/png")
            return

        if path == "/health":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Grid bot portfolio dashboard")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (127.0.0.1 or 0.0.0.0)")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Grid bot portfolio dashboard listening on {url}", flush=True)
    print(f"  API: {url}api/portfolio", flush=True)
    print(f"  Charts: {url}api/charts  {url}charts/<symbol>_trades_1m.png", flush=True)
    print("  Read-only. Single-bot UI remains on dashboard.sh (8787).", flush=True)
    print("  Ctrl+C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
