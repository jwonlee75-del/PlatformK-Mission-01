#!/usr/bin/env python3
"""1-minute OHLC trade-snapshot charts for the portfolio dashboard.

For a Seoul trading date (today if weekday, else last weekday):
  - load that day's ledger fills per bot root
  - fetch or reuse cached 1m bars (KIS inquire-time-dailychartprice, read-only)
  - write PNG snapshots with BUY▲ / SELL▼ markers
    (full day + 09:00–09:30 zoom when `_fills_clustered_morning`)

Matplotlib is optional: if import fails, skip PNG generation.
Never places orders. Never prints secrets/tokens.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE.parent
REPO = DASHBOARD.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from common import (  # noqa: E402
    default_091170_root,
    default_367380_root,
    now_seoul,
    num,
    read_json,
    resolve_bot_root,
)
from cumulative_pnl import last_n_trading_days  # noqa: E402

SEOUL = timezone(timedelta(hours=9))
TTL_SESSION_SEC = 90  # near-realtime while the market is open
TTL_OFFHOURS_SEC = 600
API_URL = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
TR_ID = "FHKST03010230"

CHART_NAME_RE = re.compile(r"^[0-9]{6}_trades_1m(?:_am)?(?:_[0-9]{8})?\.png$")

BOT_SPECS = (
    {"id": "367380", "name": "ACE NASDAQ100"},
    {"id": "091170", "name": "KODEX Bank"},
)

# Shared 30-minute morning zoom (both bots).
MORNING_ZOOM_FROM = "090000"
MORNING_ZOOM_TO = "093000"

_BUILD_LOCK = threading.Lock()
_KIS_AUTHED = False

FetchBarsFn = Callable[[str, date], list[dict]]
RenderFn = Callable[..., bool]


def seoul_chart_date(as_of: Optional[date] = None) -> date:
    """Live snapshot session: Seoul today if a weekday, else the previous weekday.

    Never walks back to the last day that had fills.
    """
    return last_n_trading_days(1, as_of=as_of)[-1]


def in_chart_session(now: Optional[datetime] = None) -> bool:
    """Seoul cash-session window used for snapshot TTL (09:00–15:30 weekdays)."""
    dt = now or now_seoul()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SEOUL)
    else:
        dt = dt.astimezone(SEOUL)
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    return dtime(9, 0) <= t <= dtime(15, 30)


def charts_dir(explicit: Optional[Path] = None) -> Path:
    env = os.environ.get("DASHBOARD_CHARTS_DIR")
    if explicit:
        path = Path(explicit)
    elif env:
        path = Path(os.path.expanduser(env))
    else:
        path = default_367380_root() / "logs" / "charts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chart_ttl_sec(*, now: Optional[datetime] = None) -> int:
    raw = os.environ.get("DASHBOARD_CHART_TTL_SEC")
    if raw not in (None, ""):
        try:
            return max(30, int(raw))
        except (TypeError, ValueError):
            pass
    return TTL_SESSION_SEC if in_chart_session(now) else TTL_OFFHOURS_SEC


def safe_chart_name(name: str) -> Optional[str]:
    """Allow only {symbol}_trades_1m[_am][_YYYYMMDD].png."""
    base = Path(name).name
    if CHART_NAME_RE.match(base):
        return base
    return None


def matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _first(*vals: Any) -> Any:
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def ledger_covers_date(led: dict, target: date) -> bool:
    if not isinstance(led, dict):
        return False
    iso = target.isoformat()
    ymd = _ymd(target)
    for key in ("date", "session_date"):
        v = str(led.get(key) or "")
        if v in (iso, ymd) or (v.startswith(iso) and len(v) >= 10):
            return True
    meta = led.get("meta") if isinstance(led.get("meta"), dict) else {}
    mv = str(meta.get("date") or meta.get("session_date") or "")
    return mv in (iso, ymd)


def load_ledger_for_date(root: Path, target: date) -> Optional[dict]:
    """Ledger for *target only*. Never walks back to the last day-with-fills.

    Prefer live ``day_ledger.json`` when it matches (realtime). Same-date
    archive is a fallback. Previous-day files are ignored.
    """
    if not root.is_dir():
        return None
    live = root / "day_ledger.json"
    if live.is_file():
        led = read_json(live, None)
        if isinstance(led, dict):
            if ledger_covers_date(led, target):
                return led
            # Undated live file counts only for Seoul calendar-today.
            if not led.get("date") and not led.get("session_date"):
                if target == now_seoul().date():
                    out = dict(led)
                    out["date"] = target.isoformat()
                    return out

    ymd = _ymd(target)
    archive = root / "ledger_archive" / f"day_ledger-{ymd}.json"
    if not archive.is_file():
        return None
    led = read_json(archive, None)
    if not isinstance(led, dict):
        return None
    claimed = str(led.get("date") or led.get("session_date") or "")
    if claimed and claimed[:10] not in (target.isoformat(), ymd):
        return None
    if not led.get("date") and not led.get("session_date"):
        led = dict(led)
        led["date"] = target.isoformat()
    return led


def parse_fill_tmd(fill: dict, *, fallback_date: date) -> Optional[str]:
    """Return HHMMSS in Seoul, or None if unparseable."""
    tmd_raw = fill.get("tmd") or fill.get("time")
    if tmd_raw not in (None, ""):
        digits = re.sub(r"\D", "", str(tmd_raw))
        if len(digits) >= 6:
            return digits.zfill(6)[-6:]
        if 1 <= len(digits) <= 5:
            padded = digits.zfill(6)
            return padded

    ts = fill.get("ts") or fill.get("timestamp") or fill.get("filled_at")
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        # epoch seconds
        try:
            dt = datetime.fromtimestamp(float(ts), tz=SEOUL)
            return dt.strftime("%H%M%S")
        except (OSError, OverflowError, ValueError):
            return None
    text = str(ts).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SEOUL)
        return dt.astimezone(SEOUL).strftime("%H%M%S")
    except ValueError:
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 6:
            return digits[-6:]
        return None


def fill_date(fill: dict, *, ledger_date: date) -> date:
    ts = fill.get("ts") or fill.get("timestamp") or fill.get("filled_at")
    if isinstance(ts, str) and "T" in ts:
        text = ts.strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SEOUL)
            return dt.astimezone(SEOUL).date()
        except ValueError:
            pass
    return ledger_date


def extract_raw_fills(led: dict) -> list[dict]:
    if not isinstance(led, dict):
        return []
    meta = led.get("meta") if isinstance(led.get("meta"), dict) else {}
    raw: list[dict] = []
    for key in ("fills", "today_fills"):
        for src in (led, meta):
            v = src.get(key)
            if isinstance(v, list):
                raw.extend(x for x in v if isinstance(x, dict))
    return raw


def fills_to_markers(fills: list[dict], chart_date: date, *, ledger_date: Optional[date] = None) -> list[dict]:
    """Map ledger fills to {tmd, price, side, qty} on chart_date only."""
    led_d = ledger_date or chart_date
    out: list[dict] = []
    seen: set[str] = set()
    for f in fills:
        side = str(f.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        px = num(_first(f.get("price"), f.get("fill_price"), f.get("avg_price")))
        if px is None:
            continue
        if fill_date(f, ledger_date=led_d) != chart_date:
            continue
        tmd = parse_fill_tmd(f, fallback_date=chart_date)
        if not tmd:
            continue
        qty = num(_first(f.get("qty"), f.get("quantity"))) or 1
        odno = _first(f.get("odno"), f.get("order_id"))
        key = f"{odno}|{side}|{px}|{qty}|{tmd}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "tmd": tmd,
                "price": float(px),
                "side": side,
                "qty": float(qty),
                "order_id": odno,
            }
        )
    out.sort(key=lambda m: m["tmd"])
    return out


def load_fills_for_bot(root: Path, chart_date: date) -> list[dict]:
    led = load_ledger_for_date(root, chart_date)
    if not led:
        return []
    led_d = chart_date
    for key in ("date", "session_date"):
        v = str(led.get(key) or "")
        if len(v) >= 10:
            try:
                led_d = date.fromisoformat(v[:10])
                break
            except ValueError:
                pass
    return fills_to_markers(extract_raw_fills(led), chart_date, ledger_date=led_d)


def count_sides(markers: list[dict]) -> tuple[int, int]:
    buys = sum(1 for m in markers if m.get("side") == "BUY")
    sells = sum(1 for m in markers if m.get("side") == "SELL")
    return buys, sells


def _fills_clustered_morning(
    markers: list[dict],
    *,
    t_to: str = MORNING_ZOOM_TO,
    min_fills: int = 2,
) -> bool:
    """True when enough fills land at or before 09:30 (pre-open 08:xx counts)."""
    n = sum(1 for m in markers if str(m.get("tmd") or "") and str(m.get("tmd")) <= t_to)
    return n >= min_fills


def has_morning_cluster(markers: list[dict], **kwargs) -> bool:
    """Public alias for ``_fills_clustered_morning``."""
    return _fills_clustered_morning(markers, **kwargs)


def bars_cache_path(directory: Path, symbol: str, chart_date: date) -> Path:
    return directory / f"bars_{symbol}_{_ymd(chart_date)}.json"


def load_cached_bars(directory: Path, symbol: str, chart_date: date) -> list[dict]:
    want = _ymd(chart_date)
    data = read_json(bars_cache_path(directory, symbol, chart_date), None)
    raw: list = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict) and isinstance(data.get("bars"), list):
        raw = data["bars"]
    out = []
    for x in raw:
        if not isinstance(x, dict) or not x.get("time"):
            continue
        bd = str(x.get("date") or "").replace("-", "")
        if bd and bd != want:
            continue
        out.append(x)
    return out


def save_cached_bars(directory: Path, symbol: str, chart_date: date, bars: list[dict]) -> None:
    ymd = _ymd(chart_date)
    stamped = []
    for b in bars:
        row = dict(b)
        row["date"] = ymd
        stamped.append(row)
    path = bars_cache_path(directory, symbol, chart_date)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stamped, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _hour_minus_one(hhmmss: str) -> str:
    h = int(hhmmss[0:2])
    m = int(hhmmss[2:4])
    s = int(hhmmss[4:6]) if len(hhmmss) >= 6 else 0
    total = h * 3600 + m * 60 + s - 60
    if total < 0:
        return "000000"
    return f"{total // 3600:02d}{(total % 3600) // 60:02d}{total % 60:02d}"


def _ensure_kis_auth() -> Any:
    global _KIS_AUTHED
    llm = Path(os.environ.get("OPEN_TRADING_API_LLM") or "/workspace/open-trading-api/examples_llm")
    if str(llm) not in sys.path:
        sys.path.insert(0, str(llm))
    import kis_auth as ka  # type: ignore

    if not _KIS_AUTHED:
        ka.auth(svr="prod")
        _KIS_AUTHED = True
    return ka


def _kis_fetch_page(symbol: str, date_ymd: str, hour: str) -> list[dict]:
    ka = _ensure_kis_auth()
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_HOUR_1": hour,
        "FID_INPUT_DATE_1": date_ymd,
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": "N",
    }
    res = ka._url_fetch(API_URL, TR_ID, "", params)
    if not res.isOK():
        body = getattr(res, "getErrorMessage", lambda: "kis_error")()
        raise RuntimeError(f"KIS minute chart fail {type(body).__name__}")
    body = res.getBody()
    out2 = getattr(body, "output2", None) or []
    if isinstance(out2, dict):
        out2 = [out2]
    return list(out2)


def fetch_minute_bars(
    symbol: str,
    chart_date: date,
    *,
    fetch_page: Optional[Callable[[str, str, str], list[dict]]] = None,
) -> list[dict]:
    """Paginate FHKST03010230 backward from 15:30 until before 09:00. Read-only."""
    date_ymd = _ymd(chart_date)
    page_fn = fetch_page or _kis_fetch_page
    seen: set[str] = set()
    rows: list[dict] = []
    hour = "153000"
    for _ in range(20):
        if fetch_page is None:
            time.sleep(0.35)
        page = page_fn(symbol, date_ymd, hour)
        if not page:
            break
        new_count = 0
        oldest = None
        for r in page:
            if not isinstance(r, dict):
                continue
            if r.get("stck_bsop_date") and str(r.get("stck_bsop_date")) != date_ymd:
                continue
            t = str(r.get("stck_cntg_hour") or r.get("time") or "")
            if not t or t in seen:
                continue
            seen.add(t)
            new_count += 1
            try:
                rows.append(
                    {
                        "date": date_ymd,
                        "time": t,
                        "open": int(float(r.get("stck_oprc") or r.get("open"))),
                        "high": int(float(r.get("stck_hgpr") or r.get("high"))),
                        "low": int(float(r.get("stck_lwpr") or r.get("low"))),
                        "close": int(float(r.get("stck_prpr") or r.get("close"))),
                        "volume": int(float(r.get("cntg_vol") or r.get("volume") or 0)),
                    }
                )
            except (TypeError, ValueError):
                continue
            if oldest is None or t < oldest:
                oldest = t
        if new_count == 0 or oldest is None:
            break
        if oldest <= "090100":
            break
        hour = _hour_minus_one(oldest)
        if hour >= oldest:
            break
    rows.sort(key=lambda x: str(x.get("time") or ""))
    return rows


def _tmd_minutes(tmd: str) -> float:
    t = str(tmd).zfill(6)[-6:]
    return int(t[0:2]) * 60 + int(t[2:4]) + int(t[4:6]) / 60.0


def marker_x_on_bars(tmd: str, bars: list[dict]) -> float:
    """Map HHMMSS onto bar index (nearest / clamped)."""
    times = [str(b.get("time") or "") for b in bars]
    if tmd in times:
        return float(times.index(tmd))
    if not times:
        return 0.0
    target = _tmd_minutes(tmd)
    mins = [_tmd_minutes(t) for t in times]
    if target <= mins[0]:
        return -0.2
    if target >= mins[-1]:
        return float(len(times) - 1) + 0.2
    for i in range(1, len(mins)):
        if target <= mins[i]:
            span = mins[i] - mins[i - 1] or 1.0
            return (i - 1) + (target - mins[i - 1]) / span
    return float(len(times) - 1)


def _filter_bars(bars: list[dict], t_from: Optional[str], t_to: Optional[str]) -> list[dict]:
    out = bars
    if t_from:
        out = [b for b in out if str(b.get("time") or "") >= t_from]
    if t_to:
        out = [b for b in out if str(b.get("time") or "") <= t_to]
    return out


def render_chart(
    bars: list[dict],
    markers: list[dict],
    out_path: Path,
    *,
    title: str,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    marker_time_from: Optional[str] = None,
) -> bool:
    """Write a dark OHLC PNG. Returns False if matplotlib is unavailable."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception:  # noqa: BLE001
        return False

    view = _filter_bars(bars, time_from, time_to)
    if not view:
        return False
    marks = markers
    m_from = time_from if marker_time_from is None else marker_time_from
    if m_from or time_to:
        marks = [
            m
            for m in markers
            if (not m_from or m["tmd"] >= m_from) and (not time_to or m["tmd"] <= time_to)
        ]

    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(10.2, 3.8), dpi=120)
    fig.patch.set_facecolor("#0b0f14")
    ax.set_facecolor("#0e1520")
    for spine in ax.spines.values():
        spine.set_color("#243041")
    ax.tick_params(colors="#8b9bb0", labelsize=8)
    ax.yaxis.label.set_color("#8b9bb0")
    ax.xaxis.label.set_color("#8b9bb0")
    ax.title.set_color("#e7eef8")
    ax.grid(True, color="#243041", linewidth=0.5, alpha=0.7)

    for i, b in enumerate(view):
        try:
            o = float(b["open"])
            h = float(b["high"])
            low = float(b["low"])
            c = float(b["close"])
        except (KeyError, TypeError, ValueError):
            continue
        color = "#3dd68c" if c >= o else "#ff6b7a"
        ax.plot([i, i], [low, h], color=color, linewidth=0.7, solid_capstyle="round")
        body = abs(c - o)
        if body < 1:
            body = 1.0
        ax.add_patch(
            Rectangle((i - 0.32, min(o, c)), 0.64, body, facecolor=color, edgecolor=color, linewidth=0.4)
        )

    buys_x, buys_y, sells_x, sells_y = [], [], [], []
    for m in marks:
        x = marker_x_on_bars(m["tmd"], view)
        if m["side"] == "BUY":
            buys_x.append(x)
            buys_y.append(m["price"])
        else:
            sells_x.append(x)
            sells_y.append(m["price"])
    if buys_x:
        ax.scatter(buys_x, buys_y, marker="^", s=36, c="#3dd68c", zorder=5, label="BUY", edgecolors="#0b0f14", linewidths=0.4)
    if sells_x:
        ax.scatter(sells_x, sells_y, marker="v", s=36, c="#ff8a6b", zorder=5, label="SELL", edgecolors="#0b0f14", linewidths=0.4)

    times = [str(b.get("time") or "") for b in view]
    ticks = []
    labels = []
    last_lbl = None
    for i, t in enumerate(times):
        if len(t) < 4:
            continue
        hhmm = t[:4]
        if hhmm.endswith("00") or hhmm.endswith("30"):
            lbl = f"{t[:2]}:{t[2:4]}"
            if lbl != last_lbl:
                ticks.append(i)
                labels.append(lbl)
                last_lbl = lbl
    if ticks:
        ax.set_xticks(ticks)
        ax.set_xticklabels(labels)
    ax.set_xlim(-1, len(view))
    ax.set_title(title, fontsize=11, pad=8)
    if buys_x or sells_x:
        leg = ax.legend(loc="upper left", fontsize=8, framealpha=0.3)
        for txt in leg.get_texts():
            txt.set_color("#e7eef8")
        leg.get_frame().set_edgecolor("#243041")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return out_path.is_file()


