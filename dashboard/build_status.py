#!/usr/bin/env python3
"""Aggregate grid-bot status for the local dashboard.

Reads config / positions / orders / logs / backtest CSVs.
Optionally queries KIS for last price + cash (read-only, no orders).
Never prints secrets or tokens.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
BACKTEST = ROOT / "backtest"
SEOUL = timezone(timedelta(hours=9))

# Cache KIS lookups so 15s refresh does not hammer the API.
_KIS_CACHE: dict[str, Any] = {"ts": 0.0, "price": None, "cash": None, "error": None}
_KIS_TTL_SEC = 30.0


def _now_seoul() -> datetime:
    return datetime.now(SEOUL)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def _load_config() -> dict:
    return _read_json(ROOT / "config.json", {}) or {}


def _live_approved() -> dict:
    path = ROOT / "LIVE_APPROVED"
    today = _now_seoul().strftime("%Y-%m-%d")
    if not path.exists():
        return {"present": False, "content": None, "valid_today": False, "path": str(path)}
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        content = ""
    # Expected: Seoul YYYY-MM-DD
    valid = content == today or content.startswith(today)
    return {
        "present": True,
        "content": content[:32] if content else "",
        "valid_today": valid,
        "today": today,
        "path": str(path),
    }


def _iter_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _list_log_files() -> list[Path]:
    if not LOGS.is_dir():
        return []
    files = sorted(LOGS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files


FILL_KINDS = {
    "engine.buy_fill",
    "engine.sell_fill",
    "broker.fill",
    "shadow.intent",
    "shadow.fill",
    "live.fill",
    "buy_fill",
    "sell_fill",
}
PRICE_KINDS = {
    "session.tick",
    "sim.tick",
    "kis.price",
}
BALANCE_KINDS = {"session.balance"}


def _parse_fills_and_meta(max_events: int = 200) -> dict:
    """Scan recent log files for fills, shadow/live events, last price, cash."""
    trades: list[dict] = []
    recent: list[dict] = []
    last_price: Optional[int] = None
    last_price_src: Optional[str] = None
    cash: Optional[float] = None
    cash_src: Optional[str] = None
    mode_hint: Optional[str] = None

    for path in _list_log_files()[:12]:
        for rec in _iter_jsonl(path):
            kind = rec.get("kind") or rec.get("action") or ""
            ts = rec.get("ts")
            data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            msg = rec.get("message") or ""

            # Price from ticks
            if kind in PRICE_KINDS or "price=" in msg:
                px = data.get("price")
                if px is None:
                    m = re.search(r"price[=:]?\s*(\d+)", msg)
                    if m:
                        px = int(m.group(1))
                if px is not None:
                    try:
                        px_i = int(px)
                        if last_price is None or (ts and (last_price_src is None or ts >= (last_price_src or ""))):
                            # Prefer chronologically latest; we'll overwrite as we go newer files first
                            # Since files are newest-first, only set if not yet set from a newer file
                            pass
                    except (TypeError, ValueError):
                        px_i = None
                    else:
                        if last_price is None:
                            last_price = px_i
                            last_price_src = f"{path.name}:{kind}"

            # Cash from balance
            if kind in BALANCE_KINDS and cash is None:
                c = data.get("cash")
                if c is not None:
                    try:
                        cash = float(c)
                        cash_src = f"{path.name}:{kind}"
                    except (TypeError, ValueError):
                        pass

            # Mode hints
            if "dry" in str(data.get("dry", "")).lower() or "dry" in kind or "shadow" in kind:
                if mode_hint is None:
                    mode_hint = "dry/shadow"
            if kind.startswith("kis.") or "live" in path.name:
                if mode_hint is None:
                    mode_hint = "live-related"

            # Fills / shadow / live trade-like events
            is_fill = False
            side = None
            price = None
            qty = None
            order_id = None
            tag = None
            source = "log"

            if kind == "engine.buy_fill":
                is_fill = True
                side = "BUY"
                pos = data.get("pos") or {}
                price = pos.get("buy_price") or data.get("price")
                qty = pos.get("qty") or data.get("qty") or 1
                order_id = data.get("order_id")
                source = "engine"
            elif kind == "engine.sell_fill":
                is_fill = True
                side = "SELL"
                # message often: sold @25000 ...
                m = re.search(r"@(\d+)", msg)
                price = int(m.group(1)) if m else data.get("price") or data.get("fill_price")
                qty = data.get("qty") or 1
                order_id = data.get("order_id")
                # try recover buy from removed dict in message
                mb = re.search(r"'buy_price':\s*(\d+)", msg)
                tag = f"buy={mb.group(1)}" if mb else None
                source = "engine"
            elif kind in ("buy_fill", "sell_fill", "kis.fill_resync"):
                is_fill = True
                side = rec.get("side") or ("BUY" if "buy" in kind else "SELL")
                if kind == "kis.fill_resync":
                    side = rec.get("side") or side
                price = rec.get("price") or data.get("price")
                qty = rec.get("qty") or data.get("qty") or 1
                order_id = rec.get("odno") or rec.get("order_id") or data.get("odno")
                tag = rec.get("message") or msg or f"{side} {qty}@{price} odno={order_id}"
                source = "kis" if (
                    rec.get("source") in ("kis_daily_ccld", "midday_kis_resync")
                    or "fills-live" in path.name
                    or "fills-sync" in path.name
                    or kind == "kis.fill_resync"
                ) else "log"
            elif kind == "broker.fill":
                is_fill = True
                side = data.get("side")
                price = data.get("fill_price") or data.get("price")
                qty = data.get("fill_qty") or data.get("qty") or 1
                order_id = data.get("order_id")
                tag = data.get("client_tag")
                linked = data.get("linked_buy_price")
                if linked and not tag:
                    tag = f"buy={linked}"
                elif linked:
                    tag = f"{tag}|buy={linked}"
                source = "broker"
            elif kind in ("shadow.intent",) or str(rec.get("action", "")).startswith("shadow_"):
                action = rec.get("action") or data.get("action") or kind
                # Treat shadow_submit as shadow trade intent; include in recent + trades as shadow
                side = rec.get("side") or data.get("side")
                price = rec.get("price") or data.get("price")
                qty = rec.get("qty") or data.get("qty") or 1
                order_id = rec.get("order_id") or data.get("order_id")
                tag = rec.get("client_tag") or data.get("client_tag") or action
                source = "shadow"
                # Only count fill-like shadow events as trades; still show submits in recent
                if "fill" in str(action).lower():
                    is_fill = True
                recent.append(
                    {
                        "ts": ts,
                        "kind": kind or action,
                        "message": msg or f"{action} {side} {qty}@{price}",
                        "file": path.name,
                        "source": source,
                    }
                )
            elif kind.startswith("live") or str(rec.get("action", "")).startswith("live"):
                side = rec.get("side") or data.get("side")
                price = rec.get("price") or data.get("price") or data.get("fill_price")
                qty = rec.get("qty") or data.get("qty") or 1
                order_id = rec.get("order_id") or data.get("order_id")
                source = "live"
                if "fill" in kind.lower() or "fill" in str(rec.get("action", "")).lower():
                    is_fill = True

            if is_fill:
                trades.append(
                    {
                        "ts": ts,
                        "kind": kind,
                        "side": side,
                        "price": price,
                        "qty": qty,
                        "order_id": order_id,
                        "tag": tag,
                        "source": source,
                        "file": path.name,
                        "message": msg[:120] if msg else "",
                    }
                )

            # Keep a short recent event stream (non-tick noise reduced)
            if kind and kind not in ("sim.tick",) and not kind.endswith(".tick"):
                if kind not in ("shadow.intent",) and not str(rec.get("action", "")).startswith("shadow_"):
                    recent.append(
                        {
                            "ts": ts,
                            "kind": kind,
                            "message": (msg or "")[:160],
                            "file": path.name,
                        }
                    )

    # Prefer broker/kis/live fills over engine.*_fill for the same (file, order_id).
    # Keep distinct runs that reuse MOCK ids across different log files.
    source_rank = {"kis": 0, "broker": 0, "live": 0, "engine": 1, "shadow": 2, "log": 3}
    by_key: dict[tuple, dict] = {}
    extras: list[dict] = []
    for t in trades:
        oid = t.get("order_id")
        if not oid:
            extras.append(t)
            continue
        key = (t.get("file"), oid)
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = t
            continue
        pr = source_rank.get(prev.get("source") or "", 9)
        nr = source_rank.get(t.get("source") or "", 9)
        if nr < pr:
            by_key[key] = t
        elif nr == pr and not prev.get("side") and t.get("side"):
            by_key[key] = t
    uniq_trades = list(by_key.values()) + extras
    # Prefer today's live/kis fills ahead of MOCK/sim logs when present
    # Stable sort: newest first, then promote live/kis ahead of MOCK/sim
    uniq_trades.sort(key=lambda x: x.get("ts") or "", reverse=True)
    def _pri(x: dict) -> int:
        src = x.get("source") or ""
        oid = str(x.get("order_id") or "")
        file_n = str(x.get("file") or "")
        is_mock = (
            oid.startswith("MOCK")
            or src == "shadow"
            or file_n.startswith("run-")  # paper/demo run logs
        )
        if is_mock:
            return 2
        is_liveish = (
            src in ("kis", "broker", "live")
            or "fills-live" in file_n
            or "fills-sync" in file_n
            or "live-session" in file_n
            or src == "day_ledger"
        )
        return 0 if is_liveish else 1
    uniq_trades.sort(key=_pri)
    # If any live/kis trades exist for today, drop MOCK-heavy sim runs from the head list
    has_live = any(_pri(t) == 0 for t in uniq_trades)
    if has_live:
        filtered = [t for t in uniq_trades if _pri(t) < 2]
        if filtered:
            uniq_trades = filtered

    recent_sorted = sorted(recent, key=lambda x: x.get("ts") or "", reverse=True)

    return {
        "trades": uniq_trades[:max_events],
        "recent_events": recent_sorted[:80],
        "last_price_from_logs": last_price,
        "last_price_src": last_price_src,
        "cash_from_logs": cash,
        "cash_src": cash_src,
        "mode_hint": mode_hint,
        "log_files": [p.name for p in _list_log_files()[:8]],
    }


def _compute_realized_pnl(trades: list[dict]) -> dict:
    """Match BUY fills with later SELL fills (FIFO) for round-trip PnL."""
    buys: list[dict] = []
    round_trips: list[dict] = []
    realized = 0.0

    # Process chronological (oldest first)
    chron = sorted(trades, key=lambda x: x.get("ts") or "")
    for t in chron:
        side = (t.get("side") or "").upper()
        try:
            px = float(t.get("price") or 0)
            qty = float(t.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        if side == "BUY" and t.get("source") in ("engine", "broker", "live"):
            buys.append({"price": px, "qty": qty, "ts": t.get("ts"), "order_id": t.get("order_id")})
        elif side == "SELL" and t.get("source") in ("engine", "broker", "live"):
            remain = qty
            while remain > 0 and buys:
                b = buys[0]
                take = min(remain, b["qty"])
                pnl = (px - b["price"]) * take
                realized += pnl
                round_trips.append(
                    {
                        "buy_price": b["price"],
                        "sell_price": px,
                        "qty": take,
                        "pnl": round(pnl, 2),
                        "buy_ts": b["ts"],
                        "sell_ts": t.get("ts"),
                    }
                )
                b["qty"] -= take
                remain -= take
                if b["qty"] <= 0:
                    buys.pop(0)

    return {
        "realized_pnl": round(realized, 2),
        "round_trips": round_trips[-50:],
        "round_trip_count": len(round_trips),
        "open_buy_lots": len(buys),
    }


def _read_csv_top(path: Path, limit: int = 8) -> dict:
    if not path.exists():
        return {"present": False, "path": str(path), "rows": [], "headers": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = list(reader.fieldnames or [])
            rows = []
            for i, row in enumerate(reader):
                if i >= limit:
                    break
                # Keep numeric-looking values as strings for JSON simplicity; UI formats
                rows.append({k: row.get(k) for k in headers})
        return {
            "present": True,
            "path": str(path),
            "headers": headers,
            "rows": rows,
            "note": f"top {len(rows)} by file order (usually best return first)",
        }
    except OSError:
        return {"present": False, "path": str(path), "rows": [], "headers": [], "error": "read_failed"}


def _try_kis_quote(symbol: str, env_dv: str = "real") -> dict:
    """Optional read-only KIS last price + cash. Never places orders."""
    import time

    now = time.time()
    if now - float(_KIS_CACHE["ts"]) < _KIS_TTL_SEC and _KIS_CACHE["ts"]:
        return {
            "ok": _KIS_CACHE["price"] is not None or _KIS_CACHE["cash"] is not None,
            "price": _KIS_CACHE["price"],
            "cash": _KIS_CACHE["cash"],
            "error": _KIS_CACHE["error"],
            "cached": True,
        }

    result = {"ok": False, "price": None, "cash": None, "error": None, "cached": False}
    # Avoid mutating cwd / secrets — import from parent with allow_mutations=False
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from broker_kis import LiveKISBroker  # type: ignore

        broker = LiveKISBroker(symbol=symbol, allow_mutations=False, env_dv=env_dv, api_retry=1)
        broker.connect()
        try:
            result["price"] = int(broker.get_last_price())
        except Exception as e:  # noqa: BLE001
            result["error"] = f"price:{type(e).__name__}"
        try:
            bal = broker.inquire_balance_summary()
            if isinstance(bal, dict):
                cash = bal.get("cash")
                if cash is not None:
                    result["cash"] = float(cash)
        except Exception as e:  # noqa: BLE001
            err = result["error"] or ""
            result["error"] = (err + f";balance:{type(e).__name__}").strip(";")
        result["ok"] = result["price"] is not None or result["cash"] is not None
    except Exception as e:  # noqa: BLE001
        result["error"] = f"import/connect:{type(e).__name__}"

    _KIS_CACHE["ts"] = now
    _KIS_CACHE["price"] = result["price"]
    _KIS_CACHE["cash"] = result["cash"]
    _KIS_CACHE["error"] = result["error"]
    return result


def _count_today_log_sells(trades: list[dict]) -> int:
    """Count today's unique SELL fills (prefer price|qty fingerprint to merge LIVE-id vs odno)."""
    today = _now_seoul().strftime("%Y-%m-%d")
    today_compact = today.replace("-", "")
    # Primary: fills-live-TODAY.jsonl odnos
    fills_path = LOGS / f"fills-live-{today_compact}.jsonl"
    odnos: set[str] = set()
    px_keys: set[str] = set()
    if fills_path.exists():
        for rec in _iter_jsonl(fills_path):
            if str(rec.get("side") or "").upper() != "SELL":
                continue
            if rec.get("kind") not in ("kis.fill_resync", "sell_fill", "buy_fill", None, ""):
                # still accept side=SELL rows
                pass
            od = str(rec.get("odno") or rec.get("order_id") or "")
            px = rec.get("price")
            qty = rec.get("qty") or 1
            if od:
                odnos.add(od)
            if px is not None:
                px_keys.add(f"{int(px)}:{int(qty)}")
    # Secondary: today's broker/engine sells not already covered by price fingerprint
    for tr in trades or []:
        if str(tr.get("side") or "").upper() != "SELL":
            continue
        ts = str(tr.get("ts") or "")
        fname = str(tr.get("file") or "")
        if not (ts.startswith(today) or today_compact in fname):
            continue
        src = str(tr.get("source") or "")
        if src not in ("broker", "engine", "kis"):
            continue
        try:
            px = int(tr.get("price"))
            qty = int(tr.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        px_keys.add(f"{px}:{qty}")
    return max(len(odnos), len(px_keys))


def _maybe_refresh_ledger_from_kis(led: dict, log_sells: int) -> dict:
    """If ledger looks stale vs live sells, run midday KIS ledger resync once.

    Guarded by env SKIP and a short mtime cooldown to avoid API hammering.
    """
    if os.environ.get("DASHBOARD_SKIP_LEDGER_RESYNC", "").lower() in ("1", "true", "yes"):
        return led
    today = _now_seoul().strftime("%Y-%m-%d")
    if str(led.get("date") or "") != today:
        return led
    led_sells = int((led.get("meta") or {}).get("sell_fill_count") or 0)
    if log_sells <= led_sells and led.get("realized_gross") is not None:
        return led
    # cooldown: skip if ledger updated within last 60s
    try:
        import time as _time
        mtime = (ROOT / "day_ledger.json").stat().st_mtime
        if _time.time() - mtime < 60:
            return led
    except OSError:
        pass
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "midday_kis_ledger_resync", ROOT / "midday_kis_ledger_resync.py"
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.run()
            return _read_json(ROOT / "day_ledger.json", {}) or led
    except Exception:
        return led
    return _read_json(ROOT / "day_ledger.json", {}) or led


def build_status(*, try_kis: bool = True) -> dict:
    cfg = _load_config()
    positions = _read_json(ROOT / "positions.json", {"positions": [], "count": 0}) or {}
    orders_state = _read_json(ROOT / "orders_state.json", {}) or {}
    log_meta = _parse_fills_and_meta()
    pnl = _compute_realized_pnl(log_meta["trades"])
    log_sells = _count_today_log_sells(log_meta.get("trades") or [])
    # Prefer explicit day ledger when present (KIS-synced) — but not if stale vs live sells
    try:
        led = _read_json(ROOT / "day_ledger.json", {}) or {}
        if try_kis and os.environ.get("DASHBOARD_SKIP_KIS", "").lower() not in ("1", "true", "yes"):
            led = _maybe_refresh_ledger_from_kis(led, log_sells)
        led_sells = int((led.get("meta") or {}).get("sell_fill_count") or 0)
        led_date_ok = str(led.get("date") or "") == _now_seoul().strftime("%Y-%m-%d")
        # Stale only when live/kis uniquely shows MORE sells than ledger claims
        ledger_stale = bool(led_date_ok and log_sells > led_sells)
        if led.get("realized_gross") is not None and not ledger_stale:
            raw_rts = led.get("round_trips") or []
            norm_rts = []
            for rt in raw_rts:
                if not isinstance(rt, dict):
                    continue
                buy_price = rt.get("buy_price", rt.get("buy"))
                sell_price = rt.get("sell_price", rt.get("sell"))
                pnl_v = rt.get("pnl", rt.get("pnl_gross"))
                try:
                    buy_f = float(buy_price) if buy_price is not None else None
                except (TypeError, ValueError):
                    buy_f = None
                try:
                    sell_f = float(sell_price) if sell_price is not None else None
                except (TypeError, ValueError):
                    sell_f = None
                try:
                    qty_f = float(rt.get("qty") or 1)
                except (TypeError, ValueError):
                    qty_f = 1.0
                try:
                    pnl_f = float(pnl_v) if pnl_v is not None else None
                except (TypeError, ValueError):
                    pnl_f = None
                if pnl_f is None and buy_f is not None and sell_f is not None:
                    pnl_f = round((sell_f - buy_f) * qty_f, 2)
                norm = dict(rt)
                if buy_f is not None:
                    norm["buy_price"] = buy_f
                if sell_f is not None:
                    norm["sell_price"] = sell_f
                if pnl_f is not None:
                    norm["pnl"] = round(pnl_f, 2)
                norm["qty"] = qty_f
                if rt.get("sell_ts") is not None:
                    norm["sell_ts"] = rt.get("sell_ts")
                norm_rts.append(norm)
            led_meta = led.get("meta") or {}
            pnl = {
                "realized_pnl": float(led.get("realized_net_est", led.get("realized_gross") or 0)),
                "realized_gross": float(led.get("realized_gross") or 0),
                "fees_day_est": led.get("fees_day_est"),
                "tax_est": led.get("tax_est"),
                "round_trips": norm_rts,
                "round_trip_count": len(norm_rts),
                "open_buy_lots": 0,  # filled below from open BUY orders
                "source": "day_ledger",
                "buy_fill_count": led_meta.get("buy_fill_count"),
                "sell_fill_count": led_meta.get("sell_fill_count"),
                "buy_fill_qty": led_meta.get("buy_fill_qty"),
                "sell_fill_qty": led_meta.get("sell_fill_qty"),
                "capital_used_ledger": led.get("capital_used"),
            }
            # Prefer ledger today_fills at head of trades (authoritative KIS sync)
            today_s = _now_seoul().strftime("%Y-%m-%d")
            if str(led.get("date") or "") == today_s:
                led_fills = []
                for f in (led_meta.get("today_fills") or []):
                    if not isinstance(f, dict):
                        continue
                    side = str(f.get("side") or "")
                    px = f.get("price")
                    qty = f.get("qty") or 1
                    odno = f.get("odno")
                    tmd = str(f.get("tmd") or "000000").zfill(6)[-6:]
                    led_fills.append(
                        {
                            "ts": f"{today_s}T{tmd[:2]}:{tmd[2:4]}:{tmd[4:6]}+09:00",
                            "kind": "buy_fill" if side == "BUY" else "sell_fill",
                            "side": side,
                            "price": px,
                            "qty": qty,
                            "order_id": odno,
                            "tag": f"{side} {qty}@{px} odno={odno}",
                            "source": "day_ledger",
                            "file": "day_ledger.json",
                            "message": f"{side} {qty}@{px} odno={odno}",
                        }
                    )
                if led_fills:
                    rest = [
                        tr
                        for tr in log_meta.get("trades") or []
                        if tr.get("source") != "day_ledger"
                        and not str(tr.get("order_id") or "").startswith("MOCK")
                        and not str(tr.get("file") or "").startswith("run-")
                    ]
                    seen = {str(x.get("order_id")) for x in led_fills}
                    rest = [tr for tr in rest if str(tr.get("order_id")) not in seen]
                    log_meta["trades"] = led_fills + rest
    except Exception:
        pass

    symbol = cfg.get("symbol") or "367380"
    grid = cfg.get("grid") or {}
    session = cfg.get("session") or {}
    limits = cfg.get("limits") or {}
    param = cfg.get("param_choice") or {}

    spacing_pct = grid.get("spacing_pct", param.get("spacing_pct", 0.002))
    levels = grid.get("levels", param.get("levels", 5))
    tp_pct = grid.get("tp_pct", param.get("tp_pct", 0.002))

    # Open orders: only pending/open/canceling (never show filled as 미체결)
    raw_orders = orders_state.get("orders") or []
    open_orders = [
        o for o in raw_orders
        if str(o.get("status", "open")).lower() in ("open", "pending", "canceling")
    ]
    if not open_orders and orders_state.get("all_orders"):
        open_orders = [
            o
            for o in orders_state["all_orders"]
            if str(o.get("status", "")).lower() in ("open", "pending", "canceling")
        ]

    # Positions list
    pos_list = positions.get("positions") or []
    pos_meta = positions.get("meta") or {}

    # Price / cash resolution
    kis = {"ok": False, "skipped": True}
    last_price = None
    price_source = None
    cash = None
    cash_source = None

    if try_kis and os.environ.get("DASHBOARD_SKIP_KIS", "").lower() not in ("1", "true", "yes"):
        env_dv = (cfg.get("safety") or {}).get("kis_env_dv", "real")
        kis = _try_kis_quote(symbol, env_dv=env_dv)
        if kis.get("price") is not None:
            last_price = kis["price"]
            price_source = "kis"
        if kis.get("cash") is not None:
            cash = kis["cash"]
            cash_source = "kis"

    if last_price is None:
        # orders_state ref_price is recent for live dry
        ref = orders_state.get("ref_price")
        if ref is not None:
            last_price = int(ref)
            price_source = "orders_state.ref_price"
        elif log_meta.get("last_price_from_logs") is not None:
            last_price = log_meta["last_price_from_logs"]
            price_source = log_meta.get("last_price_src") or "logs"

    if cash is None and log_meta.get("cash_from_logs") is not None:
        cash = log_meta["cash_from_logs"]
        cash_source = log_meta.get("cash_src") or "logs"

    # MTM
    mtm = 0.0
    cost_basis = 0.0
    for p in pos_list:
        try:
            bp = float(p.get("buy_price") or p.get("price") or 0)
            qty = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        cost_basis += bp * qty
        if last_price is not None:
            mtm += (float(last_price) - bp) * qty

    # Backtest return for current params (0.2%/5/0.2%)
    bt_daily = _read_csv_top(BACKTEST / "results.csv", limit=8)
    bt_minute = _read_csv_top(BACKTEST / "results_minute_10d.csv", limit=8)

    def _match_param_row(bt: dict) -> Optional[dict]:
        if not bt.get("rows"):
            return None
        for row in bt["rows"]:
            try:
                sp = float(row.get("spacing_pct") or -1)
                lv = int(float(row.get("levels") or -1))
                tp = float(row.get("tp_pct") or -1)
            except (TypeError, ValueError):
                continue
            if abs(sp - float(spacing_pct)) < 1e-9 and lv == int(levels) and abs(tp - float(tp_pct)) < 1e-9:
                return row
        # Also scan full file for match (top may not include current params)
        return None

    def _find_param_in_csv(path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        sp = float(row.get("spacing_pct") or -1)
                        lv = int(float(row.get("levels") or -1))
                        tp = float(row.get("tp_pct") or -1)
                    except (TypeError, ValueError):
                        continue
                    if abs(sp - float(spacing_pct)) < 1e-9 and lv == int(levels) and abs(tp - float(tp_pct)) < 1e-9:
                        return row
        except OSError:
            return None
        return None

    matched_daily = _match_param_row(bt_daily) or _find_param_in_csv(BACKTEST / "results.csv")
    matched_minute = _match_param_row(bt_minute) or _find_param_in_csv(
        BACKTEST / "results_minute_10d.csv"
    )

    # Simple return %: from backtest match if no live capital; else MTM+realized / cost
    return_pct = None
    return_note = None
    initial_capital = None
    if matched_minute:
        try:
            return_pct = float(matched_minute.get("total_return_pct"))
            initial_capital = float(matched_minute.get("initial_cash"))
            return_note = "backtest minute 10d (current params)"
        except (TypeError, ValueError):
            pass
    if return_pct is None and matched_daily:
        try:
            return_pct = float(matched_daily.get("total_return_pct"))
            initial_capital = float(matched_daily.get("initial_cash"))
            return_note = "backtest daily 3m (current params)"
        except (TypeError, ValueError):
            pass

    # Capital used today = sum(buy_price*qty for RTs) + sum(open buy price*qty
    # from open_orders). Open BUY orders contribute their filled qty (deployed);
    # fully-unfilled working buys are counted in open_buy_lots only so return%
    # reflects capital that actually traded. If RTs empty, fall back to cost_basis.
    capital_used_today = 0.0
    rts_list = pnl.get("round_trips") or []
    for rt in rts_list:
        try:
            bp = float(rt.get("buy_price") if rt.get("buy_price") is not None else rt.get("buy") or 0)
            q = float(rt.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        capital_used_today += bp * q
    open_buy_qty = 0.0
    for o in open_orders:
        if str(o.get("side") or "").upper() != "BUY":
            continue
        try:
            op = float(o.get("price") or 0)
            oq = float(o.get("qty") or 0)
            fq = float(o.get("fill_qty") or 0)
        except (TypeError, ValueError):
            continue
        open_buy_qty += oq
        if fq > 0:
            capital_used_today += op * fq
    if not rts_list:
        capital_used_today = float(cost_basis or 0) or capital_used_today
    # Prefer explicit ledger capital (matched buys + open position cost)
    if pnl.get("capital_used_ledger") is not None:
        try:
            capital_used_today = float(pnl["capital_used_ledger"])
        except (TypeError, ValueError):
            pass
    elif rts_list and cost_basis:
        # include open position cost for return% when ledger lacked capital_used
        capital_used_today = float(capital_used_today) + float(cost_basis)
    if pnl.get("source") == "day_ledger":
        pnl["open_buy_lots"] = int(open_buy_qty) if open_buy_qty == int(open_buy_qty) else open_buy_qty

    realized_for_return = pnl.get("realized_gross")
    if realized_for_return is None:
        realized_for_return = pnl.get("realized_pnl") or 0.0
    else:
        realized_for_return = float(realized_for_return)

    live_return_pct = None
    if capital_used_today > 0:
        live_return_pct = round(((mtm + float(realized_for_return)) / capital_used_today) * 100, 4)

    # Alias for telegram / older consumers: prefer live, else backtest
    return_pct_alias = live_return_pct if live_return_pct is not None else return_pct

    # Fill counts: prefer day_ledger meta; else count today's live/kis trades
    buy_fill_count = pnl.get("buy_fill_count")
    sell_fill_count = pnl.get("sell_fill_count")
    buy_fill_qty = pnl.get("buy_fill_qty")
    sell_fill_qty = pnl.get("sell_fill_qty")
    if buy_fill_count is None or sell_fill_count is None:
        today_s = _now_seoul().strftime("%Y-%m-%d")
        bc = bq = sc = sq = 0
        for t in log_meta.get("trades") or []:
            ts = str(t.get("ts") or "")
            if today_s not in ts and not str(t.get("file") or "").startswith("fills-"):
                continue
            side = str(t.get("side") or "").upper()
            try:
                q = float(t.get("qty") or 1)
            except (TypeError, ValueError):
                q = 1.0
            if side == "BUY":
                bc += 1
                bq += q
            elif side == "SELL":
                sc += 1
                sq += q
        if buy_fill_count is None:
            buy_fill_count = bc
        if sell_fill_count is None:
            sell_fill_count = sc
        if buy_fill_qty is None:
            buy_fill_qty = int(bq) if bq == int(bq) else bq
        if sell_fill_qty is None:
            sell_fill_qty = int(sq) if sq == int(sq) else sq

    # 3-trading-day cumulative realized (Seoul weekdays ending today)
    try:
        from cumulative_pnl import compute_cumulative_pnl
        cum3 = compute_cumulative_pnl(n_days=3, archive_today=True)
    except Exception:
        cum3 = None

    approval = _live_approved()
    now = _now_seoul()

    # Session window check
    in_session = None
    try:
        start_s = session.get("start", "09:05")
        end_s = session.get("end", "15:20")
        t = now.time()
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        from datetime import time as dtime

        in_session = dtime(sh, sm) <= t <= dtime(eh, em)
    except Exception:  # noqa: BLE001
        in_session = None

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Seoul",
        "overview": {
            "symbol": symbol,
            "symbol_name": cfg.get("symbol_name"),
            "last_price": last_price,
            "price_source": price_source,
            "cash": cash,
            "cash_source": cash_source,
            "config_summary": {
                "spacing_pct": spacing_pct,
                "spacing_display": f"{float(spacing_pct) * 100:.1f}%",
                "levels": levels,
                "tp_pct": tp_pct,
                "tp_display": f"{float(tp_pct) * 100:.1f}%",
                "session": f"{session.get('start', '09:05')}–{session.get('end', '15:20')}",
                "qty_per_order": grid.get("qty_per_order", 1),
                "max_holdings": limits.get("max_holdings"),
                "max_new_buys_per_day": limits.get("max_new_buys_per_day"),
                "tick_size": grid.get("tick_size"),
            },
            "in_session": in_session,
            "mode": orders_state.get("mode") or log_meta.get("mode_hint"),
            "ref_price": orders_state.get("ref_price"),
            "spacing": orders_state.get("spacing") or pos_meta.get("spacing"),
            "buy_lines": orders_state.get("buy_lines") or pos_meta.get("buy_lines") or [],
            "day_high": orders_state.get("day_high") or pos_meta.get("day_high"),
            "new_buys_filled_today": orders_state.get("new_buys_filled_today")
            if orders_state.get("new_buys_filled_today") is not None
            else pos_meta.get("new_buys_filled_today"),
            "buy_fill_count": buy_fill_count,
            "sell_fill_count": sell_fill_count,
            "buy_fill_qty": buy_fill_qty,
            "sell_fill_qty": sell_fill_qty,
        },
        "live_approved": approval,
        "kis": {
            "ok": bool(kis.get("ok")),
            "error": kis.get("error"),
            "cached": kis.get("cached"),
            "skipped": kis.get("skipped", False),
        },
        "open_orders": open_orders,
        "orders_meta": {
            "updated_at": orders_state.get("updated_at"),
            "all_orders_count": len(orders_state.get("all_orders") or []),
            "note": orders_state.get("note"),
        },
        "positions": pos_list,
        "positions_meta": {
            "count": positions.get("count", len(pos_list)),
            "total_qty": positions.get("total_qty", sum(int(p.get("qty") or 0) for p in pos_list)),
            **{k: pos_meta.get(k) for k in ("symbol", "spacing", "buy_lines", "day_high", "new_buys_filled_today")},
        },
        "trades": log_meta["trades"][:100],
        "recent_events": log_meta["recent_events"][:60],
        "log_files": log_meta["log_files"],
        "pnl": {
            "realized": pnl["realized_pnl"],
            "realized_gross": pnl.get("realized_gross"),
            "fees_day_est": pnl.get("fees_day_est"),
            "tax_est": pnl.get("tax_est"),
            "pnl_source": pnl.get("source", "trades"),
            "mtm": round(mtm, 2),
            "total_est": round(pnl["realized_pnl"] + mtm, 2),
            "cost_basis": round(cost_basis, 2),
            "capital_used_today": round(float(capital_used_today or 0), 2),
            "round_trip_count": pnl["round_trip_count"],
            "round_trips": pnl["round_trips"][-20:],
            "open_buy_lots": pnl["open_buy_lots"],
            "buy_fill_count": buy_fill_count,
            "sell_fill_count": sell_fill_count,
            "buy_fill_qty": buy_fill_qty,
            "sell_fill_qty": sell_fill_qty,
            "live_return_pct": live_return_pct,
            "return_pct": return_pct_alias,
            "backtest_return_pct": return_pct,
            "backtest_return_note": return_note,
            "initial_capital_hint": initial_capital,
            "cumulative_3d": cum3,
        },
        "backtest": {
            "daily_3m": bt_daily,
            "minute_10d": bt_minute,
            "matched_params_daily": matched_daily,
            "matched_params_minute": matched_minute,
        },
    }


if __name__ == "__main__":
    skip = "--skip-kis" in sys.argv
    status = build_status(try_kis=not skip)
    # Never dump secrets — status has none by design
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
