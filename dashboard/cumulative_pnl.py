#!/usr/bin/env python3
"""3-day cumulative realized PnL for grid-bot dashboard / Telegram.

Sums realized_gross / realized_net_est across the last N Seoul trading days
(ending today). Sources, in preference order per day:

1. logs/day_ledger-YYYYMMDD.json (archived snapshot)
2. root day_ledger.json when its date matches
3. best day_ledger.json.bak-* whose embedded date matches (prefer eod=True)
4. logs/status-summary-YYYYMMDD*.json pnl block
5. reconstruct from logs/fills-live-YYYYMMDD.jsonl (FIFO / price-link; marked)

Missing days are returned with available=False so callers can show e.g. `9/9: n/a`.
Never prints secrets. Read-only (optional soft archive of today's ledger).
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
LEDGER_ARCHIVE = ROOT / "ledger_archive"
SEOUL = timezone(timedelta(hours=9))

FEE_RATE_PER_SIDE = 0.00015  # 0.015%
TAX_RATE_ON_GROSS = 0.154


def _now_seoul() -> datetime:
    return datetime.now(SEOUL)


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def last_n_trading_days(n: int = 3, *, as_of: Optional[date] = None) -> list[date]:
    """Weekdays only (Mon–Fri). KR exchange holidays are not calendared here."""
    d = as_of or _now_seoul().date()
    out: list[date] = []
    cur = d
    while len(out) < n:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            out.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(out))


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _day_record_from_ledger(led: dict, *, source: str) -> dict:
    gross = _num(led.get("realized_gross"))
    net = _num(led.get("realized_net_est"))
    if net is None and gross is not None:
        fees = _num(led.get("fees_day_est")) or 0.0
        tax = _num(led.get("tax_est")) or 0.0
        net = gross - fees - tax
    rts = led.get("round_trips") or []
    return {
        "date": str(led.get("date") or ""),
        "available": gross is not None,
        "realized_gross": gross,
        "realized_net_est": net,
        "fees_day_est": _num(led.get("fees_day_est")),
        "tax_est": _num(led.get("tax_est")),
        "round_trip_count": len(rts) if isinstance(rts, list) else None,
        "eod": bool(led.get("eod") or led.get("session_ended")),
        "source": source,
        "gap": False,
    }


def _score_ledger(led: dict) -> tuple:
    """Prefer EOD + higher absolute completeness (gross present, more RTs)."""
    eod = 1 if (led.get("eod") or led.get("session_ended")) else 0
    rts = len(led.get("round_trips") or [])
    gross = _num(led.get("realized_gross"))
    has = 1 if gross is not None else 0
    return (eod, has, rts, gross or 0.0)


def _load_archived(day: date) -> Optional[dict]:
    ymd = day.strftime("%Y%m%d")
    for path in (
        LEDGER_ARCHIVE / f"day_ledger-{ymd}.json",
        LOGS / f"day_ledger-{ymd}.json",
        ROOT / f"day_ledger-{ymd}.json",
        LOGS / f"day_ledger-{day.isoformat()}.json",
    ):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("realized_gross") is not None:
            if str(data.get("date") or "") in ("", day.isoformat()):
                data = dict(data)
                data["date"] = day.isoformat()
            return _day_record_from_ledger(data, source=str(path.relative_to(ROOT)))
    return None


def _load_current_if_match(day: date) -> Optional[dict]:
    led = _read_json(ROOT / "day_ledger.json")
    if not isinstance(led, dict):
        return None
    if str(led.get("date") or "") != day.isoformat():
        return None
    if led.get("realized_gross") is None:
        return None
    return _day_record_from_ledger(led, source="day_ledger.json")


def _load_from_backups(day: date) -> Optional[dict]:
    best: Optional[dict] = None
    best_score: Optional[tuple] = None
    best_src: Optional[str] = None
    for path in sorted(ROOT.glob("day_ledger.json.bak*")):
        led = _read_json(path)
        if not isinstance(led, dict):
            continue
        if str(led.get("date") or "") != day.isoformat():
            continue
        if led.get("realized_gross") is None:
            continue
        score = _score_ledger(led)
        if best_score is None or score > best_score:
            best = led
            best_score = score
            best_src = path.name
    if best is None:
        return None
    return _day_record_from_ledger(best, source=best_src or "bak")


def _load_from_status_summary(day: date) -> Optional[dict]:
    ymd = day.strftime("%Y%m%d")
    candidates = sorted(
        list(LOGS.glob(f"status-summary-{ymd}*.json"))
        + list(LOGS.glob(f"status-summary-{day.isoformat()}*.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        pnl = data.get("pnl") or {}
        gross = _num(pnl.get("realized_gross"))
        if gross is None:
            continue
        net = _num(pnl.get("realized", pnl.get("realized_pnl", pnl.get("realized_net_est"))))
        led_like = {
            "date": day.isoformat(),
            "realized_gross": gross,
            "realized_net_est": net,
            "fees_day_est": pnl.get("fees_day_est"),
            "tax_est": pnl.get("tax_est"),
            "round_trips": pnl.get("round_trips") or [],
            "eod": "eod" in path.name.lower(),
            "session_ended": "eod" in path.name.lower(),
        }
        return _day_record_from_ledger(led_like, source=str(path.relative_to(ROOT)))
    return None


def _reconstruct_from_fills(day: date) -> Optional[dict]:
    """Best-effort FIFO / same-price link from fills-live JSONL. Marked as reconstructed."""
    ymd = day.strftime("%Y%m%d")
    path = LOGS / f"fills-live-{ymd}.jsonl"
    if not path.exists():
        return None
    buys: list[dict] = []
    sells: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        side = str(rec.get("side") or "").upper()
        try:
            px = float(rec.get("price"))
            qty = float(rec.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        row = {"price": px, "qty": qty, "odno": rec.get("odno")}
        if side == "BUY":
            buys.append(row)
        elif side == "SELL":
            sells.append(row)
    if not sells:
        # zero-realized day with fills still counts as available
        if buys:
            return {
                "date": day.isoformat(),
                "available": True,
                "realized_gross": 0.0,
                "realized_net_est": 0.0,
                "fees_day_est": 0.0,
                "tax_est": 0.0,
                "round_trip_count": 0,
                "eod": False,
                "source": f"fills-live-{ymd}.jsonl (no sells)",
                "gap": False,
            }
        return None

    pool = deepcopy(buys)
    rts: list[dict] = []
    for s in sells:
        # Prefer same-price open buy (TP-style), else FIFO
        idx = None
        for i, b in enumerate(pool):
            if abs(b["price"] - (s["price"] - 60)) < 1e-6 or abs(b["price"] - s["price"]) < 1e-6:
                # prefer buy that is ~spacing below sell if present
                pass
        # TP link: sell ≈ buy + spacing; try exact price-60 first for 0.2% ~60won grids
        for i, b in enumerate(pool):
            if abs((s["price"] - b["price"]) - 60) < 1e-6:
                idx = i
                break
        if idx is None:
            for i, b in enumerate(pool):
                if abs(b["price"] - s["price"]) < 1e-6:
                    idx = i
                    break
        if idx is None and pool:
            idx = 0
        if idx is None:
            continue
        b = pool.pop(idx)
        take = min(b["qty"], s["qty"])
        pnl_g = (s["price"] - b["price"]) * take
        rts.append({"buy": b["price"], "sell": s["price"], "qty": take, "pnl_gross": pnl_g})
        rem = b["qty"] - take
        if rem > 1e-9:
            pool.insert(idx, {"price": b["price"], "qty": rem, "odno": b.get("odno")})

    gross = round(sum(float(r["pnl_gross"]) for r in rts), 2)
    buy_notional = sum(float(r["buy"]) * float(r["qty"]) for r in rts)
    sell_notional = sum(float(r["sell"]) * float(r["qty"]) for r in rts)
    fees = round((buy_notional + sell_notional) * FEE_RATE_PER_SIDE)
    tax = round(gross * TAX_RATE_ON_GROSS) if gross > 0 else 0
    net = round(gross - fees - tax, 2)
    return {
        "date": day.isoformat(),
        "available": True,
        "realized_gross": float(gross),
        "realized_net_est": float(net),
        "fees_day_est": float(fees),
        "tax_est": float(tax),
        "round_trip_count": len(rts),
        "eod": False,
        "source": f"fills-live-{ymd}.jsonl (reconstructed)",
        "gap": False,
    }


def load_day_pnl(day: date) -> dict:
    """Load one day's realized PnL with source attribution."""
    for loader in (
        _load_archived,
        _load_current_if_match,
        _load_from_backups,
        _load_from_status_summary,
        _reconstruct_from_fills,
    ):
        try:
            rec = loader(day)
        except Exception:
            rec = None
        if rec and rec.get("available"):
            return rec
    return {
        "date": day.isoformat(),
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


def maybe_archive_today_ledger() -> Optional[Path]:
    """Soft-write ledger_archive/ + logs/ day snapshots from current day_ledger if fresher."""
    led = _read_json(ROOT / "day_ledger.json")
    if not isinstance(led, dict) or not led.get("date"):
        return None
    if led.get("realized_gross") is None:
        return None
    day_s = str(led["date"])
    try:
        day = date.fromisoformat(day_s)
    except ValueError:
        return None
    ymd = day.strftime("%Y%m%d")
    written = None
    for dest_dir in (LEDGER_ARCHIVE, LOGS):
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        dest = dest_dir / f"day_ledger-{ymd}.json"
        existing = _read_json(dest)
        if isinstance(existing, dict):
            if _score_ledger(existing) >= _score_ledger(led) and existing.get("realized_gross") is not None:
                written = written or dest
                continue
        try:
            dest.write_text(json.dumps(led, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written = dest
        except OSError:
            continue
    return written


def compute_cumulative_pnl(
    *,
    n_days: int = 3,
    as_of: Optional[date] = None,
    archive_today: bool = True,
) -> dict:
    """Return cumulative realized for last n Seoul trading days ending as_of/today."""
    if archive_today:
        maybe_archive_today_ledger()

    days = last_n_trading_days(n_days, as_of=as_of)
    per_day = [load_day_pnl(d) for d in days]

    gross_sum = 0.0
    net_sum = 0.0
    fees_sum = 0.0
    tax_sum = 0.0
    gaps: list[str] = []
    available_days = 0
    for rec in per_day:
        if not rec.get("available"):
            gaps.append(rec["date"])
            continue
        available_days += 1
        gross_sum += float(rec["realized_gross"] or 0)
        net_sum += float(rec["realized_net_est"] or 0)
        fees_sum += float(rec.get("fees_day_est") or 0)
        tax_sum += float(rec.get("tax_est") or 0)

    return {
        "n_days": n_days,
        "days": [d.isoformat() for d in days],
        "per_day": per_day,
        "available_days": available_days,
        "gaps": gaps,
        "realized_gross": round(gross_sum, 2) if available_days else None,
        "realized_net_est": round(net_sum, 2) if available_days else None,
        "fees_est": round(fees_sum, 2) if available_days else None,
        "tax_est": round(tax_sum, 2) if available_days else None,
        "complete": available_days == n_days and not gaps,
    }


def format_cumulative_lines(cum: dict, *, indent: str = "  ") -> list[str]:
    """Telegram / text lines under PnL."""
    if not cum or cum.get("realized_net_est") is None:
        return [
            f"{indent}3일 누적 실현(순익추정): n/a",
        ]

    def _won(v: Any) -> str:
        try:
            n = int(round(float(v)))
            sign = "+" if n > 0 else ""
            return f"{sign}{n:,}"
        except (TypeError, ValueError):
            return "-"

    net = cum["realized_net_est"]
    gross = cum.get("realized_gross")
    lines = [
        f"{indent}3일 누적 실현(순익추정): {_won(net)}원",
    ]
    if gross is not None:
        lines.append(f"{indent}3일 누적 실현(총차익): {_won(gross)}원")

    # per-day breakdown: 9/9 +A · 9/10 +B · 9/11 +C
    parts: list[str] = []
    for rec in cum.get("per_day") or []:
        try:
            d = date.fromisoformat(str(rec.get("date")))
            label = f"{d.month}/{d.day}"
        except Exception:
            label = str(rec.get("date") or "?")
        if not rec.get("available"):
            parts.append(f"{label}: n/a")
        else:
            parts.append(f"{label} {_won(rec.get('realized_net_est'))}")
    if parts:
        lines.append(f"{indent}" + " · ".join(parts))

    if cum.get("gaps"):
        gap_labs = []
        for g in cum["gaps"]:
            try:
                d = date.fromisoformat(g)
                gap_labs.append(f"{d.month}/{d.day}")
            except Exception:
                gap_labs.append(g)
        lines.append(f"{indent}※ 결측일: {', '.join(gap_labs)}")

    return lines


if __name__ == "__main__":
    out = compute_cumulative_pnl()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print("---")
    print("\n".join(format_cumulative_lines(out)))
