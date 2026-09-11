#!/usr/bin/env python3
"""Midday KIS ledger resync — read-only vs orders (no new placements).

Fetches inquire_daily_ccld fills + balance, rebuilds day_ledger FIFO/linked RTs,
refreshes positions meta from current positions.json when qty matches KIS,
and lightly syncs orders_state filled flags from KIS odnos when possible.

Does NOT submit buys/sells. Restoring TP/recycle is left to the live session
(safety_freeze respected there).
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from broker_kis import LiveKISBroker  # noqa: E402
from main import load_config, today_seoul  # noqa: E402
from morning_after_approve import (  # noqa: E402
    DAY_LEDGER_PATH,
    FEE_RATE,
    POSITIONS_PATH,
    TAX_RATE,
    _as_int,
    _inquire_daily,
    _now_iso,
    _parse_ccld_df,
    _rebuild_lots,
    _write_json,
)

SEOUL = ZoneInfo("Asia/Seoul")
ORDERS_PATH = ROOT / "orders_state.json"
FILLS_JSONL = ROOT / "logs" / f"fills-live-{today_seoul().replace('-', '')}.jsonl"
DUMP_PATH = ROOT / "logs" / f"fills-kis-resync-{today_seoul().replace('-', '')}.json"


def _session_linked_sells() -> dict[str, int]:
    """Map sell fill price|odno hints -> linked_buy_price from live session jsonl."""
    linked: dict[str, int] = {}
    logs = sorted((ROOT / "logs").glob(f"live-session-{today_seoul().replace('-', '')}-*.jsonl"))
    for path in logs:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "broker.fill":
                    continue
                data = rec.get("data") or {}
                if str(data.get("side") or "").upper() != "SELL":
                    continue
                lb = data.get("linked_buy_price")
                if lb is None:
                    continue
                px = int(data.get("fill_price") or data.get("price") or 0)
                oid = str(data.get("order_id") or "")
                linked[f"px:{px}"] = int(lb)
                if oid:
                    linked[f"oid:{oid}"] = int(lb)
        except OSError:
            continue
    return linked


def _rts_linked_or_fifo(fills: list[dict[str, Any]], linked: dict[str, int]) -> list[dict[str, Any]]:
    """Prefer engine-linked buy lots for TP sells; else FIFO on today's buys."""
    fills_sorted = sorted(fills, key=lambda f: (f.get("tmd") or "", f.get("odno") or ""))
    buy_q: deque[dict[str, Any]] = deque()
    for f in fills_sorted:
        if f["side"] != "BUY":
            continue
        for _ in range(int(f.get("qty") or f.get("ccld_qty") or 1)):
            buy_q.append(
                {
                    "buy_odno": f["odno"],
                    "buy": int(f["avg"]),
                    "grid_line": int(f.get("ord_unpr") or f["avg"]),
                    "tmd": f.get("tmd") or "000000",
                }
            )
    round_trips: list[dict[str, Any]] = []
    for f in fills_sorted:
        if f["side"] != "SELL":
            continue
        rem = int(f.get("qty") or f.get("ccld_qty") or 1)
        sell_px = int(f["avg"])
        want = linked.get(f"px:{sell_px}")
        while rem > 0 and buy_q:
            lot = None
            if want is not None:
                # pull matching linked buy from queue
                kept: deque[dict[str, Any]] = deque()
                while buy_q:
                    cand = buy_q.popleft()
                    if lot is None and int(cand["buy"]) == int(want):
                        lot = cand
                    else:
                        kept.append(cand)
                buy_q = kept
            if lot is None:
                lot = buy_q.popleft()
            round_trips.append(
                {
                    "buy_odno": lot.get("buy_odno"),
                    "sell_odno": f["odno"],
                    "buy": lot["buy"],
                    "sell": sell_px,
                    "qty": 1,
                    "pnl_gross": int(sell_px - lot["buy"]),
                    "grid_line": lot.get("grid_line"),
                    "sell_tmd": f.get("tmd"),
                    "match": "linked" if want is not None and int(lot["buy"]) == int(want) else "FIFO",
                }
            )
            rem -= 1
            want = None  # only first unit uses linked hint
    return round_trips


