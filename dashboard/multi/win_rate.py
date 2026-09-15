"""Cumulative grid win rate from ledger round-trips.

Win = pnl_gross > 0, loss = pnl_gross < 0, breakeven (0) is excluded
from the denominator. Rate = wins / (wins + losses).

Sources: every ``ledger_archive/day_ledger-*.json`` plus today's live
``day_ledger.json`` when its session date is today. One ledger per calendar
day (live today wins over a same-day archive). Explicit ``round_trips`` only
— no generic FIFO. Read-only. Never prints secrets.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

from common import now_seoul, num, read_json

_LEDGER_NAME = re.compile(r"day_ledger-(\d{8})\.json$")


def empty_win_rate(*, reason: str = "no_round_trips") -> dict:
    return {
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "round_trips": 0,
        "decided": 0,
        "win_rate": None,
        "win_rate_pct": None,
        "days": 0,
        "today_included": False,
        "per_day": [],
        "source": "ledger_archive+today",
        "empty_reason": reason,
    }


def rt_pnl_gross(rt: dict) -> Optional[float]:
    """Prefer pnl_gross, then pnl, else (sell-buy)*qty."""
    if not isinstance(rt, dict):
        return None
    pnl = num(rt.get("pnl_gross"))
    if pnl is None:
        pnl = num(rt.get("pnl"))
    if pnl is None:
        buy = num(rt.get("buy_price") if rt.get("buy_price") is not None else rt.get("buy"))
        sell = num(rt.get("sell_price") if rt.get("sell_price") is not None else rt.get("sell"))
        qty = num(rt.get("qty")) or 1.0
        if buy is not None and sell is not None:
            pnl = (sell - buy) * qty
    return pnl


def classify_rt(pnl: Optional[float]) -> Optional[str]:
    if pnl is None:
        return None
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "breakeven"


def _tally(rts: list[dict]) -> dict:
    wins = losses = even = skipped = 0
    for rt in rts:
        kind = classify_rt(rt_pnl_gross(rt))
        if kind == "win":
            wins += 1
        elif kind == "loss":
            losses += 1
        elif kind == "breakeven":
            even += 1
        else:
            skipped += 1
    decided = wins + losses
    rate = (wins / decided) if decided else None
    return {
        "wins": wins,
        "losses": losses,
        "breakeven": even,
        "round_trips": wins + losses + even,
        "skipped": skipped,
        "decided": decided,
        "win_rate": round(rate, 6) if rate is not None else None,
        "win_rate_pct": round(rate * 100, 2) if rate is not None else None,
    }


def _ledger_date(led: dict, *, fallback: Optional[date] = None) -> Optional[date]:
    for key in ("date", "session_date"):
        v = str(led.get(key) or "")
        if len(v) >= 10:
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                pass
        digits = re.sub(r"\D", "", v)
        if len(digits) == 8:
            try:
                return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                pass
    return fallback


def _extract_rts(led: dict) -> list[dict]:
    raw = led.get("round_trips") if isinstance(led, dict) else None
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _iter_archive_ledgers(root: Path) -> list[tuple[date, Path, dict]]:
    archive = root / "ledger_archive"
    if not archive.is_dir():
        return []
    out: list[tuple[date, Path, dict]] = []
    for path in sorted(archive.glob("day_ledger-*.json")):
        m = _LEDGER_NAME.search(path.name)
        led = read_json(path, None)
        if not isinstance(led, dict):
            continue
        fb = None
        if m:
            try:
                fb = date(int(m.group(1)[0:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
            except ValueError:
                fb = None
        day = _ledger_date(led, fallback=fb)
        if day is None:
            continue
        out.append((day, path, led))
    return out


def _today_ledger(root: Path, today: date) -> Optional[tuple[date, Path, dict]]:
    path = root / "day_ledger.json"
    led = read_json(path, None)
    if not isinstance(led, dict):
        return None
    day = _ledger_date(led)
    if day is None:
        # Undated live file counts only as calendar-today.
        if today == now_seoul().date():
            day = today
        else:
            return None
    if day != today:
        return None
    return (day, path, led)


def compute_win_rate(root: Path, *, today: Optional[date] = None) -> dict:
    """All archive days + today. One ledger per date. Explicit RTs only."""
    root = Path(root)
    today = today or now_seoul().date()
    if not root.is_dir():
        return empty_win_rate(reason="root_missing")

    by_day: dict[date, tuple[Path, dict, str]] = {}
    for day, path, led in _iter_archive_ledgers(root):
        by_day[day] = (path, led, path.name)

    live = _today_ledger(root, today)
    today_included = False
    if live:
        day, path, led = live
        by_day[day] = (path, led, "day_ledger.json")
        today_included = True

    if not by_day:
        return empty_win_rate(reason="no_ledgers")

    per_day = []
    all_rts: list[dict] = []
    for day in sorted(by_day):
        path, led, source = by_day[day]
        rts = _extract_rts(led)
        tally = _tally(rts)
        all_rts.extend(rts)
        per_day.append(
            {
                "date": day.isoformat(),
                "wins": tally["wins"],
                "losses": tally["losses"],
                "breakeven": tally["breakeven"],
                "round_trips": tally["round_trips"],
                "decided": tally["decided"],
                "win_rate": tally["win_rate"],
                "source": source,
            }
        )

    tot = _tally(all_rts)
    tot.update(
        {
            "days": len(per_day),
            "today_included": today_included,
            "per_day": per_day,
            "source": "ledger_archive+today",
            "empty_reason": None if tot["decided"] else "no_decided_round_trips",
        }
    )
    return tot


def combine_win_rates(bots: list[dict]) -> dict:
    """Hero aggregate: sum wins/losses across bots, then recompute rate."""
    wins = losses = even = rts = days = 0
    sources: list[str] = []
    for b in bots:
        if not b.get("ok"):
            continue
        wr = (b.get("pnl") or {}).get("win_rate") or {}
        if not isinstance(wr, dict):
            continue
        w, l = int(wr.get("wins") or 0), int(wr.get("losses") or 0)
        if w == 0 and l == 0 and not wr.get("decided"):
            if not wr.get("days"):
                continue
        wins += w
        losses += l
        even += int(wr.get("breakeven") or 0)
        rts += int(wr.get("round_trips") or 0)
        days += int(wr.get("days") or 0)
        sources.append(str(b.get("id") or ""))
    decided = wins + losses
    rate = (wins / decided) if decided else None
    return {
        "wins": wins,
        "losses": losses,
        "breakeven": even,
        "round_trips": rts,
        "decided": decided,
        "win_rate": round(rate, 6) if rate is not None else None,
        "win_rate_pct": round(rate * 100, 2) if rate is not None else None,
        "days": days,
        "bots": sources,
        "source": "hero_sum",
        "empty_reason": None if decided else "no_decided_round_trips",
    }