def empty_symbol_entry(symbol: str, chart_date: date, reason: str) -> dict:
    return {
        "available": False,
        "symbol": symbol,
        "date": chart_date.isoformat(),
        "url": None,
        "zoom_url": None,
        "generated_at": None,
        "buy_count": 0,
        "sell_count": 0,
        "bar_count": 0,
        "fill_count": 0,
        "no_fills": True,
        "empty_reason": reason,
    }


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def empty_index(chart_date: date, reason: str = "not_built") -> dict:
    return {
        "ok": False,
        "date": chart_date.isoformat(),
        "generated_at": None,
        "ttl_sec": chart_ttl_sec(),
        "matplotlib": matplotlib_available(),
        "kis_used": False,
        "stale": True,
        "symbols": {spec["id"]: empty_symbol_entry(spec["id"], chart_date, reason) for spec in BOT_SPECS},
    }


def read_chart_index(directory: Optional[Path] = None, *, live_date: Optional[date] = None) -> dict:
    """Disk index only — never fetches KIS.

    A previous session's index is always stale so the live PNG cannot stick
    on yesterday when today still has zero fills.
    """
    live = live_date or seoul_chart_date()
    directory = charts_dir(directory)
    data = read_json(directory / "index.json", None)
    if isinstance(data, dict) and data.get("symbols"):
        data.setdefault("ttl_sec", chart_ttl_sec())
        gen = data.get("generated_at")
        stale = True
        if gen:
            try:
                dt = datetime.fromisoformat(str(gen))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=SEOUL)
                age = (now_seoul() - dt.astimezone(SEOUL)).total_seconds()
                stale = age > float(data.get("ttl_sec") or chart_ttl_sec())
            except ValueError:
                stale = True
        if str(data.get("date") or "") != live.isoformat():
            stale = True
        data["stale"] = stale
        if "ok" not in data:
            data["ok"] = any((s or {}).get("available") for s in (data.get("symbols") or {}).values())
        return data
    return empty_index(live, "not_built")


