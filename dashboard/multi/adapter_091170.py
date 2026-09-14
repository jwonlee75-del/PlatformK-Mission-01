"""Read-only adapter for the slot-based 091170 (KODEX 은행) bot.

Operator SSOT files under ``GRID_BOT_091170_ROOT`` (never this repo's state):

  last_price.json   — last, base, day_high, updated_at
  day_ledger.json   — daily_buy_notional, fills[], plan_buys/tps, safety_frozen,
                      freeze_reasons, ratchet_steps, ma20, session_date
  orders_state.json — orders[], open_orders[] (side/price/qty/status/slot_id)
  positions.json    — qty, avg_price, positions[], slots[] (repeats_done, buy_price,
                      open buy/sell ids)
  plan.json         — morning buys/tps/slots, caps, kis_cash, placed_order_ids
  LIVE_APPROVED, config.json
  ledger_archive/   — optional multi-day realized

PnL: ``realized_*`` / ``round_trips`` if present; else pair ``fills`` by slot_id or
the bot's TP offsets (+50/+75). Unpaired fills stay zero — no generic FIFO.
If ``ops_summary.py`` exists on the 091170 root, prefer its structured PnL helpers.
Never prints secrets.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional

from common import (
    config_summary,
    empty_bot,
    in_session,
    list_present_files,
    live_approved,
    now_seoul,
    num,
    num_or_zero,
    read_json,
    redact,
)

BOT_ID = "091170"
SYMBOL_NAME = "KODEX 은행"
SCHEMA = "slots"

_WATCH_FILES = (
    "config.json",
    "last_price.json",
    "positions.json",
    "plan.json",
    "day_ledger.json",
    "orders_state.json",
    "LIVE_APPROVED",
    "ops_summary.py",
)

# 김프로 slots: offsets -75/-180/-330/-525, TP +50/+75/+75/+75
_TP_OFFSETS = (50, 75)
_KIMPRO = {
    "slot_count": 4,
    "slot_offsets": [-75, -180, -330, -525],
    "qty_per_slot": 5,
    "tp_offsets": [50, 75, 75, 75],
    "daily_buy_cap": 800_000,
    "order_cap": 100_000,
    "sibling_cash_reserve": 200_000,
    "session_start": "09:05",
    "session_end": "15:00",
}

# Slot statuses that mean "we hold inventory here"
_HOLDING = {
    "filled",
    "holding",
    "held",
    "open",
    "active",
    "in_position",
    "sell_pending",
    "waiting_sell",
    "tp_pending",
    "occupied",
}
_EMPTY = {
    "empty",
    "idle",
    "free",
    "waiting_buy",
    "pending_buy",
    "planned",
    "none",
    "",
}


def _first(*vals: Any) -> Any:
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def _as_list(obj: Any, *keys: str) -> list:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _slot_id(s: dict) -> Any:
    return _first(s.get("slot_id"), s.get("slot"), s.get("id"), s.get("idx"), s.get("level"))


def _slot_is_holding(s: dict) -> bool:
    status = str(s.get("status") or s.get("state") or s.get("phase") or "").lower()
    buy = _first(s.get("buy_price"), s.get("avg_price"), s.get("fill_price"), s.get("price"))
    qty = num(s.get("qty") if s.get("qty") is not None else s.get("quantity", s.get("filled_qty")))
    if status in _EMPTY and (qty is None or qty <= 0) and buy is None:
        return False
    if status in _HOLDING:
        return True
    if qty is not None and qty > 0 and buy is not None:
        return True
    if buy is not None and status not in _EMPTY:
        return True
    return False


def _normalize_slot(s: dict) -> dict:
    buy = num(_first(s.get("buy_price"), s.get("avg_price"), s.get("fill_price"), s.get("price")))
    qty = num(_first(s.get("qty"), s.get("quantity"), s.get("filled_qty"))) or 0
    return {
        "slot": _slot_id(s),
        "status": s.get("status") or s.get("state") or s.get("phase"),
        "buy_price": buy,
        "qty": qty,
        "sell_price": num(_first(s.get("sell_price"), s.get("tp"), s.get("tp_price"), s.get("target"))),
        "buy_order_id": _first(
            s.get("buy_order_id"), s.get("open_buy_id"), s.get("buy_odno"), s.get("odno_buy")
        ),
        "sell_order_id": _first(
            s.get("sell_order_id"), s.get("open_sell_id"), s.get("sell_odno"), s.get("odno_sell")
        ),
        "repeats_done": s.get("repeats_done"),
        "date": s.get("date") or s.get("filled_at") or s.get("opened_at"),
        "holding": _slot_is_holding(s),
    }


def _positions_from_slots(positions_raw: Any) -> tuple[list[dict], list[dict], dict]:
    raw = positions_raw if isinstance(positions_raw, dict) else {}
    slots_in = _as_list(raw, "slots")
    if not slots_in and isinstance(positions_raw, list):
        slots_in = [x for x in positions_raw if isinstance(x, dict)]
    slots = [_normalize_slot(s) for s in slots_in]
    holdings = []
    for s in slots:
        if not s.get("holding"):
            continue
        holdings.append(
            {
                "buy_price": s.get("buy_price"),
                "qty": s.get("qty") or 1,
                "date": s.get("date") or "",
                "sell_order_id": s.get("sell_order_id"),
                "sell_price": s.get("sell_price"),
                "slot": s.get("slot"),
                "status": s.get("status"),
                "repeats_done": s.get("repeats_done"),
            }
        )
    # SSOT also has positions[] lots (may exist alongside slots)
    if not holdings:
        for p in _as_list(raw, "positions"):
            buy = num(_first(p.get("buy_price"), p.get("avg_price"), p.get("price")))
            qty = num(_first(p.get("qty"), p.get("quantity"))) or 0
            if buy is None and qty <= 0:
                continue
            holdings.append(
                {
                    "buy_price": buy,
                    "qty": qty or 1,
                    "date": p.get("date") or "",
                    "sell_order_id": _first(p.get("sell_order_id"), p.get("open_sell_id")),
                    "sell_price": num(p.get("sell_price")),
                    "slot": _slot_id(p),
                    "status": p.get("status"),
                }
            )
    # Top-level qty / avg_price when no lots listed
    if not holdings:
        avg = num(raw.get("avg_price"))
        qty = num(raw.get("qty"))
        if avg is not None and qty and qty > 0:
            holdings.append({"buy_price": avg, "qty": qty, "date": "", "slot": None, "status": "open"})
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    if raw.get("qty") is not None:
        meta = dict(meta)
        meta.setdefault("qty", raw.get("qty"))
        meta.setdefault("avg_price", raw.get("avg_price"))
    return holdings, slots, meta


def _iter_plan_sides(plan: dict) -> list[tuple[str, dict]]:
    mapping = (
        ("buys", "BUY"),
        ("sells", "SELL"),
        ("tps", "SELL"),
        ("plan_buys", "BUY"),
        ("plan_sells", "SELL"),
        ("plan_tps", "SELL"),
        ("morning_buys", "BUY"),
        ("buy_orders", "BUY"),
        ("sell_orders", "SELL"),
        ("pending_buys", "BUY"),
        ("pending_sells", "SELL"),
        ("open_buys", "BUY"),
        ("open_sells", "SELL"),
        ("buy_plan", "BUY"),
        ("sell_plan", "SELL"),
    )
    out: list[tuple[str, dict]] = []
    for key, side in mapping:
        for item in plan.get(key) or []:
            if isinstance(item, dict):
                out.append((side, item))
    # Generic orders[] with side field
    for item in plan.get("orders") or []:
        if isinstance(item, dict):
            out.append((str(item.get("side") or "BUY").upper(), item))
    return out


def _plan_to_orders(plan: dict) -> list[dict]:
    orders = []
    for side, item in _iter_plan_sides(plan):
        status = str(item.get("status") or item.get("state") or "planned").lower()
        if status in ("filled", "canceled", "cancelled", "done", "closed"):
            continue
        price = num(_first(item.get("price"), item.get("limit"), item.get("buy_price"), item.get("sell_price")))
        orders.append(
            {
                "order_id": _first(item.get("order_id"), item.get("odno"), item.get("id")) or "",
                "side": str(item.get("side") or side).upper(),
                "price": price,
                "qty": num(_first(item.get("qty"), item.get("quantity"))) or 1,
                "status": item.get("status") or item.get("state") or "planned",
                "client_tag": item.get("tag") or item.get("client_tag") or f"plan_{side}",
                "slot": _slot_id(item),
                "source": "plan.json",
            }
        )
    return orders


def _orders_from_orders_state(raw: dict) -> list[dict]:
    rows = raw.get("open_orders") or raw.get("orders") or raw.get("all_orders") or []
    open_orders = []
    for o in rows:
        if not isinstance(o, dict):
            continue
        status = str(o.get("status") or "open").lower()
        if status not in ("open", "pending", "canceling", "planned", "working"):
            continue
        open_orders.append(
            {
                "order_id": o.get("order_id") or o.get("odno") or "",
                "side": str(o.get("side") or "").upper(),
                "price": num(o.get("price")),
                "qty": num(o.get("qty")) or 1,
                "status": o.get("status") or "open",
                "client_tag": o.get("client_tag") or o.get("tag"),
                "slot": _slot_id(o),
                "source": "orders_state.json",
                "updated_at": o.get("updated_at") or o.get("created_at"),
            }
        )
    return open_orders


def _normalize_rt(rt: dict) -> Optional[dict]:
    buy = num(_first(rt.get("buy_price"), rt.get("buy")))
    sell = num(_first(rt.get("sell_price"), rt.get("sell")))
    qty = num(rt.get("qty")) or 1.0
    pnl = num(_first(rt.get("pnl"), rt.get("pnl_gross")))
    if pnl is None and buy is not None and sell is not None:
        pnl = round((sell - buy) * qty, 2)
    if buy is None and sell is None and pnl is None:
        return None
    return {
        "buy_price": buy,
        "sell_price": sell,
        "qty": qty,
        "pnl": round(pnl, 2) if pnl is not None else None,
        "sell_ts": rt.get("sell_ts") or rt.get("tmd") or rt.get("sell_tmd"),
        "slot": _slot_id(rt),
    }


def _fills_from_ledger(led: dict) -> list[dict]:
    meta = led.get("meta") if isinstance(led.get("meta"), dict) else {}
    raw = []
    for key in ("fills", "today_fills"):
        for src in (led, meta):
            v = src.get(key)
            if isinstance(v, list):
                raw.extend(x for x in v if isinstance(x, dict))
    # SSOT: plan_buys / tps on the ledger are planned, not fills — skip as trades
    today = str(led.get("session_date") or led.get("date") or now_seoul().strftime("%Y-%m-%d"))
    trades = []
    seen: set[str] = set()
    for f in raw:
        side = str(f.get("side") or "").upper()
        px = num(_first(f.get("price"), f.get("fill_price")))
        qty = num(_first(f.get("qty"), f.get("quantity"))) or 1
        odno = _first(f.get("odno"), f.get("order_id"))
        tmd = str(f.get("tmd") or f.get("time") or "").zfill(6)[-6:] if f.get("tmd") or f.get("time") else ""
        key = f"{odno}|{side}|{px}|{qty}|{tmd}"
        if key in seen:
            continue
        seen.add(key)
        ts = f.get("ts")
        if not ts and tmd and tmd != "000000":
            ts = f"{today}T{tmd[:2]}:{tmd[2:4]}:{tmd[4:6]}+09:00"
        trades.append(
            {
                "ts": ts,
                "kind": "buy_fill" if side == "BUY" else ("sell_fill" if side == "SELL" else "fill"),
                "side": side or None,
                "price": px,
                "qty": qty,
                "order_id": odno,
                "tag": f"{side} {qty}@{px} odno={odno}".strip(),
                "source": "day_ledger",
                "file": "day_ledger.json",
                "slot": _slot_id(f),
            }
        )
    trades.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return trades


def _pair_fills_to_rts(fills: list[dict]) -> list[dict]:
    """Pair fills by slot_id, then by TP offsets (+50/+75). No leftover FIFO."""
    buys = [dict(f) for f in fills if str(f.get("side") or "").upper() == "BUY"]
    sells = [dict(f) for f in fills if str(f.get("side") or "").upper() == "SELL"]
    used_b: set[int] = set()
    rts: list[dict] = []

    def _take_buy(pred) -> Optional[dict]:
        for i, b in enumerate(buys):
            if i in used_b:
                continue
            if pred(b):
                used_b.add(i)
                return b
        return None

    for s in sells:
        sid = _slot_id(s)
        sp = num(s.get("price"))
        sq = num(s.get("qty")) or 1
        b = None
        if sid is not None:
            b = _take_buy(lambda x, sid=sid: _slot_id(x) == sid)
        if b is None and sp is not None:
            def _tp_match(x, sp=sp):
                bp = num(x.get("price"))
                if bp is None:
                    return False
                return any(abs((sp - bp) - off) < 1e-6 for off in _TP_OFFSETS)
            b = _take_buy(_tp_match)
        if b is None:
            continue
        bp = num(b.get("price"))
        bq = num(b.get("qty")) or 1
        qty = min(sq, bq)
        pnl = None if bp is None or sp is None else round((sp - bp) * qty, 2)
        rts.append(
            {
                "buy_price": bp,
                "sell_price": sp,
                "qty": qty,
                "pnl": pnl,
                "sell_ts": s.get("ts"),
                "slot": sid if sid is not None else _slot_id(b),
            }
        )
    return rts


def _call_ops_fn(fn, led: dict) -> Any:
    for args, kwargs in (
        ((led,), {}),
        ((), {"led": led}),
        ((), {"ledger": led}),
        ((), {}),
    ):
        try:
            return fn(*args, **kwargs)
        except TypeError:
            continue
        except Exception:
            return None
    return None


def _pnl_from_ops_text(text: str) -> Optional[dict]:
    """Best-effort parse of ops_summary.build_text() Korean/ASCII PnL lines."""
    if not text or not isinstance(text, str):
        return None
    import re

    def _grab(*pats: str) -> Optional[float]:
        for pat in pats:
            m = re.search(pat, text)
            if not m:
                continue
            raw = m.group(1).replace(",", "").replace("원", "")
            n = num(raw)
            if n is not None:
                return n
        return None

    gross = _grab(
        r"총차익[^+\-\d]*([+\-]?\d[\d,]*)",
        r"realized_gross[^+\-\d]*([+\-]?\d[\d,]*)",
        r"실현\s*\(총[^)]*\)[^+\-\d]*([+\-]?\d[\d,]*)",
    )
    net = _grab(
        r"순익[^+\-\d]*([+\-]?\d[\d,]*)",
        r"실현\s*\(순[^)]*\)[^+\-\d]*([+\-]?\d[\d,]*)",
        r"realized_net[^+\-\d]*([+\-]?\d[\d,]*)",
    )
    realized = _grab(
        r"실현(?:손익| PnL|pnl)?[^+\-\d]*([+\-]?\d[\d,]*)",
        r"\bPnL[^+\-\d]*([+\-]?\d[\d,]*)",
    )
    if gross is None and net is None and realized is None:
        return None
    out: dict[str, Any] = {"source": "ops_summary.build_text"}
    if gross is not None:
        out["realized_gross"] = gross
    if net is not None:
        out["realized_net_est"] = net
        out["realized"] = net
    elif realized is not None:
        out["realized"] = realized
        if gross is None:
            out["realized_gross"] = realized
    snippet = "\n".join(text.splitlines()[:12])[:400]
    out["ops_summary_text"] = snippet
    return out


def _try_ops_summary_pnl(root: Path, led: dict) -> Optional[dict]:
    """Prefer 091170 ops_summary helpers, including build_text() layout/PnL."""
    path = root / "ops_summary.py"
    if not path.exists():
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("ops_summary_091170", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    for name in ("ledger_pnl", "compute_pnl", "pnl_from_ledger", "build_pnl"):
        fn = getattr(mod, name, None)
        if not callable(fn):
            continue
        out = _call_ops_fn(fn, led)
        if isinstance(out, dict) and (
            out.get("realized_gross") is not None or out.get("realized") is not None or out.get("round_trips")
        ):
            return out
    # SSOT: human layout / PnL logic lives in build_text()
    fn = getattr(mod, "build_text", None)
    if callable(fn):
        out = _call_ops_fn(fn, led)
        if isinstance(out, dict) and (
            out.get("realized_gross") is not None or out.get("realized") is not None or out.get("round_trips")
        ):
            return out
        if isinstance(out, str):
            parsed = _pnl_from_ops_text(out)
            if parsed:
                return parsed
    return None


def _kimpro_config_summary(cfg: dict, plan: dict) -> dict:
    """367380-style summary plus 김프로 slot/cap defaults from the operator SSOT."""
    session = cfg.get("session") if isinstance(cfg.get("session"), dict) else {}
    start = session.get("start") or _KIMPRO["session_start"]
    end = session.get("end") or _KIMPRO["session_end"]
    caps = plan.get("caps") if isinstance(plan, dict) and isinstance(plan.get("caps"), dict) else {}
    summary = config_summary(cfg if isinstance(cfg, dict) else {})
    summary["session"] = f"{start}–{end}"
    summary["levels"] = summary.get("levels") or _KIMPRO["slot_count"]
    summary["slots"] = summary.get("slots") or _KIMPRO["slot_count"]
    summary["qty_per_order"] = summary.get("qty_per_order") or _KIMPRO["qty_per_slot"]
    summary["slot_offsets"] = (
        cfg.get("slot_offsets") or (cfg.get("grid") or {}).get("slot_offsets") or _KIMPRO["slot_offsets"]
    )
    summary["tp_offsets"] = cfg.get("tp_offsets") or (cfg.get("grid") or {}).get("tp_offsets") or _KIMPRO["tp_offsets"]
    summary["tp_display"] = summary.get("tp_display") or "+50/+75/+75/+75"
    summary["spacing_display"] = summary.get("spacing_display") or "−75/−180/−330/−525"
    summary["daily_buy_cap"] = caps.get("daily_buy") or caps.get("daily_buy_cap") or _KIMPRO["daily_buy_cap"]
    summary["order_cap"] = caps.get("order") or caps.get("order_cap") or _KIMPRO["order_cap"]
    summary["sibling_cash_reserve"] = (
        caps.get("sibling_cash_reserve") or cfg.get("sibling_cash_reserve") or _KIMPRO["sibling_cash_reserve"]
    )
    return summary


def _ledger_pnl(led: dict, *, root: Optional[Path] = None) -> dict:
    """PnL from realized_*/round_trips, ops_summary, or slot/TP-paired fills."""
    ops = _try_ops_summary_pnl(root, led) if root is not None else None
    if ops:
        led = {**led, **{k: ops[k] for k in ops if k in (
            "realized_gross", "realized_net_est", "realized_net", "realized",
            "round_trips", "fees_day_est", "tax_est",
        )}}
        ops_text = ops.get("ops_summary_text")
    else:
        ops_text = None

    rts_raw = led.get("round_trips") or []
    rts = []
    if isinstance(rts_raw, list):
        for rt in rts_raw:
            if isinstance(rt, dict):
                n = _normalize_rt(rt)
                if n:
                    rts.append(n)

    fills = _fills_from_ledger(led)
    if not rts and fills:
        paired = _pair_fills_to_rts(fills)
        if paired:
            rts = paired

    gross = num(led.get("realized_gross"))
    net = num(_first(led.get("realized_net_est"), led.get("realized_net"), led.get("realized")))
    if gross is None and rts:
        gross = round(sum(float(r["pnl"] or 0) for r in rts), 2)
    if net is None and gross is not None:
        fees = num(led.get("fees_day_est")) or 0.0
        tax = num(led.get("tax_est")) or 0.0
        net = round(gross - fees - tax, 2)
    if net is None and not rts:
        net = 0.0
        if gross is None:
            gross = 0.0

    source = None
    if ops:
        source = str(ops.get("source") or "ops_summary")
    elif led.get("realized_gross") is not None or led.get("realized_net_est") is not None:
        source = "day_ledger"
    elif rts and any(r.get("slot") is not None for r in rts):
        source = "day_ledger.fills_slot"
    elif rts:
        source = "day_ledger.round_trips" if rts_raw else "day_ledger.fills_tp"
    else:
        source = "day_ledger.empty"

    meta = led.get("meta") if isinstance(led.get("meta"), dict) else {}
    return {
        "realized": float(net if net is not None else 0.0),
        "realized_gross": float(gross) if gross is not None else 0.0,
        "fees_day_est": num(led.get("fees_day_est")),
        "tax_est": num(led.get("tax_est")),
        "round_trips": rts[-20:],
        "round_trip_count": len(rts),
        "pnl_source": source,
        "buy_fill_count": meta.get("buy_fill_count"),
        "sell_fill_count": meta.get("sell_fill_count"),
        "buy_fill_qty": meta.get("buy_fill_qty"),
        "sell_fill_qty": meta.get("sell_fill_qty"),
        "capital_used": num(led.get("capital_used")),
        "ops_summary_text": ops_text,
    }


def _count_fills(trades: list[dict], pnl: dict) -> dict:
    if pnl.get("buy_fill_count") is not None and pnl.get("sell_fill_count") is not None:
        return pnl
    bc = bq = sc = sq = 0
    for t in trades:
        side = str(t.get("side") or "").upper()
        q = num(t.get("qty")) or 1
        if side == "BUY":
            bc += 1
            bq += q
        elif side == "SELL":
            sc += 1
            sq += q
    if pnl.get("buy_fill_count") is None:
        pnl["buy_fill_count"] = bc
    if pnl.get("sell_fill_count") is None:
        pnl["sell_fill_count"] = sc
    if pnl.get("buy_fill_qty") is None:
        pnl["buy_fill_qty"] = int(bq) if bq == int(bq) else bq
    if pnl.get("sell_fill_qty") is None:
        pnl["sell_fill_qty"] = int(sq) if sq == int(sq) else sq
    return pnl


def _pick_base(*objs: Any) -> Any:
    keys = ("base", "session_base", "session_base_0905", "base_price", "ref_price", "ref")
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for k in keys:
            if obj.get(k) is not None:
                return obj.get(k)
        meta = obj.get("meta")
        if isinstance(meta, dict):
            for k in keys:
                if meta.get(k) is not None:
                    return meta.get(k)
    return None


def _pick_price(*objs: Any) -> tuple[Any, Optional[str]]:
    keys = (
        ("last_price", "last_price"),
        ("last", "last"),
        ("kis_last", "kis_last"),
        ("ref_price", "ref_price"),
        ("price", "price"),
    )
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for key, src in keys:
            if obj.get(key) is not None:
                return obj.get(key), src
        meta = obj.get("meta")
        if isinstance(meta, dict):
            for key, src in keys:
                if meta.get(key) is not None:
                    return meta.get(key), f"meta.{src}"
    return None, None


def _pick_cash(*objs: Any) -> tuple[Any, Optional[str]]:
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for key in ("cash", "kis_cash", "dnca_tot_amt"):
            if obj.get(key) is not None:
                return obj.get(key), key
        meta = obj.get("meta")
        if isinstance(meta, dict) and meta.get("cash") is not None:
            return meta.get("cash"), "meta.cash"
    return None, None


def _safety_frozen(*objs: Any) -> tuple[bool, list]:
    reasons: list = []
    frozen = False
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for key in ("safety_frozen", "frozen", "is_frozen"):
            if obj.get(key) is True:
                frozen = True
        meta = obj.get("meta")
        if isinstance(meta, dict):
            if meta.get("safety_frozen") is True or meta.get("frozen") is True:
                frozen = True
            r = meta.get("safety_reasons") or meta.get("freeze_reasons")
            if isinstance(r, list):
                reasons.extend(str(x) for x in r)
        r = obj.get("safety_reasons") or obj.get("freeze_reasons") or obj.get("reasons")
        if isinstance(r, list) and obj.get("safety_frozen") is True:
            reasons.extend(str(x) for x in r)
    return bool(frozen), reasons


def _ledger_session_date(led: dict) -> str:
    """091170 uses session_date; 367380-style files use date."""
    return str(led.get("session_date") or led.get("date") or "")


def _day_record_from_ledger(led: dict, *, source: str, day: date) -> Optional[dict]:
    """One day's realized from an explicit ledger. Never reuse a file across other days."""
    if not isinstance(led, dict):
        return None
    key = _ledger_session_date(led)
    if key:
        if key != day.isoformat():
            return None
    else:
        # Undated live file counts only as calendar-today — do not copy onto other days.
        if day != now_seoul().date():
            return None
    gross = num(led.get("realized_gross"))
    net = num(_first(led.get("realized_net_est"), led.get("realized_net"), led.get("realized")))
    rts_raw = led.get("round_trips") or []
    rts: list[dict] = []
    if isinstance(rts_raw, list):
        for rt in rts_raw:
            if isinstance(rt, dict):
                n = _normalize_rt(rt)
                if n:
                    rts.append(n)
    if gross is None and rts:
        gross = round(sum(float(r.get("pnl") or 0) for r in rts), 2)
    if gross is None and net is None:
        # Today's 김프로 ledger often has fills[] without realized_* / archive.
        fills = _fills_from_ledger(led)
        paired = _pair_fills_to_rts(fills) if fills else []
        if paired:
            rts = paired
            gross = round(sum(float(r.get("pnl") or 0) for r in paired), 2)
    if gross is None and net is None:
        return None
    if net is None and gross is not None:
        fees = num(led.get("fees_day_est")) or 0.0
        tax = num(led.get("tax_est")) or 0.0
        net = round(gross - fees - tax, 2)
    return {
        "date": day.isoformat(),
        "available": True,
        "realized_gross": gross,
        "realized_net_est": net,
        "fees_day_est": num(led.get("fees_day_est")),
        "tax_est": num(led.get("tax_est")),
        "round_trip_count": len(rts) if rts else (len(rts_raw) if isinstance(rts_raw, list) else None),
        "eod": bool(led.get("eod") or led.get("session_ended")),
        "source": source,
        "gap": False,
    }


