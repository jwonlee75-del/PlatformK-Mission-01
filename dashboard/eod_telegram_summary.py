#!/usr/bin/env python3
"""End-of-day Telegram summary for 367380 grid bot (Asia/Seoul).

Standing routine helper:
  1) Resync day_ledger from KIS (read-only; no new orders)
  2) Mark ledger eod/session_ended and archive to ledger_archive/
  3) Send EOD-labeled Telegram summary (--kis + --eod)

Exits 0 on success. On send failure after a successful resync/archive, attempts
a short error Telegram (no secrets) and still exits 0 so the routine stays quiet.
Never places live orders. Never prints tokens/secrets.
"""
from __future__ import annotations

import json
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DASH = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DASH))

SEOUL = ZoneInfo("Asia/Seoul")
LEDGER = ROOT / "day_ledger.json"
ARCHIVE_DIR = ROOT / "ledger_archive"
LIVE_APPROVED = ROOT / "LIVE_APPROVED"


def _now_iso() -> str:
    return datetime.now(SEOUL).isoformat(timespec="seconds")


def _safe_print(msg: str) -> None:
    # Avoid dumping anything that looks like credentials
    low = msg.lower()
    for bad in ("token", "appkey", "appsecret", "bearer", "authorization"):
        if bad in low:
            msg = f"[redacted:{bad}]"
            break
    print(msg, flush=True)


def _mark_and_archive_ledger() -> dict:
    led: dict = {}
    if LEDGER.exists():
        led = json.loads(LEDGER.read_text(encoding="utf-8"))
    led["eod"] = True
    led["session_ended"] = True
    led["updated_at"] = _now_iso()
    note = str(led.get("note") or "")
    if "eod_telegram_summary" not in note:
        led["note"] = (note + "; eod_telegram_summary: marked EOD/session_ended").strip("; ")
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    day = str(led.get("date") or datetime.now(SEOUL).date().isoformat())
    ymd = day.replace("-", "")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"day_ledger-{ymd}.json"
    shutil.copy2(LEDGER, dest)
    # also soft-copy under logs/
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEDGER, logs / f"day_ledger-{ymd}.json")
    return led


def _resync() -> int:
    from midday_kis_ledger_resync import run as resync_run

    return int(resync_run())


def _send_eod() -> dict:
    from telegram_summary import send_summary

    return send_summary(try_kis=True, live_tag=False, eod=True)


def _send_error_telegram(err: str) -> None:
    try:
        from telegram_summary import _api, CHAT_ID

        if not CHAT_ID:
            return
        day = datetime.now(SEOUL).strftime("%Y-%m-%d")
        text = (
            f"⚠️ 장마감 EOD 텔레그램 전송 실패 ({day} KST)\n"
            f"원인: {err[:200]}\n"
            "원장 동기화/아카이브는 완료됐을 수 있음. 수동: "
            "python3 dashboard/telegram_summary.py --kis --eod"
        )
        _api("sendMessage", {"chat_id": CHAT_ID, "text": text[:1500]}, timeout=15)
    except Exception:
        pass


def main() -> int:
    approved = LIVE_APPROVED.exists()
    _safe_print(f"EOD telegram summary start approved={approved} (no new orders)")

    try:
        rc = _resync()
        if rc != 0:
            _safe_print(f"resync_rc={rc} continuing to archive/send")
    except Exception as e:
        _safe_print(f"resync_err {type(e).__name__}: {str(e)[:120]}")
        # still try archive+send from existing ledger

    try:
        led = _mark_and_archive_ledger()
        meta = led.get("meta") or {}
        _safe_print(
            "ledger_archived "
            f"date={led.get('date')} "
            f"buys={meta.get('buy_fill_count')}/{meta.get('buy_fill_qty')} "
            f"sells={meta.get('sell_fill_count')}/{meta.get('sell_fill_qty')} "
            f"gross={led.get('realized_gross')} net={led.get('realized_net_est')} "
            f"rts={len(led.get('round_trips') or [])} "
            f"hldg={meta.get('kis_hldg_qty')}"
        )
    except Exception as e:
        _safe_print(f"archive_err {type(e).__name__}: {str(e)[:120]}")
        led = {}

    try:
        out = _send_eod()
        _safe_print(f"telegram ok={out.get('ok')} message_id={out.get('message_id')}")
        return 0
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:160]}"
        _safe_print(f"send_err {err}")
        if approved:
            _send_error_telegram(err)
        # Quiet exit for routine: still 0 after attempting error notice
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        _safe_print("fatal " + traceback.format_exc().splitlines()[-1][:160])
        raise SystemExit(0)
