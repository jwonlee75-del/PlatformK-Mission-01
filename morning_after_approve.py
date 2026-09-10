#!/usr/bin/env python3
"""Post-Telegram-approve morning entrypoint (real orders).

Restores overnight TP sells, syncs day_ledger for today's Seoul date,
places any missing buy lines, writes state, sends Telegram summary, exits.
Does NOT start the live session loop (routine starts that separately).

Requires: LIVE_APPROVED with today's Seoul YYYY-MM-DD AND/OR --i-approve-live-orders.
Never prints secrets/tokens.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from broker_kis import LiveKISBroker  # noqa: E402
from grid_engine import calc_spacing, build_buy_lines  # noqa: E402
from models import Order, OrderStatus, Side  # noqa: E402
from persistence import append_jsonl  # noqa: E402
from session import SEOUL  # noqa: E402
from main import (  # noqa: E402
    load_config,
    live_approval_ok,
    today_seoul,
    telegram_alert,
    LIVE_APPROVED_PATH,
    ORDERS_STATE_PATH,
    make_run_log_path,
)

CONFIG_PATH = ROOT / "config.json"
DAY_LEDGER_PATH = ROOT / "day_ledger.json"
POSITIONS_PATH = ROOT / "positions.json"
FEE_RATE = 0.00015  # per side on matched RT notionals
TAX_RATE = 0.154


def _now() -> datetime:
    return datetime.now(SEOUL)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _row_keys(row) -> dict[str, Any]:
    return {str(k).upper(): k for k in row.index}


def _row_get(row, keys: dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        k = keys.get(n.upper())
        if k is not None:
            return row[k]
    return default


def _as_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return default
        return int(float(str(v)))
    except (TypeError, ValueError):
        return default


def _norm_odno(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    # keep zero-padded form when present; also allow comparisons via lstrip
    return s


def _parse_ccld_df(df, *, filled_only: bool | None = None) -> list[dict[str, Any]]:
    """Parse inquire_daily_ccld output1 rows into fill/order dicts."""
    out: list[dict[str, Any]] = []
    if df is None or getattr(df, "empty", True):
        return out
    for _, row in df.iterrows():
        keys = _row_keys(row)
        odno = _norm_odno(_row_get(row, keys, "ODNO", "ORGN_ODNO", default=""))
        side_raw = str(_row_get(row, keys, "SLL_BUY_DVSN_CD", default="") or "")
        # 01 sell, 02 buy (KIS)
        if side_raw == "01":
            side = "SELL"
        elif side_raw == "02":
            side = "BUY"
        else:
            # fallback text
            txt = str(_row_get(row, keys, "SLL_BUY_DVSN_CD_NAME", "SLL_BUY_DVSN", default="") or "")
            side = "SELL" if "매도" in txt else ("BUY" if "매수" in txt else "")
        avg = _as_int(_row_get(row, keys, "AVG_PRVS", "CCLD_UNPR", default=0))
        ccld_qty = _as_int(_row_get(row, keys, "TOT_CCLD_QTY", "CCLD_QTY", default=0))
        ord_qty = _as_int(_row_get(row, keys, "ORD_QTY", default=0))
        rmn_qty = _as_int(_row_get(row, keys, "RMN_QTY", "PSBL_QTY", default=0))
        ord_unpr = _as_int(_row_get(row, keys, "ORD_UNPR", default=0))
        tmd = str(_row_get(row, keys, "ORD_TMD", "CCLD_TMD", default="") or "").zfill(6)[-6:]
        orgno = str(_row_get(row, keys, "ORD_GNO_BRNO", "KRX_FWDG_ORD_ORGNO", default="") or "")
        pdno = str(_row_get(row, keys, "PDNO", default="") or "")
        rec = {
            "odno": odno,
            "side": side,
            "avg": avg or ord_unpr,
            "qty": ccld_qty if ccld_qty > 0 else (ord_qty if filled_only is False else ccld_qty),
            "ccld_qty": ccld_qty,
            "ord_qty": ord_qty,
            "rmn_qty": rmn_qty if rmn_qty > 0 else max(ord_qty - ccld_qty, 0),
            "ord_unpr": ord_unpr or avg,
            "tmd": tmd or "000000",
            "ord_orgno": orgno,
            "pdno": pdno,
        }
        if filled_only is True and rec["ccld_qty"] <= 0:
            continue
        if filled_only is False and rec["rmn_qty"] <= 0:
            continue
        if not rec["side"]:
            continue
        out.append(rec)
    return out


def _inquire_daily(
    broker: LiveKISBroker, *, ccld_dvsn: str
) -> tuple[Any, Any]:
    tr = broker._trenv()
    today = _now().strftime("%Y%m%d")

    def _call():
        return broker._inquire_daily_ccld_fn(
            env_dv=broker.env_dv,
            pd_dv="inner",
            cano=tr.my_acct,
            acnt_prdt_cd=tr.my_prod,
            inqr_strt_dt=today,
            inqr_end_dt=today,
            sll_buy_dvsn_cd="00",
            ccld_dvsn=ccld_dvsn,
            inqr_dvsn="00",
            inqr_dvsn_3="00",
            pdno=broker.symbol,
            excg_id_dvsn_cd=broker.excg_id_dvsn_cd,
        )

    return broker._with_retry(f"inquire_daily_ccld:{ccld_dvsn}", _call)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _prior_ledger_for_carry(today: str) -> dict[str, Any]:
    """Return prior-day ledger (or current if still previous date) for carry lots."""
    led = _load_json(DAY_LEDGER_PATH)
    if not led:
        return {}
    d = str(led.get("date") or "")
    if d and d < today:
        return led
    if d == today:
        # Already today's — use unmatched as current open; no extra carry needed
        return led
    return led


def _lots_from_positions_file(kis_qty: int) -> list[dict[str, Any]] | None:
    raw = _load_json(POSITIONS_PATH)
    positions = raw.get("positions") or []
    if not positions:
        return None
    lots: list[dict[str, Any]] = []
    for p in positions:
        qty = int(p.get("qty") or 1)
        for _ in range(qty):
            lots.append(
                {
                    "buy": int(p["buy_price"]),
                    "grid_line": int(p.get("grid_line") or p["buy_price"]),
                    "buy_odno": str(p.get("buy_odno") or ""),
                    "date": str(p.get("date") or ""),
                    "source": str(p.get("source") or "positions.json"),
                    "sell_order_id": p.get("sell_order_id"),
                }
            )
    if len(lots) == kis_qty:
        return lots
    return None


def _rebuild_lots(
    *,
    today: str,
    kis_qty: int,
    fills: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (open_lots, round_trips). Abort caller if qty cannot reconcile."""
    preferred = _lots_from_positions_file(kis_qty)
    fills_sorted = sorted(fills, key=lambda f: (f.get("tmd") or "", f.get("odno") or ""))
    buy_events = [f for f in fills_sorted if f["side"] == "BUY"]
    sell_events = [f for f in fills_sorted if f["side"] == "SELL"]

    # FIFO RTs for today (always from today's fills)
    buy_q: deque[dict[str, Any]] = deque()
    for f in buy_events:
        for _ in range(int(f.get("qty") or f.get("ccld_qty") or 1)):
            buy_q.append(
                {
                    "buy_odno": f["odno"],
                    "buy": int(f["avg"]),
                    "grid_line": int(f.get("ord_unpr") or f["avg"]),
                    "source": "today",
                    "tmd": f.get("tmd") or "000000",
                    "date": today,
                }
            )
    round_trips: list[dict[str, Any]] = []
    for f in sell_events:
        rem = int(f.get("qty") or f.get("ccld_qty") or 1)
        while rem > 0 and buy_q:
            lot = buy_q.popleft()
            sell_px = int(f["avg"])
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
                }
            )
            rem -= 1
    today_unmatched = list(buy_q)

    if preferred is not None:
        # Keep preferred lots; still return today's RTs
        return preferred, round_trips

    # Reconstruct: prior open unmatched + today's unmatched buys
    prior = _prior_ledger_for_carry(today)
    carry: list[dict[str, Any]] = []
    prior_date = str(prior.get("date") or "")
    meta = prior.get("meta") or {}
    if prior_date and prior_date < today:
        um = list(meta.get("unmatched_buys") or [])
        pins = list(meta.get("open_pin") or [])
        ordered: list[dict[str, Any]] = []
        used: set[int] = set()
        for px in pins:
            for i, u in enumerate(um):
                if i in used:
                    continue
                gl = int(u.get("grid_line") or u.get("buy") or 0)
                bp = int(u.get("buy") or 0)
                if gl == int(px) or bp == int(px):
                    ordered.append(u)
                    used.add(i)
                    break
        for i, u in enumerate(um):
            if i not in used:
                ordered.append(u)
        for u in ordered:
            carry.append(
                {
                    "buy_odno": str(u.get("odno") or u.get("buy_odno") or ""),
                    "buy": int(u["buy"]),
                    "grid_line": int(u.get("grid_line") or u["buy"]),
                    "source": f"carry_{prior_date}",
                    "date": prior_date,
                    "tmd": "000000",
                }
            )
    elif prior_date == today:
        # Ledger already today — use its unmatched as open base if positions missing
        for u in meta.get("unmatched_buys") or []:
            carry.append(
                {
                    "buy_odno": str(u.get("odno") or u.get("buy_odno") or ""),
                    "buy": int(u["buy"]),
                    "grid_line": int(u.get("grid_line") or u["buy"]),
                    "source": str(u.get("source") or "ledger_today"),
                    "date": today,
                    "tmd": "000000",
                }
            )
        # If we took today's unmatched from ledger, don't double-add today_unmatched
        open_lots = carry
        if len(open_lots) == kis_qty:
            return open_lots, round_trips
        # fall through to combine carefully
        carry = [c for c in carry if str(c.get("source") or "").startswith("carry_")]

    open_lots = carry + today_unmatched
    if len(open_lots) == kis_qty:
        return open_lots, round_trips

    # Cannot reconcile
    raise RuntimeError(
        f"cannot reconcile lots: rebuilt={len(open_lots)} kis_hldg_qty={kis_qty} "
        f"carry={len(carry)} today_unmatched={len(today_unmatched)} "
        f"symbol={cfg.get('symbol')}"
    )


