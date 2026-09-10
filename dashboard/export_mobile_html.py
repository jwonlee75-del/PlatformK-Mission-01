#!/usr/bin/env python3
"""Build a self-contained mobile dashboard HTML and optionally send via Telegram."""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from build_status import build_status  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
CHAT_ID = "840503590"
OUT_DIR = ROOT / "logs"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or ""
    if t:
        return t
    try:
        secrets = json.loads(
            Path("/home/box/agent-data/box-secrets.json").read_text(encoding="utf-8")
        ).get("secrets") or {}
        return str(secrets.get("TELEGRAM_BOT_TOKEN") or "")
    except Exception:
        return ""


def build_mobile_html(status: dict | None = None, *, try_kis: bool = True) -> tuple[str, Path]:
    if status is None:
        status = build_status(try_kis=try_kis)
    status = dict(status)
    status["_offline_snapshot"] = True

    index = (HERE / "index.html").read_text(encoding="utf-8")
    boot = json.dumps(status, ensure_ascii=False, default=str)
    inject = f"""
<script>
window.__DASHBOARD_BOOTSTRAP__ = {boot};
(function(){{
  const boot = window.__DASHBOARD_BOOTSTRAP__;
  if (!boot) return;
  const origFetch = window.fetch.bind(window);
  window.fetch = function(url, opts){{
    try {{
      const u = String(url);
      if (u.includes('/api/status')) {{
        // file:// or failed network → serve embedded snapshot
        if (location.protocol === 'file:' || !navigator.onLine) {{
          return Promise.resolve(new Response(JSON.stringify(boot), {{
            headers: {{'Content-Type': 'application/json'}}
          }}));
        }}
        return origFetch(url, opts).catch(function(){{
          return new Response(JSON.stringify(boot), {{
            headers: {{'Content-Type': 'application/json'}}
          }});
        }});
      }}
    }} catch (e) {{}}
    return origFetch(url, opts);
  }};
}})();
</script>
"""
    # Prefer injecting before the main script so bootstrap is ready
    if "<script>\nconst REFRESH_MS" in index:
        html = index.replace("<script>\nconst REFRESH_MS", inject + "\n<script>\nconst REFRESH_MS", 1)
    elif "</head>" in index:
        html = index.replace("</head>", inject + "\n</head>", 1)
    else:
        html = inject + index

    # Soften meta refresh for offline file (still useful)
    html = html.replace(
        '<meta http-equiv="refresh" content="120" />',
        '<!-- offline snapshot: no auto page reload -->',
        1,
    )

    now = datetime.now(SEOUL)
    fname = f"GridBot-367380-{now.strftime('%Y%m%d-%H%M')}.html"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / fname
    path.write_text(html, encoding="utf-8")
    return html, path


def send_document(path: Path, caption: str) -> dict:
    token = _token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    boundary = "----GridBotBoundary7MA4YWxkTrZu0gW"
    data = path.read_bytes()
    filename = path.name
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    field("chat_id", CHAT_ID)
    field("caption", caption[:1024])
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: text/html; charset=utf-8\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(data)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def send_text(text: str) -> dict:
    token = _token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    payload = json.dumps({"chat_id": CHAT_ID, "text": text[:3500], "disable_web_page_preview": False}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def export_and_send(*, try_kis: bool = True, tunnel_url: str | None = None) -> dict:
    html, path = build_mobile_html(try_kis=try_kis)
    caption = (
        "📱 GridBot 367380 대시보드 (오프라인 HTML)\n"
        "iPad: 파일을 탭 → Safari/Files에서 열기\n"
        "홈 화면 추가: Safari 공유 → 홈 화면에 추가"
    )
    doc = send_document(path, caption)
    doc_mid = (doc.get("result") or {}).get("message_id")

    lines = ["📡 GridBot iPad 대시보드"]
    if tunnel_url:
        lines += [
            "",
            f"라이브 URL: {tunnel_url}",
            "→ Safari에서 열고 공유 → 홈 화면에 추가(북마크)",
        ]
    else:
        lines += ["", "라이브 터널: 현재 없음 (HTML 스냅샷만 전송)"]
    lines += [
        "",
        f"첨부 HTML: {path.name}",
        "오프라인으로도 PnL/주문/포지션 확인 가능",
    ]
    msg = send_text("\n".join(lines))
    text_mid = (msg.get("result") or {}).get("message_id")
    return {
        "ok": bool(doc.get("ok")) and bool(msg.get("ok")),
        "path": str(path),
        "bytes": path.stat().st_size,
        "document_message_id": doc_mid,
        "text_message_id": text_mid,
        "tunnel_url": tunnel_url,
    }


if __name__ == "__main__":
    try_kis = "--no-kis" not in sys.argv
    tunnel = None
    for a in sys.argv[1:]:
        if a.startswith("--tunnel="):
            tunnel = a.split("=", 1)[1].strip() or None
    if "--build-only" in sys.argv:
        _, path = build_mobile_html(try_kis=try_kis)
        print("built", path)
    else:
        out = export_and_send(try_kis=try_kis, tunnel_url=tunnel)
        print(
            "ok", out["ok"],
            "doc_mid", out["document_message_id"],
            "text_mid", out["text_message_id"],
            "path", out["path"],
            "bytes", out["bytes"],
        )