def _cumulative_091170(root: Path, n_days: int = 3) -> Optional[dict]:
    """Archive days plus root day_ledger.json when its session_date matches.

    Missing archive days stay gaps (available=False). Never invents history.
    """
    try:
        from cumulative_pnl import last_n_trading_days
    except Exception:
        # dashboard/ may not be on path
        try:
            import sys

            dash = str(Path(__file__).resolve().parent.parent)
            if dash not in sys.path:
                sys.path.insert(0, dash)
            from cumulative_pnl import last_n_trading_days
        except Exception:
            return None

    days = last_n_trading_days(n_days)
    per_day = []
    for d in days:
        rec = None
        ymd = d.strftime("%Y%m%d")
        for path in (
            root / "ledger_archive" / f"day_ledger-{ymd}.json",
            root / "logs" / f"day_ledger-{ymd}.json",
            root / f"day_ledger-{ymd}.json",
        ):
            rec = _day_record_from_ledger(read_json(path, None), source=path.name, day=d)
            if rec:
                break
        if rec is None:
            current = read_json(root / "day_ledger.json", None)
            rec = _day_record_from_ledger(current, source="day_ledger.json", day=d)
        if rec is None:
            rec = {
                "date": d.isoformat(),
                "available": False,
                "realized_gross": None,
                "realized_net_est": None,
                "fees_day_est": None,
                "tax_est": None,
                "round_trip_count": None,
                "eod": False,
                "source": None,
                "gap": True,
            }
        per_day.append(rec)

    available_days = sum(1 for r in per_day if r.get("available"))
    if available_days == 0:
        return None

    gross_sum = sum(float(r["realized_gross"] or 0) for r in per_day if r.get("available"))
    net_sum = sum(float(r["realized_net_est"] or 0) for r in per_day if r.get("available"))
    fees_sum = sum(float(r.get("fees_day_est") or 0) for r in per_day if r.get("available"))
    tax_sum = sum(float(r.get("tax_est") or 0) for r in per_day if r.get("available"))
    gaps = [r["date"] for r in per_day if not r.get("available")]
    return {
        "n_days": n_days,
        "days": [d.isoformat() for d in days],
        "per_day": per_day,
        "available_days": available_days,
        "gaps": gaps,
        "realized_gross": round(gross_sum, 2),
        "realized_net_est": round(net_sum, 2),
        "fees_est": round(fees_sum, 2),
        "tax_est": round(tax_sum, 2),
        "complete": available_days == n_days and not gaps,
    }