def _seed_broker_open_from_unfilled(
    broker: LiveKISBroker, unfilled: list[dict[str, Any]], symbol: str
) -> None:
    """Hydrate local open-order map so duplicate price checks see KIS book."""
    max_n = 0
    for u in unfilled:
        oid = f"KIS-{u['odno']}"
        side = Side.BUY if u["side"] == "BUY" else Side.SELL
        o = Order(
            order_id=oid,
            symbol=symbol,
            side=side,
            price=int(u["ord_unpr"]),
            qty=int(u["rmn_qty"] or u.get("ord_qty") or 1),
            status=OrderStatus.OPEN,
            client_tag=f"kis_open@{u['ord_unpr']}",
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        broker._orders[oid] = o
        broker._kis_meta[oid] = {
            "odno": u["odno"],
            "ord_orgno": u.get("ord_orgno") or "",
            "side": side.value,
            "price": o.price,
            "qty": o.qty,
            "status": OrderStatus.OPEN.value,
        }
        try:
            max_n = max(max_n, int(str(u["odno"]).lstrip("0") or "0"))
        except ValueError:
            pass
    # Also bump LIVE- seq from existing orders_state
    st = _load_json(ORDERS_STATE_PATH)
    for oid in list((st.get("kis_meta") or {}).keys()) + list(
        (o.get("order_id") for o in (st.get("orders") or []))
    ):
        if isinstance(oid, str) and oid.startswith("LIVE-"):
            try:
                max_n = max(max_n, int(oid.split("-", 1)[1]))
            except ValueError:
                pass
    import itertools

    broker._id_seq = itertools.count(max(max_n, 1) + 1)


def run(args: argparse.Namespace) -> int:
    cfg = load_config(CONFIG_PATH)
    ok, reason = live_approval_ok(args, cfg)
    if not ok:
        print("MORNING_AFTER_APPROVE REFUSED — no orders.", file=sys.stderr)
        print(f"  reason: {reason}", file=sys.stderr)
        print(
            f"  Approve: echo {today_seoul()} > {LIVE_APPROVED_PATH}",
            file=sys.stderr,
        )
        print(
            "  Or: python3 morning_after_approve.py --i-approve-live-orders",
            file=sys.stderr,
        )
        return 2

    run_log = make_run_log_path("morning-after-approve")

    def log_fn(kind: str, message: str, data: dict) -> None:
        append_jsonl(
            str(run_log),
            {
                "ts": _now_iso(),
                "kind": kind,
                "message": message,
                "data": data,
            },
        )
        print(f"[{kind}] {message}")

    today = today_seoul()
    symbol = cfg["symbol"]
    print("=" * 72)
    print("MORNING AFTER APPROVE  (REAL ORDERS — restore TP + missing buys)")
    print(f"approval={reason}  date={today}  symbol={symbol}")
    print("=" * 72)

    # 1) Connect read-only first
    broker = LiveKISBroker(
        symbol=symbol,
        allow_mutations=False,
        api_retry=int(cfg.get("safety", {}).get("api_retry", 3)),
        env_dv=str(cfg.get("safety", {}).get("kis_env_dv", "real")),
        log=log_fn,
        intent_log_dir=ROOT / "logs",
    )
    broker.connect()

    try:
        last = broker.get_last_price()
        bal = broker.inquire_balance_summary()
    except Exception as e:  # noqa: BLE001
        msg = f"❌ morning_after_approve read fail\n{type(e).__name__}: {e}"
        telegram_alert(msg, cfg=cfg)
        return 1

    cash = bal.get("dnca_tot_amt")
    holdings = (bal.get("holdings_symbol") or [])
    hold = holdings[0] if holdings else {}
    kis_qty = int(hold.get("hldg_qty") or 0)
    kis_avg = _as_int(hold.get("pchs_avg_pric"), 0)

    try:
        df_fill, df_fill2 = _inquire_daily(broker, ccld_dvsn="01")
        df_open, _ = _inquire_daily(broker, ccld_dvsn="02")
    except Exception as e:  # noqa: BLE001
        msg = f"❌ morning_after_approve ccld fail\n{type(e).__name__}: {e}"
        telegram_alert(msg, cfg=cfg)
        return 1

    fills = _parse_ccld_df(df_fill, filled_only=True)
    # filter symbol if present
    fills = [f for f in fills if not f.get("pdno") or f["pdno"] == symbol]
    unfilled = _parse_ccld_df(df_open, filled_only=False)
    unfilled = [u for u in unfilled if not u.get("pdno") or u["pdno"] == symbol]

    fees_kis = None
    if df_fill2 is not None and not getattr(df_fill2, "empty", True):
        try:
            row = df_fill2.iloc[0]
            keys = _row_keys(row)
            fees_kis = _as_int(_row_get(row, keys, "PRSM_TLEX_SMTL", default=None), 0)
        except Exception:  # noqa: BLE001
            fees_kis = None

    spacing = calc_spacing(
        last, cfg["grid"]["spacing_pct"], cfg["grid"]["tick_size"]
    )
    buy_lines = build_buy_lines(last, spacing, int(cfg["grid"]["levels"]))
    log_fn(
        "morning.snapshot",
        f"last={last} cash={cash} hldg={kis_qty} avg={kis_avg} "
        f"fills={len(fills)} unfilled={len(unfilled)} spacing={spacing}",
        {
            "last": last,
            "cash": cash,
            "kis_qty": kis_qty,
            "kis_avg": kis_avg,
            "spacing": spacing,
            "buy_lines": buy_lines,
            "fill_count": len(fills),
            "unfilled_count": len(unfilled),
        },
    )

    # 3) Rebuild lots
    try:
        open_lots, round_trips = _rebuild_lots(
            today=today, kis_qty=kis_qty, fills=fills, cfg=cfg
        )
    except Exception as e:  # noqa: BLE001
        msg = (
            f"❌ morning_after_approve reconcile fail\n"
            f"{type(e).__name__}: {e}\n"
            f"KIS hldg_qty={kis_qty} last={last}"
        )
        print(msg, file=sys.stderr)
        telegram_alert(msg, cfg=cfg)
        return 1

    # Enable mutations for placing
    broker.allow_mutations = True
    _seed_broker_open_from_unfilled(broker, unfilled, symbol)

    open_sell_prices = {
        int(u["ord_unpr"])
        for u in unfilled
        if u["side"] == "SELL" and int(u.get("rmn_qty") or 0) > 0
    }
    open_buy_prices = {
        int(u["ord_unpr"])
        for u in unfilled
        if u["side"] == "BUY" and int(u.get("rmn_qty") or 0) > 0
    }

    qty_per = int(cfg["grid"]["qty_per_order"])
    placed_tps: list[Order] = []
    linked_existing_tps: list[dict[str, Any]] = []
    skipped_tps: list[dict[str, Any]] = []

    # 4) Restore TP sells
    for lot in open_lots:
        buy_px = int(lot["buy"])
        tp_px = buy_px + spacing
        if tp_px in open_sell_prices:
            skipped_tps.append({"buy": buy_px, "tp": tp_px, "reason": "already_open"})
            linked_existing_tps.append({"buy": buy_px, "tp": tp_px})
            lot["tp"] = tp_px
            lot["sell_order_id"] = lot.get("sell_order_id") or f"EXISTING@{tp_px}"
            continue
        order = Order(
            order_id="",
            symbol=symbol,
            side=Side.SELL,
            price=tp_px,
            qty=qty_per,
            client_tag=f"tp_sell@{buy_px}",
            linked_buy_price=buy_px,
        )
        try:
            o = broker.submit(order)
            placed_tps.append(o)
            open_sell_prices.add(tp_px)
            lot["tp"] = tp_px
            lot["sell_order_id"] = o.order_id
            log_fn(
                "morning.tp_placed",
                f"SELL {o.qty}@{tp_px} for buy={buy_px} id={o.order_id}",
                {"buy": buy_px, "tp": tp_px, "order_id": o.order_id},
            )
        except Exception as e:  # noqa: BLE001
            msg = f"❌ TP place fail buy={buy_px} tp={tp_px}: {type(e).__name__}: {e}"
            print(msg, file=sys.stderr)
            telegram_alert(msg, cfg=cfg)
            return 1

    # 5) Place missing buys (do NOT wipe holdings)
    levels = int(cfg["grid"]["levels"])
    max_new = int(cfg["limits"]["max_new_buys_per_day"])
    max_hold = int(cfg["limits"]["max_holdings"])
    today_buy_fills = sum(
        int(f.get("qty") or f.get("ccld_qty") or 1)
        for f in fills
        if f["side"] == "BUY"
    )
    # Count only "new" buys toward daily cap — treat all today's buy fills as new for morning
    remaining_new = max(0, max_new - today_buy_fills)
    remaining_hold_slots = max(0, max_hold - kis_qty - len(open_buy_prices))

    placed_buys: list[Order] = []
    skipped_buys: list[dict[str, Any]] = []
    for i, px in enumerate(buy_lines):
        if px in open_buy_prices:
            skipped_buys.append({"price": px, "reason": "already_open"})
            continue
        if len(placed_buys) >= remaining_new:
            skipped_buys.append({"price": px, "reason": "max_new_buys"})
            continue
        if len(placed_buys) >= remaining_hold_slots:
            skipped_buys.append({"price": px, "reason": "max_holdings_slots"})
            continue
        if len(open_buy_prices) + len(placed_buys) >= levels and False:
            pass  # levels is target lines, not a hard open-buy cap beyond missing
        order = Order(
            order_id="",
            symbol=symbol,
            side=Side.BUY,
            price=int(px),
            qty=qty_per,
            client_tag=f"grid_L{i+1}@{px}",
        )
        try:
            o = broker.submit(order)
            placed_buys.append(o)
            open_buy_prices.add(int(px))
            log_fn(
                "morning.buy_placed",
                f"BUY {o.qty}@{px} id={o.order_id}",
                {"price": px, "order_id": o.order_id},
            )
        except Exception as e:  # noqa: BLE001
            # Duplicate locally / broker reject — skip
            skipped_buys.append(
                {"price": px, "reason": f"{type(e).__name__}: {e}"}
            )
            log_fn("morning.buy_skip", f"@{px} {type(e).__name__}", {"price": px})

    # Re-fetch unfilled after placements for accurate open book
    try:
        df_open2, _ = _inquire_daily(broker, ccld_dvsn="02")
        unfilled_after = _parse_ccld_df(df_open2, filled_only=False)
        unfilled_after = [
            u for u in unfilled_after if not u.get("pdno") or u["pdno"] == symbol
        ]
    except Exception:  # noqa: BLE001
        unfilled_after = unfilled

    # 6) Write day_ledger / positions / orders_state for TODAY
    buy_events = [f for f in fills if f["side"] == "BUY"]
    sell_events = [f for f in fills if f["side"] == "SELL"]
    open_cost = sum(int(l["buy"]) for l in open_lots)
    matched_buy_notional = sum(rt["buy"] * rt["qty"] for rt in round_trips)
    matched_sell_notional = sum(rt["sell"] * rt["qty"] for rt in round_trips)
    today_buy_notional = sum(int(f["avg"]) * int(f.get("qty") or 1) for f in buy_events)
    realized_gross = sum(int(rt["pnl_gross"]) for rt in round_trips)
    fees_model = round((matched_buy_notional + matched_sell_notional) * FEE_RATE)
    tax_est = round(max(realized_gross, 0) * TAX_RATE)
    realized_net = float(realized_gross - fees_model - tax_est)
    capital_used = matched_buy_notional + open_cost
    lot_avg = round(open_cost / len(open_lots)) if open_lots else 0

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
            "morning_after_approve: KIS inquire_daily_ccld; FIFO RTs today; "
            "open lots = positions match or prior carry + today unmatched; "
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
            "match_method": "FIFO_today_plus_carry_open_lots",
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
            "kis_unfilled_count": len(unfilled_after),
            "open_pin": [int(l.get("grid_line") or l["buy"]) for l in open_lots],
            "kis_hldg_qty": kis_qty,
            "kis_avg": kis_avg,
            "kis_last": last,
            "kis_cash": cash,
        },
    }
    _write_json(DAY_LEDGER_PATH, ledger)

    # positions.json — one lot per share
    pos_list = []
    open_tp_meta = []
    for l in open_lots:
        tp = int(l.get("tp") or (int(l["buy"]) + spacing))
        pos_list.append(
            {
                "buy_price": int(l["buy"]),
                "qty": 1,
                "date": str(l.get("date") or today),
                "sell_order_id": l.get("sell_order_id"),
                "grid_line": int(l.get("grid_line") or l["buy"]),
                "buy_odno": l.get("buy_odno"),
                "source": l.get("source"),
            }
        )
        open_tp_meta.append(
            {
                "buy": int(l["buy"]),
                "tp": tp,
                "order_id": l.get("sell_order_id"),
            }
        )
    positions_payload = {
        "positions": pos_list,
        "count": len(pos_list),
        "total_qty": len(pos_list),
        "meta": {
            "symbol": symbol,
            "spacing": spacing,
            "buy_lines": buy_lines,
            "new_buys_filled_today": today_buy_fills,
            "updated_at": _now_iso(),
            "source": "morning_after_approve",
            "kis_avg": kis_avg,
            "kis_qty": kis_qty,
            "kis_last": last,
            "kis_cash": cash,
            "lot_avg": lot_avg,
            "open_tp_sells": open_tp_meta,
            "tps_placed_now": [o.order_id for o in placed_tps],
            "buys_placed_now": [o.order_id for o in placed_buys],
        },
    }
    _write_json(POSITIONS_PATH, positions_payload)

    # orders_state.json — open book from KIS unfilled + newly placed
    orders_out: list[dict[str, Any]] = []
    kis_meta_out: dict[str, Any] = {}
    # Map newly placed
    for o in placed_tps + placed_buys:
        orders_out.append(o.to_dict())
        meta = broker.export_kis_meta().get(o.order_id) or {}
        kis_meta_out[o.order_id] = dict(meta)

    # Include remaining KIS unfilled not already represented by new LIVE ids
    new_odnos = {
        str((kis_meta_out.get(o.order_id) or {}).get("odno"))
        for o in placed_tps + placed_buys
    }
    for u in unfilled_after:
        if u["odno"] in new_odnos:
            continue
        oid = f"KIS-{u['odno']}"
        # Prefer existing LIVE id from previous orders_state
        prev = _load_json(ORDERS_STATE_PATH)
        for poid, pm in (prev.get("kis_meta") or {}).items():
            if str(pm.get("odno")) == str(u["odno"]):
                oid = poid
                break
        side = Side.BUY if u["side"] == "BUY" else Side.SELL
        od = {
            "order_id": oid,
            "symbol": symbol,
            "side": side.value,
            "price": int(u["ord_unpr"]),
            "qty": int(u["rmn_qty"] or 1),
            "status": "open",
            "client_tag": (
                f"tp_sell@{int(u['ord_unpr']) - spacing}"
                if side == Side.SELL
                else f"grid_open@{u['ord_unpr']}"
            ),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "fill_price": None,
            "fill_qty": 0,
            "linked_buy_price": (
                int(u["ord_unpr"]) - spacing if side == Side.SELL else None
            ),
            "is_ratchet_reorder": False,
            "is_recycle_rebuy": False,
        }
        orders_out.append(od)
        kis_meta_out[oid] = {
            "odno": u["odno"],
            "ord_orgno": u.get("ord_orgno") or "",
            "side": side.value,
            "price": int(u["ord_unpr"]),
            "qty": int(u["rmn_qty"] or 1),
            "status": "open",
        }

    orders_state = {
        "updated_at": _now_iso(),
        "mode": "morning_after_approve",
        "ref_price": last,
        "spacing": spacing,
        "buy_lines": buy_lines,
        "day_high": last,
        "new_buys_filled_today": today_buy_fills,
        "orders": orders_out,
        "kis_meta": kis_meta_out,
        "meta": {
            "tps_placed": len(placed_tps),
            "buys_placed": len(placed_buys),
            "skipped_tps": skipped_tps,
            "skipped_buys": skipped_buys,
            "kis_unfilled": len(unfilled_after),
        },
    }
    _write_json(ORDERS_STATE_PATH, orders_state)

    # 7) Telegram summary
    open_buys = [o for o in orders_out if o["side"] == "BUY"]
    open_sells = [o for o in orders_out if o["side"] == "SELL"]
    lines = [
        "✅ morning_after_approve 완료",
        "",
        f"종목: {symbol} ({cfg.get('symbol_name', '')})",
        f"일자: {today}",
        f"현재가: {last:,}  간격: {spacing}",
        f"보유: {kis_qty}주 (avg≈{kis_avg:,})",
        f"TP 신규: {len(placed_tps)} / 기존유지: {len(skipped_tps)}",
        f"매수 신규: {len(placed_buys)} / 스킵: {len(skipped_buys)}",
        f"오픈북: 매수 {len(open_buys)} / 매도(TP) {len(open_sells)}",
        f"당일 RT: {len(round_trips)} (실현 {realized_gross})",
        f"매수선: {buy_lines}",
    ]
    if placed_tps:
        lines.append("TP:")
        for o in placed_tps:
            lines.append(f"  • SELL {o.qty}@{o.price:,} ({o.client_tag})")
    if placed_buys:
        lines.append("BUY:")
        for o in placed_buys:
            lines.append(f"  • BUY {o.qty}@{o.price:,} ({o.client_tag})")
    telegram_alert("\n".join(lines), cfg=cfg)

    # Optional dashboard summary (best-effort; never fail the run)
    try:
        import subprocess

        subprocess.run(
            ["python3", str(ROOT / "dashboard" / "telegram_summary.py"), "--kis"],
            check=False,
            timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[telegram_summary] skip: {type(e).__name__}")

    print("=" * 72)
    print(
        f"DONE tps_new={len(placed_tps)} buys_new={len(placed_buys)} "
        f"holdings={kis_qty} ledger={DAY_LEDGER_PATH}"
    )
    print("=" * 72)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Morning post-approve grid restore + buys")
    ap.add_argument(
        "--i-approve-live-orders",
        action="store_true",
        help="Explicit live order approval (also accepts LIVE_APPROVED today)",
    )
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