def _append_fill_jsonl(fills: list[dict[str, Any]]) -> None:
    existing_odnos: set[str] = set()
    if FILLS_JSONL.exists():
        for line in FILLS_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("odno"):
                existing_odnos.add(str(rec["odno"]))
    with FILLS_JSONL.open("a", encoding="utf-8") as fh:
        for f in sorted(fills, key=lambda x: (x.get("tmd") or "", x.get("odno") or "")):
            odno = str(f.get("odno") or "")
            if not odno or odno in existing_odnos:
                continue
            tmd = str(f.get("tmd") or "000000").zfill(6)[-6:]
            today = today_seoul()
            rec = {
                "ts": f"{today}T{tmd[:2]}:{tmd[2:4]}:{tmd[4:6]}+09:00",
                "kind": "kis.fill_resync",
                "side": f["side"],
                "price": f["avg"],
                "qty": f.get("qty") or 1,
                "odno": odno,
                "ord_unpr": f.get("ord_unpr"),
                "symbol": "367380",
                "source": "midday_kis_resync",
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            existing_odnos.add(odno)


def run() -> int:
    cfg = load_config(ROOT / "config.json")
    today = today_seoul()
    symbol = cfg["symbol"]
    print(f"MIDDAY KIS LEDGER RESYNC  date={today} symbol={symbol} (no new orders)")

    # backup
    ts = datetime.now(SEOUL).strftime("%Y%m%d-%H%M%S")
    if DAY_LEDGER_PATH.exists():
        bak = ROOT / f"day_ledger.json.bak-pre-resync-{ts}"
        shutil.copy2(DAY_LEDGER_PATH, bak)
        print(f"backed up ledger -> {bak.name}")

    def log_fn(kind: str, message: str, data: dict) -> None:
        print(f"[{kind}] {message}")

    broker = LiveKISBroker(
        symbol=symbol,
        allow_mutations=False,
        api_retry=int(cfg.get("safety", {}).get("api_retry", 3)),
        env_dv=str(cfg.get("safety", {}).get("kis_env_dv", "real")),
        log=log_fn,
        intent_log_dir=ROOT / "logs",
    )
    broker.connect()

    last = broker.get_last_price()
    bal = broker.inquire_balance_summary()
    cash = bal.get("dnca_tot_amt")
    holdings = bal.get("holdings_symbol") or []
    hold = holdings[0] if holdings else {}
    kis_qty = int(hold.get("hldg_qty") or 0)
    kis_avg = _as_int(hold.get("pchs_avg_pric"), 0)

    df_fill, df_fill2 = _inquire_daily(broker, ccld_dvsn="01")
    df_open, _ = _inquire_daily(broker, ccld_dvsn="02")
    fills = _parse_ccld_df(df_fill, filled_only=True)
    fills = [f for f in fills if not f.get("pdno") or f["pdno"] == symbol]
    unfilled = _parse_ccld_df(df_open, filled_only=False)
    unfilled = [u for u in unfilled if not u.get("pdno") or u["pdno"] == symbol]

    fees_kis = None
    if df_fill2 is not None and not getattr(df_fill2, "empty", True):
        try:
            row = df_fill2.iloc[0]
            from morning_after_approve import _row_get, _row_keys

            keys = _row_keys(row)
            fees_kis = _as_int(_row_get(row, keys, "PRSM_TLEX_SMTL", default=None), 0)
        except Exception:
            fees_kis = None

    buy_events = [f for f in fills if f["side"] == "BUY"]
    sell_events = [f for f in fills if f["side"] == "SELL"]
    print(
        f"KIS last={last} cash={cash} hldg={kis_qty} avg={kis_avg} "
        f"fills={len(fills)} buys={len(buy_events)} sells={len(sell_events)} unfilled={len(unfilled)}"
    )
    for f in sell_events:
        print(f"  SELL odno={f['odno']} avg={f['avg']} qty={f.get('qty')} tmd={f.get('tmd')}")

    linked = _session_linked_sells()
    # open lots: prefer positions.json when qty matches
    open_lots, _fifo_rts = _rebuild_lots(today=today, kis_qty=kis_qty, fills=fills, cfg=cfg)
    round_trips = _rts_linked_or_fifo(fills, linked)
    if not round_trips:
        round_trips = _fifo_rts

    open_cost = sum(int(l["buy"]) for l in open_lots)
    matched_buy_notional = sum(rt["buy"] * rt["qty"] for rt in round_trips)
    matched_sell_notional = sum(rt["sell"] * rt["qty"] for rt in round_trips)
    today_buy_notional = sum(int(f["avg"]) * int(f.get("qty") or 1) for f in buy_events)
    realized_gross = sum(int(rt["pnl_gross"]) for rt in round_trips)
    fees_model = round((matched_buy_notional + matched_sell_notional) * FEE_RATE)
    tax_est = round(max(realized_gross, 0) * TAX_RATE)
    realized_net = float(realized_gross - fees_model - tax_est)
    capital_used = matched_buy_notional + open_cost

    ledger = {
        "date": today,
        "updated_at": _now_iso(),
        "symbol": symbol,
        "eod": False,
        "session_ended": False,
        "round_trips": round_trips,
        "realized_gross": realized_gross,
        "fees_day_est": fees_model,
        "tax_est": tax_est,
        "realized_net_est": realized_net,
        "note": (
            "midday_kis_ledger_resync: KIS inquire_daily_ccld; linked-or-FIFO RTs; "
            "open lots = positions match or carry+today; no new orders placed; "
            "fees_model=0.015%/side on matched RT notionals; tax 15.4% on gross"
        ),
        "fees_model_est": fees_model,
        "fees_kis_day_tlex": fees_kis,
        "capital_used": capital_used,
        "matched_buy_notional": matched_buy_notional,
        "open_position_cost": open_cost,
        "today_buy_notional": today_buy_notional,
        "meta": {
            "buy_fill_count": len(buy_events),
            "sell_fill_count": len(sell_events),
            "buy_fill_qty": sum(int(f.get("qty") or 1) for f in buy_events),
            "sell_fill_qty": sum(int(f.get("qty") or 1) for f in sell_events),
            "match_method": "linked_or_FIFO_today_plus_carry_open_lots",
            "unmatched_buys": [
                {
                    "odno": l.get("buy_odno"),
                    "buy": l["buy"],
                    "grid_line": l.get("grid_line"),
                    "source": l.get("source"),
                }
                for l in open_lots
            ],
            "today_fills": [
                {
                    "odno": f["odno"],
                    "side": f["side"],
                    "price": f["avg"],
                    "qty": f.get("qty") or 1,
                    "tmd": f.get("tmd"),
                }
                for f in sorted(fills, key=lambda x: (x.get("tmd") or "", x.get("odno") or ""))
            ],
            "kis_unfilled_count": len(unfilled),
            "open_pin": [int(l.get("grid_line") or l["buy"]) for l in open_lots],
            "kis_hldg_qty": kis_qty,
            "kis_avg": kis_avg,
            "kis_last": last,
            "kis_cash": cash,
        },
    }
    _write_json(DAY_LEDGER_PATH, ledger)

    # Refresh positions meta only (keep lot list / sell_order_ids from live session)
    pos = {}
    if POSITIONS_PATH.exists():
        pos = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    meta = dict(pos.get("meta") or {})
    meta.update(
        {
            "symbol": symbol,
            "kis_avg": kis_avg,
            "kis_qty": kis_qty,
            "kis_last": last,
            "kis_cash": cash,
            "ledger_synced_at": _now_iso(),
            "source": meta.get("source") or "live-session",
        }
    )
    # If positions qty drifted from KIS, rewrite from open_lots (preserve sell_order_id when possible)
    cur_qty = int(pos.get("total_qty") or len(pos.get("positions") or []))
    if cur_qty != kis_qty and open_lots:
        by_buy: dict[int, list[dict]] = {}
        for p in pos.get("positions") or []:
            by_buy.setdefault(int(p["buy_price"]), []).append(p)
        new_pos = []
        for l in open_lots:
            bp = int(l["buy"])
            prev = by_buy.get(bp) or []
            old = prev.pop(0) if prev else {}
            new_pos.append(
                {
                    "buy_price": bp,
                    "qty": 1,
                    "date": str(l.get("date") or old.get("date") or today),
                    "sell_order_id": old.get("sell_order_id") or l.get("sell_order_id"),
                    "grid_line": int(l.get("grid_line") or bp),
                    "buy_odno": l.get("buy_odno") or old.get("buy_odno"),
                    "source": l.get("source") or old.get("source"),
                }
            )
        pos = {
            "positions": new_pos,
            "count": len(new_pos),
            "total_qty": len(new_pos),
            "meta": meta,
        }
        _write_json(POSITIONS_PATH, pos)
        print(f"positions rewritten to match kis_qty={kis_qty}")
    else:
        pos["meta"] = meta
        pos["count"] = len(pos.get("positions") or [])
        pos["total_qty"] = len(pos.get("positions") or [])
        _write_json(POSITIONS_PATH, pos)
        print(f"positions meta refreshed qty={cur_qty} (matches KIS)")

    # Mark filled sells in orders_state all_orders when present; do not clobber live open book
    if ORDERS_PATH.exists():
        st = json.loads(ORDERS_PATH.read_text(encoding="utf-8"))
        sell_odnos = {str(f["odno"]) for f in sell_events}
        sell_px = {(int(f["avg"]), int(f.get("qty") or 1)) for f in sell_events}
        changed = 0
        for o in st.get("all_orders") or []:
            if str(o.get("side") or "").upper() != "SELL":
                continue
            if str(o.get("status") or "").lower() == "filled":
                continue
            px = int(o.get("fill_price") or o.get("price") or 0)
            qty = int(o.get("qty") or 1)
            odno = str(o.get("odno") or o.get("kis_odno") or "")
            if (odno and odno in sell_odnos) or ((px, qty) in sell_px and "tp_sell" in str(o.get("client_tag") or "")):
                # only mark if this order_id appears as filled in session
                o["status"] = "filled"
                o["fill_price"] = px
                o["fill_qty"] = qty
                changed += 1
        # drop filled from open orders list
        st["orders"] = [
            o
            for o in (st.get("orders") or [])
            if str(o.get("status") or "").lower() not in ("filled", "canceled", "cancelled")
        ]
        st["ledger_synced_at"] = _now_iso()
        st["kis_unfilled_count"] = len(unfilled)
        _write_json(ORDERS_PATH, st)
        print(f"orders_state touched changed_filled_flags={changed} open={len(st['orders'])}")

    dump = {
        "ts": _now_iso(),
        "fills": fills,
        "unfilled": unfilled,
        "kis": {"last": last, "cash": cash, "qty": kis_qty, "avg": kis_avg},
        "round_trips": round_trips,
        "realized_gross": realized_gross,
        "realized_net_est": realized_net,
    }
    DUMP_PATH.write_text(json.dumps(dump, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _append_fill_jsonl(fills)

    print(
        f"DONE realized_gross={realized_gross} net_est={realized_net} "
        f"rts={len(round_trips)} open_lots={len(open_lots)} "
        f"buy_fills={len(buy_events)} sell_fills={len(sell_events)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
