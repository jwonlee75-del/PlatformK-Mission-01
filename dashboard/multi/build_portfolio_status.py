#!/usr/bin/env python3
"""Aggregate both grid bots into one portfolio status JSON.

Read-only. Never places/cancels orders. Never includes secrets.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent
REPO = DASHBOARD.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from adapter_091170 import build_091170  # noqa: E402
from common import (  # noqa: E402
    default_091170_root,
    default_367380_root,
    empty_bot,
    now_seoul,
    num,
    redact,
    resolve_bot_root,
)
from win_rate import combine_win_rates, compute_win_rate  # noqa: E402

# Per-symbol quote cache so 15s refresh does not hammer KIS.
_KIS_CACHE: dict[str, dict[str, Any]] = {}
_KIS_TTL_SEC = 30.0


def _try_kis_quote(symbol: str, env_dv: str = "real") -> dict:
    """Read-only last price + cash. Cached per symbol. Never places orders."""
    now = time.time()
    cached = _KIS_CACHE.get(symbol)
    if cached and now - float(cached.get("ts") or 0) < _KIS_TTL_SEC:
        return {
            "ok": cached.get("price") is not None or cached.get("cash") is not None,
            "price": cached.get("price"),
            "cash": cached.get("cash"),
            "error": cached.get("error"),
            "cached": True,
        }

    result: dict[str, Any] = {"ok": False, "price": None, "cash": None, "error": None, "cached": False}
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
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

    _KIS_CACHE[symbol] = {
        "ts": now,
        "price": result["price"],
        "cash": result["cash"],
        "error": result["error"],
    }
    return result


def _skip_kis_env() -> bool:
    return os.environ.get("DASHBOARD_SKIP_KIS", "").lower() in ("1", "true", "yes")


def _normalize_367380(status: dict, *, root: Path) -> dict:
    ov = dict(status.get("overview") or {})
    pos_meta = status.get("positions_meta") or {}
    frozen = bool(ov.get("safety_frozen") or pos_meta.get("safety_frozen"))
    reasons = ov.get("safety_reasons") or pos_meta.get("safety_reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)] if reasons else []
    ov["safety_frozen"] = frozen
    ov["safety_reasons"] = reasons
    pnl = dict(status.get("pnl") or {})
    pnl["win_rate"] = compute_win_rate(root)
    return redact(
        {
            "id": "367380",
            "schema": "grid",
            "ok": True,
            "error": None,
            "root": str(root),
            "root_exists": root.is_dir(),
            "overview": ov,
            "live_approved": status.get("live_approved") or {},
            "kis": status.get("kis") or {},
            "open_orders": status.get("open_orders") or [],
            "positions": status.get("positions") or [],
            "positions_meta": pos_meta,
            "slots": [],
            "plan": {"present": False},
            "trades": status.get("trades") or [],
            "pnl": pnl,
            "files_present": [
                n
                for n in ("config.json", "positions.json", "orders_state.json", "day_ledger.json", "LIVE_APPROVED")
                if (root / n).exists()
            ]
            + (["ledger_archive/"] if (root / "ledger_archive").is_dir() else [])
            + (["logs/"] if (root / "logs").is_dir() else []),
            "recent_events": status.get("recent_events") or [],
        }
    )


def build_bot_367380(*, try_kis: bool, root: Optional[Path] = None) -> dict:
    root = Path(root or resolve_bot_root("367380", default_367380_root()))
    if not root.is_dir():
        return empty_bot(
            bot_id="367380",
            schema="grid",
            root=root,
            code="root_missing",
            message=f"367380 bot path not found: {root}",
        )
    try:
        from build_status import build_status

        # Prefer the in-repo aggregator when this checkout *is* the 367380 bot.
        same = root.resolve() == REPO.resolve()
        if same:
            st = build_status(try_kis=try_kis)
            return _normalize_367380(st, root=root)
        # Alternate checkout: still try build_status (it reads this repo's files)
        # then overlay would be wrong — fall through to a file-only note.
        st = build_status(try_kis=try_kis)
        out = _normalize_367380(st, root=REPO)
        out["warning"] = (
            f"build_status reads this checkout ({REPO}); "
            f"GRID_BOT_367380_ROOT={root} is noted but not remapped"
        )
        out["requested_root"] = str(root)
        return out
    except Exception as e:  # noqa: BLE001
        return empty_bot(
            bot_id="367380",
            schema="grid",
            root=root,
            code=type(e).__name__,
            message=str(e)[:300],
        )


def _sum_num(bots: list[dict], getter) -> Optional[float]:
    total = 0.0
    any_v = False
    for b in bots:
        if not b.get("ok"):
            continue
        v = getter(b)
        n = num(v)
        if n is None:
            continue
        total += n
        any_v = True
    return round(total, 2) if any_v else None


def _combine_cumulative(bots: list[dict]) -> Optional[dict]:
    cums = []
    for b in bots:
        if not b.get("ok"):
            continue
        c = (b.get("pnl") or {}).get("cumulative_3d")
        if c and (c.get("realized_net_est") is not None or c.get("realized_gross") is not None):
            cums.append((b.get("id"), c))
    if not cums:
        return None

    by_date: dict[str, dict] = {}
    net = 0.0
    gross = 0.0
    fees = 0.0
    tax = 0.0
    available = 0
    for _bid, c in cums:
        if c.get("realized_net_est") is not None:
            net += float(c["realized_net_est"])
        if c.get("realized_gross") is not None:
            gross += float(c["realized_gross"])
        fees += float(c.get("fees_est") or 0)
        tax += float(c.get("tax_est") or 0)
        available += int(c.get("available_days") or 0)
        for rec in c.get("per_day") or []:
            d = str(rec.get("date") or "")
            slot = by_date.setdefault(
                d,
                {
                    "date": d,
                    "available": False,
                    "realized_gross": 0.0,
                    "realized_net_est": 0.0,
                    "bots": [],
                },
            )
            if rec.get("available"):
                slot["available"] = True
                slot["realized_gross"] += float(rec.get("realized_gross") or 0)
                slot["realized_net_est"] += float(rec.get("realized_net_est") or 0)
                slot["bots"].append(_bid)
    per_day = [by_date[k] for k in sorted(by_date)]
    for rec in per_day:
        rec["realized_gross"] = round(rec["realized_gross"], 2)
        rec["realized_net_est"] = round(rec["realized_net_est"], 2)
    return {
        "n_days": max((c.get("n_days") or 3) for _b, c in cums),
        "per_day": per_day,
        "realized_gross": round(gross, 2),
        "realized_net_est": round(net, 2),
        "fees_est": round(fees, 2),
        "tax_est": round(tax, 2),
        "sources": [b for b, _ in cums],
        "available_days": available,
    }


def build_portfolio_status(*, try_kis: bool = True) -> dict:
    if _skip_kis_env():
        try_kis = False

    root_367 = resolve_bot_root("367380", default_367380_root())
    root_091 = resolve_bot_root("091170", default_091170_root())

    bot_367 = build_bot_367380(try_kis=try_kis, root=root_367)

    kis_091 = None
    if try_kis:
        env_dv = "real"
        try:
            cfg_path = root_091 / "config.json"
            if cfg_path.exists():
                import json

                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                env_dv = (cfg.get("safety") or {}).get("kis_env_dv", "real")
        except Exception:
            env_dv = "real"
        kis_091 = _try_kis_quote("091170", env_dv=env_dv)

    bot_091 = build_091170(root=root_091, try_kis=try_kis, kis_quote=kis_091)

    bots = [bot_367, bot_091]
    ok_bots = [b for b in bots if b.get("ok")]

    cash = None
    cash_source = None
    for b in bots:
        c = (b.get("overview") or {}).get("cash")
        if c is not None:
            cash = c
            cash_source = f"{b.get('id')}:{(b.get('overview') or {}).get('cash_source') or 'file'}"
            break

    realized = _sum_num(ok_bots, lambda b: (b.get("pnl") or {}).get("realized"))
    realized_gross = _sum_num(ok_bots, lambda b: (b.get("pnl") or {}).get("realized_gross"))
    mtm = _sum_num(ok_bots, lambda b: (b.get("pnl") or {}).get("mtm"))
    cost = _sum_num(ok_bots, lambda b: (b.get("pnl") or {}).get("cost_basis"))
    total_est = None
    if realized is not None or mtm is not None:
        total_est = round((realized or 0) + (mtm or 0), 2)

    orders_n = sum(len(b.get("open_orders") or []) for b in ok_bots)
    pos_n = sum(len(b.get("positions") or []) for b in ok_bots)
    pos_qty = 0
    for b in ok_bots:
        meta = b.get("positions_meta") or {}
        if meta.get("total_qty") is not None:
            pos_qty += int(num(meta.get("total_qty")) or 0)
        else:
            pos_qty += int(sum(num(p.get("qty")) or 0 for p in (b.get("positions") or [])))

    kis_any_ok = any((b.get("kis") or {}).get("ok") for b in bots)
    kis_any_skip = all((b.get("kis") or {}).get("skipped") for b in bots)
    kis_errors = [f"{b.get('id')}:{(b.get('kis') or {}).get('error')}" for b in bots if (b.get("kis") or {}).get("error")]

    errors = {b["id"]: b["error"] for b in bots if b.get("error")}

    return redact(
        {
            "generated_at": now_seoul().isoformat(timespec="seconds"),
            "timezone": "Asia/Seoul",
            "skip_kis": not try_kis,
            "read_only": True,
            "hero": {
                "realized": realized if realized is not None else 0.0,
                "realized_gross": realized_gross if realized_gross is not None else 0.0,
                "mtm": mtm if mtm is not None else 0.0,
                "total_est": total_est if total_est is not None else 0.0,
                "cost_basis": cost if cost is not None else 0.0,
                "cash": cash,
                "cash_source": cash_source,
                "open_orders": orders_n,
                "positions": pos_n,
                "position_qty": pos_qty,
                "bots_ok": len(ok_bots),
                "bots_total": len(bots),
                "cumulative_3d": _combine_cumulative(bots),
                "win_rate": combine_win_rates(bots),
            },
            "kis": {
                "ok": kis_any_ok,
                "skipped": kis_any_skip or not try_kis,
                "errors": kis_errors,
            },
            "bots": bots,
            "errors": errors,
        }
    )


if __name__ == "__main__":
    skip = "--skip-kis" in sys.argv or _skip_kis_env()
    print(
        __import__("json").dumps(
            build_portfolio_status(try_kis=not skip),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