def build_091170(*, root: Path, try_kis: bool = False, kis_quote: Optional[dict] = None) -> dict:
    root = Path(root)
    if not root.is_dir():
        return empty_bot(
            bot_id=BOT_ID,
            schema=SCHEMA,
            root=root,
            code="root_missing",
            message=f"091170 bot path not found: {root} (set GRID_BOT_091170_ROOT)",
        )

    try:
        return _build_091170(root=root, try_kis=try_kis, kis_quote=kis_quote)
    except Exception as e:  # noqa: BLE001
        err = empty_bot(
            bot_id=BOT_ID,
            schema=SCHEMA,
            root=root,
            code=type(e).__name__,
            message=str(e)[:300],
        )
        err["files_present"] = list_present_files(root, list(_WATCH_FILES))
        return err


def _build_091170(*, root: Path, try_kis: bool, kis_quote: Optional[dict]) -> dict:
    cfg = read_json(root / "config.json", {}) or {}
    last_blob = read_json(root / "last_price.json", {}) or {}
    positions_raw = read_json(root / "positions.json", {}) or {}
    plan = read_json(root / "plan.json", {}) or {}
    led = read_json(root / "day_ledger.json", {}) or {}
    orders_state = read_json(root / "orders_state.json", {}) or {}

    holdings, slots, pos_meta = _positions_from_slots(positions_raw)
    plan_present = bool(plan)
    open_orders = _orders_from_orders_state(orders_state)
    if not open_orders:
        # plan morning buys / tps when orders_state has no working orders
        extra = []
        if isinstance(led, dict):
            extra.extend(_plan_to_orders({"buys": led.get("plan_buys") or [], "tps": led.get("tps") or led.get("plan_tps") or []}))
        if not extra:
            extra = _plan_to_orders(plan) if isinstance(plan, dict) else []
        open_orders = extra

    trades = _fills_from_ledger(led) if isinstance(led, dict) else []
    pnl = _ledger_pnl(led, root=root) if isinstance(led, dict) else {
        "realized": 0.0,
        "realized_gross": 0.0,
        "round_trips": [],
        "round_trip_count": 0,
        "pnl_source": "missing_ledger",
    }
    pnl = _count_fills(trades, pnl)
    if pnl.get("capital_used") is None and isinstance(led, dict):
        pnl["capital_used"] = num(led.get("daily_buy_notional"))

    last_price = None
    price_source = None
    cash = None
    cash_source = None
    kis = {"ok": False, "skipped": not try_kis, "error": None, "cached": False}

    if kis_quote:
        kis = {
            "ok": bool(kis_quote.get("ok")),
            "error": kis_quote.get("error"),
            "cached": kis_quote.get("cached"),
            "skipped": False,
        }
        if kis_quote.get("price") is not None:
            last_price = kis_quote["price"]
            price_source = "kis"
        if kis_quote.get("cash") is not None:
            cash = kis_quote["cash"]
            cash_source = "kis"

    if last_price is None:
        px, src = _pick_price(last_blob)
        if px is None:
            px, src = _pick_price(plan, led, positions_raw, pos_meta, orders_state, cfg)
        elif src:
            src = "last_price.json"
        if px is not None:
            try:
                last_price = int(float(px))
                price_source = src
            except (TypeError, ValueError):
                last_price = None

    if cash is None:
        c, src = _pick_cash(plan, led, positions_raw, pos_meta, orders_state)
        if c is not None:
            try:
                cash = float(c)
                cash_source = src
            except (TypeError, ValueError):
                cash = None

    mtm = 0.0
    cost_basis = 0.0
    for p in holdings:
        bp = num(p.get("buy_price")) or 0.0
        qty = num(p.get("qty")) or 0.0
        cost_basis += bp * qty
        if last_price is not None:
            mtm += (float(last_price) - bp) * qty

    capital = pnl.get("capital_used")
    if capital is None:
        capital = cost_basis
        for rt in pnl.get("round_trips") or []:
            bp = num(rt.get("buy_price")) or 0.0
            q = num(rt.get("qty")) or 1.0
            capital = (capital or 0) + bp * q if cost_basis == 0 else capital
        if cost_basis and pnl.get("round_trips"):
            capital = float(cost_basis) + sum(
                (num(rt.get("buy_price")) or 0) * (num(rt.get("qty")) or 1)
                for rt in (pnl.get("round_trips") or [])
            )

    realized_for_return = pnl.get("realized_gross")
    if realized_for_return is None:
        realized_for_return = pnl.get("realized") or 0.0
    live_return_pct = None
    if capital and float(capital) > 0:
        live_return_pct = round(((mtm + float(realized_for_return)) / float(capital)) * 100, 4)

    frozen, reasons = _safety_frozen(positions_raw, pos_meta, plan, led, orders_state, cfg, last_blob)
    if not reasons and isinstance(led, dict) and led.get("freeze_reasons"):
        fr = led.get("freeze_reasons")
        if isinstance(fr, list):
            reasons = [str(x) for x in fr]
        elif fr:
            reasons = [str(fr)]
    base = _pick_base(last_blob, plan, led, pos_meta, positions_raw, cfg)
    symbol = cfg.get("symbol") or BOT_ID
    name = cfg.get("symbol_name") or SYMBOL_NAME
    session = cfg.get("session") if isinstance(cfg.get("session"), dict) else {}
    if not session.get("start") or not session.get("end"):
        session = {
            "start": session.get("start") or _KIMPRO["session_start"],
            "end": session.get("end") or _KIMPRO["session_end"],
        }
    cum = _cumulative_091170(root)
    day_high = _first(last_blob.get("day_high"), led.get("day_high") if isinstance(led, dict) else None)
    ma20 = led.get("ma20") if isinstance(led, dict) else None
    ratchet = led.get("ratchet_steps") if isinstance(led, dict) else None

    pnl_out = {
        "realized": round(float(pnl.get("realized") or 0), 2),
        "realized_gross": round(float(pnl.get("realized_gross") or 0), 2),
        "fees_day_est": pnl.get("fees_day_est"),
        "tax_est": pnl.get("tax_est"),
        "pnl_source": pnl.get("pnl_source"),
        "mtm": round(mtm, 2),
        "total_est": round(float(pnl.get("realized") or 0) + mtm, 2),
        "cost_basis": round(cost_basis, 2),
        "capital_used_today": round(float(capital or 0), 2),
        "round_trip_count": pnl.get("round_trip_count") or 0,
        "round_trips": pnl.get("round_trips") or [],
        "open_buy_lots": sum(1 for o in open_orders if str(o.get("side") or "").upper() == "BUY"),
        "buy_fill_count": pnl.get("buy_fill_count"),
        "sell_fill_count": pnl.get("sell_fill_count"),
        "buy_fill_qty": pnl.get("buy_fill_qty"),
        "sell_fill_qty": pnl.get("sell_fill_qty"),
        "live_return_pct": live_return_pct,
        "return_pct": live_return_pct,
        "cumulative_3d": cum,
        "ops_summary_text": pnl.get("ops_summary_text"),
    }

    plan_summary = {
        "present": plan_present,
        "base": num(base) if base is not None else base,
        "buy_count": sum(1 for o in open_orders if str(o.get("side") or "").upper() == "BUY"),
        "sell_count": sum(1 for o in open_orders if str(o.get("side") or "").upper() == "SELL"),
        "caps": plan.get("caps") if isinstance(plan, dict) else None,
        "placed_order_ids_count": len(plan.get("placed_order_ids") or []) if isinstance(plan, dict) else 0,
        "keys": sorted(plan.keys())[:20] if isinstance(plan, dict) else [],
    }

    return redact(
        {
            "id": BOT_ID,
            "schema": SCHEMA,
            "ok": True,
            "error": None,
            "root": str(root),
            "root_exists": True,
            "overview": {
                "symbol": symbol,
                "symbol_name": name,
                "last_price": last_price,
                "price_source": price_source,
                "cash": cash,
                "cash_source": cash_source,
                "config_summary": _kimpro_config_summary(
                    cfg if isinstance(cfg, dict) else {},
                    plan if isinstance(plan, dict) else {},
                ),
                "in_session": in_session(session if isinstance(session, dict) else {}),
                "mode": orders_state.get("mode") or plan.get("mode") or cfg.get("mode"),
                "safety_frozen": frozen,
                "safety_reasons": reasons,
                "base": num(base) if base is not None else base,
                "day_high": num(day_high) if day_high is not None else day_high,
                "ma20": num(ma20) if ma20 is not None else ma20,
                "ratchet_steps": ratchet,
                "ref_price": num(
                    last_blob.get("last")
                    or orders_state.get("ref_price")
                    or plan.get("ref_price")
                    or plan.get("last")
                ),
            },
            "live_approved": live_approved(root),
            "kis": kis,
            "open_orders": open_orders,
            "positions": holdings,
            "positions_meta": {
                "count": len(holdings),
                "total_qty": int(sum(num_or_zero(p.get("qty")) for p in holdings)),
                "slot_count": len(slots),
            },
            "slots": slots,
            "plan": plan_summary,
            "trades": trades[:100],
            "pnl": pnl_out,
            "files_present": list_present_files(root, list(_WATCH_FILES)),
            "ledger_date": (led.get("session_date") or led.get("date")) if isinstance(led, dict) else None,
        }
    )
