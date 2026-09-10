#!/usr/bin/env python3
"""Send grid-bot dashboard summary to Telegram; handle refresh callbacks."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_status import build_status  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
CHAT_ID = "840503590"
ROOT = Path(__file__).resolve().parent.parent
OFFSET_FILE = ROOT / "logs" / "telegram_update_offset.txt"


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


def _api(method: str, payload: dict | None = None, *, timeout: int = 35) -> dict:
    token = _token()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    url = f"https://api.telegram.org/bot{token}/{method}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _fmt_won(n) -> str:
    try:
        return f"{int(n):,}원"
    except Exception:
        return "-"


def format_summary(status: dict, *, live_tag: bool = False) -> str:
    ov = status.get("overview") or {}
    pnl = status.get("pnl") or {}
    orders = status.get("open_orders") or []
    positions = status.get("positions") or []
    trades = status.get("trades") or []
    live = status.get("live_approved")
    now = datetime.now(SEOUL).strftime("%m/%d %H:%M:%S")
    tag = " · 실시간" if live_tag else ""

    price = ov.get("last_price") or ov.get("price") or ov.get("ref_price")
    cash = ov.get("cash") or ov.get("dnca_tot_amt")
    symbol = ov.get("symbol") or "367380"
    name = ov.get("symbol_name") or ov.get("name") or "ACE 미국나스닥100"
    cs = ov.get("config_summary") or {}
    spacing_won = ov.get("spacing")
    spacing_pct = cs.get("spacing_display") or (
        f"{float(cs.get('spacing_pct') or ov.get('spacing_pct') or 0)*100:.1f}%"
        if (cs.get("spacing_pct") is not None or ov.get("spacing_pct") is not None)
        else None
    )
    levels = cs.get("levels") if cs.get("levels") is not None else ov.get("levels")
    tp = cs.get("tp_display") or ov.get("tp_display")
    if not tp and (cs.get("tp_pct") is not None or ov.get("tp_pct") is not None):
        raw = cs.get("tp_pct", ov.get("tp_pct"))
        tp = f"{float(raw)*100:.1f}%"
    # human line: "0.2%(60원) / 5단 / 익절 0.2%"
    if spacing_pct and spacing_won is not None:
        spacing_txt = f"{spacing_pct}({spacing_won}원)"
    elif spacing_pct:
        spacing_txt = str(spacing_pct)
    elif spacing_won is not None:
        spacing_txt = f"{spacing_won}원"
    else:
        spacing_txt = "-"
    levels_txt = f"{levels}단" if levels is not None else "-단"
    tp_txt = str(tp) if tp is not None else "-"

    lines = [
        f"📊 그리드 대시보드 요약 ({now} KST){tag}",
        "",
        f"종목: {name} ({symbol})",
        f"현재가: {price:,}" if isinstance(price, (int, float)) else f"현재가: {price or '-'}",
        f"현금: {_fmt_won(cash)}" if cash is not None else "현금: -",
        f"설정: 간격 {spacing_txt} / {levels_txt} / 익절 {tp_txt}",
        f"실주문 승인: {'예' if live else '아니오(안전)'}",
        "",
        f"미체결: {len(orders)}건",
    ]
    for o in orders[:5]:
        side = o.get("side") or "?"
        px = o.get("price")
        qty = o.get("qty") or 1
        lines.append(f"  · {side} {qty}@{px}")
    if len(orders) > 5:
        lines.append(f"  …외 {len(orders) - 5}건")

    lines += ["", f"포지션: {len(positions)}건"]
    for pos in positions[:5]:
        lines.append(f"  · {pos.get('qty')}주 @ {pos.get('buy_price')}")
    if not positions:
        lines.append("  (없음)")

    realized = pnl.get("realized_pnl", pnl.get("realized"))
    mtm = pnl.get("mtm_pnl", pnl.get("mtm"))
    realized_g = pnl.get("realized_gross")
    fees = pnl.get("fees_day_est")
    tax = pnl.get("tax_est")
    rts = pnl.get("round_trip_count")

    buy_n = pnl.get("buy_fill_count", ov.get("buy_fill_count"))
    sell_n = pnl.get("sell_fill_count", ov.get("sell_fill_count"))
    buy_q = pnl.get("buy_fill_qty", ov.get("buy_fill_qty"))
    sell_q = pnl.get("sell_fill_qty", ov.get("sell_fill_qty"))

    def _intish(v):
        if v is None:
            return None
        try:
            f = float(v)
            return int(f) if f == int(f) else f
        except (TypeError, ValueError):
            return v

    def _won_plain(v):
        if v is None:
            return "-"
        try:
            return f"{int(round(float(v))):,}"
        except (TypeError, ValueError):
            return str(v)

    # 수익률: live first, then return_pct alias, then backtest
    ret = pnl.get("live_return_pct")
    ret_label = ""
    if ret is None:
        ret = pnl.get("return_pct")
    if ret is None and pnl.get("backtest_return_pct") is not None:
        ret = pnl.get("backtest_return_pct")
        ret_label = " (백테스트)"
    if isinstance(ret, (int, float)):
        ret_txt = f"{ret:.4f}%{ret_label}"
    else:
        ret_txt = "-" if ret is None else f"{ret}{ret_label}"

    # 평가: show mtm; if 0 and no positions, annotate 보유없음
    if mtm is None:
        mtm_txt = "-"
    elif float(mtm) == 0 and not positions:
        mtm_txt = "0 (보유없음)"
    else:
        mtm_txt = _won_plain(mtm)

    bn, bq = _intish(buy_n), _intish(buy_q)
    sn, sq = _intish(sell_n), _intish(sell_q)
    if bn is not None and sn is not None:
        fill_line = f"체결: 매수 {bn}건({bq if bq is not None else '?'}주) / 매도 {sn}건({sq if sq is not None else '?'}주)"
    else:
        fill_line = "체결: -"

    if mtm is None:
        mtm_line = "  평가(MTM): -"
    elif float(mtm) == 0 and not positions:
        mtm_line = "  평가(MTM): 0 (보유없음)"
    else:
        mtm_line = f"  평가(MTM): {_won_plain(mtm)}원"

    lines += [
        "",
        fill_line,
        "",
        "PnL",
        f"  실현(총차익): {_won_plain(realized_g)}원",
        f"  실현(순익추정): {_won_plain(realized)}원",
        f"  왕복: {rts if rts is not None else '-'}회 / 수수료~{_won_plain(fees)} / 세금~{_won_plain(tax)}",
        mtm_line,
        f"  수익률: {ret_txt}",
        "",
        f"최근 체결 로그: {len(trades)}건",
    ]
    for t in trades[:3]:
        kind = t.get("kind") or t.get("side") or "fill"
        msg = (t.get("message") or t.get("summary") or "")[:60]
        lines.append(f"  · {kind} {msg}")

    lines += ["", "🔄 새로고침으로 실시간 다시 받기"]
    return "\n".join(lines)


def send_summary(*, try_kis: bool = False, live_tag: bool = False) -> dict:
    status = build_status(try_kis=try_kis)
    text = format_summary(status, live_tag=live_tag or try_kis)
    today = datetime.now(SEOUL).strftime("%Y%m%d")
    payload = {
        "chat_id": CHAT_ID,
        "text": text[:3500],
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🔄 새로고침", "callback_data": f"grid_dash_refresh_{today}"},
                {"text": "📱 대시보드 HTML", "callback_data": f"grid_dash_html_{today}"},
            ]]
        },
    }
    data = _api("sendMessage", payload, timeout=20)
    return {
        "ok": bool(data.get("ok")),
        "message_id": (data.get("result") or {}).get("message_id"),
    }


def _read_offset() -> int | None:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return None


def _write_offset(n: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(n))


def process_refresh_callbacks(*, try_kis: bool = True, long_poll: int = 0) -> int:
    offset = _read_offset()
    params: dict = {"timeout": int(long_poll), "limit": 50}
    if offset is not None:
        params["offset"] = offset
    token = _token()
    q = urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{token}/getUpdates?{q}"
    with urllib.request.urlopen(url, timeout=max(40, long_poll + 10)) as r:
        data = json.load(r)
    handled = 0
    for u in data.get("result") or []:
        uid = u["update_id"]
        _write_offset(uid + 1)
        cq = u.get("callback_query")
        if not cq:
            continue
        cb = cq.get("data") or ""
        if cb.startswith("grid_dash_html_"):
            _api(
                "answerCallbackQuery",
                {
                    "callback_query_id": cq["id"],
                    "text": "대시보드 HTML 전송 중…",
                    "show_alert": False,
                },
                timeout=15,
            )
            try:
                from export_mobile_html import export_and_send
                tunnel = None
                try:
                    tunnel = (ROOT / "logs" / "tunnel_url.txt").read_text().strip() or None
                except Exception:
                    tunnel = None
                export_and_send(try_kis=try_kis, tunnel_url=tunnel)
            except Exception as e:
                _api(
                    "sendMessage",
                    {"chat_id": CHAT_ID, "text": f"HTML 전송 실패: {type(e).__name__}"},
                    timeout=15,
                )
            handled += 1
            continue
        if not cb.startswith("grid_dash_refresh_"):
            continue
        if cb.endswith("_sample"):
            _api(
                "answerCallbackQuery",
                {
                    "callback_query_id": cq["id"],
                    "text": "샘플이라 새로고침 없음",
                    "show_alert": False,
                },
                timeout=15,
            )
            continue
        _api(
            "answerCallbackQuery",
            {
                "callback_query_id": cq["id"],
                "text": "실시간 요약 불러오는 중…",
                "show_alert": False,
            },
            timeout=15,
        )
        send_summary(try_kis=try_kis, live_tag=True)
        handled += 1
    return handled


def serve_refresh_until(end_hhmm: str = "15:25", *, poll_timeout: int = 25) -> None:
    eh, em = map(int, end_hhmm.split(":"))
    print(f"refresh poller until {end_hhmm} KST", flush=True)
    while True:
        now = datetime.now(SEOUL)
        if now.time() >= dtime(eh, em):
            print("poller end", flush=True)
            break
        try:
            n = process_refresh_callbacks(try_kis=True, long_poll=poll_timeout)
            if n:
                print(f"handled {n} refresh(es)", flush=True)
        except Exception as e:
            print(f"poll_err {type(e).__name__}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--poll-once" in args:
        n = process_refresh_callbacks(try_kis="--no-kis" not in args, long_poll=0)
        print("handled", n)
    elif "--serve-refresh" in args:
        until = "15:25"
        for a in sys.argv[1:]:
            if a.startswith("--until="):
                until = a.split("=", 1)[1]
        serve_refresh_until(until)
    else:
        try_kis = "--kis" in args or "--live" in args
        out = send_summary(try_kis=try_kis, live_tag=try_kis)
        print("ok", out["ok"], "message_id", out["message_id"])