def _resolve_bars(
    symbol: str,
    chart_date: date,
    directory: Path,
    *,
    skip_kis: bool,
    fetch_bars: Optional[FetchBarsFn],
) -> tuple[list[dict], bool]:
    cached = load_cached_bars(directory, symbol, chart_date)
    if fetch_bars is not None:
        bars = fetch_bars(symbol, chart_date) or []
        if bars:
            save_cached_bars(directory, symbol, chart_date, bars)
            return bars, True
        return cached, False
    if skip_kis:
        return cached, False
    try:
        bars = fetch_minute_bars(symbol, chart_date)
    except Exception:  # noqa: BLE001
        return cached, False
    if bars:
        save_cached_bars(directory, symbol, chart_date, bars)
        return bars, True
    return cached, False


def build_snapshots(
    *,
    chart_date: Optional[date] = None,
    charts_dir_path: Optional[Path] = None,
    roots: Optional[dict[str, Path]] = None,
    fetch_bars: Optional[FetchBarsFn] = None,
    skip_kis: bool = False,
    render_fn: Optional[RenderFn] = None,
) -> dict:
    """Rebuild PNG snapshots + index.json. Read-only KIS if fetch_bars is None."""
    target = chart_date or seoul_chart_date()
    directory = charts_dir(charts_dir_path)
    roots = roots or {
        "367380": resolve_bot_root("367380", default_367380_root()),
        "091170": resolve_bot_root("091170", default_091170_root()),
    }
    painter = render_fn or render_chart
    generated_at = now_seoul().isoformat(timespec="seconds")
    kis_used = False
    symbols: dict[str, dict] = {}
    mpl_ok = matplotlib_available() if render_fn is None else True

    for spec in BOT_SPECS:
        sid = spec["id"]
        root = Path(roots.get(sid) or "")
        markers = load_fills_for_bot(root, target) if root.is_dir() else []
        buys, sells = count_sides(markers)
        bars, fetched = _resolve_bars(
            sid, target, directory, skip_kis=skip_kis, fetch_bars=fetch_bars
        )
        kis_used = kis_used or fetched

        if not root.is_dir():
            symbols[sid] = empty_symbol_entry(sid, target, "root_missing")
            symbols[sid]["buy_count"] = buys
            symbols[sid]["sell_count"] = sells
            symbols[sid]["no_fills"] = True
            continue
        if not bars:
            reason = "no_cached_bars" if skip_kis and fetch_bars is None else "no_bars"
            entry = empty_symbol_entry(sid, target, reason)
            entry["buy_count"] = buys
            entry["sell_count"] = sells
            entry["fill_count"] = len(markers)
            entry["no_fills"] = len(markers) == 0
            symbols[sid] = entry
            continue
        if render_fn is None and not mpl_ok:
            entry = empty_symbol_entry(sid, target, "matplotlib_unavailable")
            entry["buy_count"] = buys
            entry["sell_count"] = sells
            entry["fill_count"] = len(markers)
            entry["bar_count"] = len(bars)
            entry["no_fills"] = len(markers) == 0
            symbols[sid] = entry
            continue

        fname = f"{sid}_trades_1m.png"
        dated = f"{sid}_trades_1m_{_ymd(target)}.png"
        out = directory / fname
        title = f"{spec['name']} {sid} · {target.isoformat()} 1m"
        ok = bool(
            painter(
                bars,
                markers,
                out,
                title=title,
                time_from=None,
                time_to=None,
            )
        )
        if ok:
            dated_path = directory / dated
            try:
                dated_path.write_bytes(out.read_bytes())
            except OSError:
                pass

        zoom_url = None
        if _fills_clustered_morning(markers):
            zname = f"{sid}_trades_1m_am.png"
            zok = bool(
                painter(
                    bars,
                    markers,
                    directory / zname,
                    title=f"{spec['name']} {sid} · {target.isoformat()} 1m 09:00-09:30",
                    time_from=MORNING_ZOOM_FROM,
                    time_to=MORNING_ZOOM_TO,
                    marker_time_from="000000",
                )
            )
            if zok:
                zoom_url = f"/charts/{zname}"

        if not ok:
            entry = empty_symbol_entry(sid, target, "render_failed")
            entry["buy_count"] = buys
            entry["sell_count"] = sells
            entry["fill_count"] = len(markers)
            entry["bar_count"] = len(bars)
            entry["no_fills"] = len(markers) == 0
            symbols[sid] = entry
            continue

        symbols[sid] = {
            "available": True,
            "symbol": sid,
            "date": target.isoformat(),
            "url": f"/charts/{fname}",
            "zoom_url": zoom_url,
            "generated_at": generated_at,
            "buy_count": buys,
            "sell_count": sells,
            "bar_count": len(bars),
            "fill_count": len(markers),
            "no_fills": len(markers) == 0,
            "empty_reason": None,
        }

    index = {
        "ok": any(s.get("available") for s in symbols.values()),
        "date": target.isoformat(),
        "generated_at": generated_at,
        "ttl_sec": chart_ttl_sec(),
        "matplotlib": mpl_ok if render_fn is None else True,
        "kis_used": kis_used and fetch_bars is None and not skip_kis,
        "stale": False,
        "symbols": symbols,
    }
    _write_json(directory / "index.json", index)
    return index


def get_or_build_charts(
    *,
    refresh: bool = False,
    skip_kis: bool = False,
    charts_dir_path: Optional[Path] = None,
    fetch_bars: Optional[FetchBarsFn] = None,
    render_fn: Optional[RenderFn] = None,
    chart_date: Optional[date] = None,
    roots: Optional[dict[str, Path]] = None,
) -> dict:
    """Return index; rebuild if missing, stale, or refresh=1.

    15s UI polls should call read_chart_index(), not this, so KIS is not hammered.
    """
    if os.environ.get("DASHBOARD_SKIP_KIS", "").lower() in ("1", "true", "yes"):
        skip_kis = True
    live = chart_date or seoul_chart_date()
    with _BUILD_LOCK:
        current = read_chart_index(charts_dir_path, live_date=live)
        need = refresh or not current.get("generated_at") or current.get("stale")
        if not need:
            return current
        return build_snapshots(
            chart_date=live,
            charts_dir_path=charts_dir_path,
            roots=roots,
            skip_kis=skip_kis,
            fetch_bars=fetch_bars,
            render_fn=render_fn,
        )


def attach_charts(
    status: dict,
    directory: Optional[Path] = None,
    *,
    live_date: Optional[date] = None,
) -> dict:
    """Attach last-known *live-session* chart index (no rebuild).

    Previous-day snapshots stay on dated PNGs and are not advertised as live.
    """
    live = live_date or seoul_chart_date()
    idx = read_chart_index(directory, live_date=live)
    if str(idx.get("date") or "") != live.isoformat():
        idx = empty_index(live, "stale_previous_session")
    status["charts"] = idx
    symbols = idx.get("symbols") or {}
    for b in status.get("bots") or []:
        bid = str(b.get("id") or "")
        entry = symbols.get(bid) or empty_symbol_entry(bid or "?", live, "not_built")
        if str(entry.get("date") or "") != live.isoformat():
            entry = empty_symbol_entry(bid or "?", live, "stale_previous_session")
        b["chart"] = entry
    return status
