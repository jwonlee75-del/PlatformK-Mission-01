"""Shared helpers for the portfolio dashboard. Never logs secrets."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SEOUL = timezone(timedelta(hours=9))

SECRET_KEY_RE = re.compile(
    r"(chat[_-]?id|app[_-]?key|app[_-]?secret|secret|token|password|"
    r"authorization|api[_-]?key|telegram|kis_config|private[_-]?key)",
    re.I,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FALLBACK_091170_ROOT = Path("/workspace/grid-bot-091170")


def default_091170_root() -> Path:
    return Path(os.environ.get("GRID_BOT_091170_ROOT") or str(FALLBACK_091170_ROOT))


def default_367380_root() -> Path:
    return Path(os.environ.get("GRID_BOT_367380_ROOT") or str(REPO_ROOT))


# Back-compat aliases (resolved at import; prefer the functions above)
DEFAULT_091170_ROOT = FALLBACK_091170_ROOT
DEFAULT_367380_ROOT = REPO_ROOT


def now_seoul() -> datetime:
    return datetime.now(SEOUL)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def num_or_zero(v: Any) -> float:
    n = num(v)
    return 0.0 if n is None else n


def redact(obj: Any) -> Any:
    """Drop secret-looking keys from nested JSON before returning to clients."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if SECRET_KEY_RE.search(str(k)):
                continue
            out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def live_approved(root: Path) -> dict:
    path = root / "LIVE_APPROVED"
    today = now_seoul().strftime("%Y-%m-%d")
    if not path.exists():
        return {"present": False, "content": None, "valid_today": False, "today": today, "path": str(path)}
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        content = ""
    valid = content == today or content.startswith(today)
    return {
        "present": True,
        "content": content[:32] if content else "",
        "valid_today": valid,
        "today": today,
        "path": str(path),
    }


def in_session(session: dict | None) -> Optional[bool]:
    try:
        session = session or {}
        start_s = session.get("start", "09:05")
        end_s = session.get("end", "15:20")
        t = now_seoul().time()
        sh, sm = map(int, str(start_s).split(":"))
        eh, em = map(int, str(end_s).split(":"))
        return dtime(sh, sm) <= t <= dtime(eh, em)
    except Exception:  # noqa: BLE001
        return None


def config_summary(cfg: dict) -> dict:
    grid = cfg.get("grid") or {}
    session = cfg.get("session") or {}
    limits = cfg.get("limits") or {}
    param = cfg.get("param_choice") or {}
    spacing_pct = grid.get("spacing_pct", param.get("spacing_pct"))
    levels = grid.get("levels", param.get("levels"))
    tp_pct = grid.get("tp_pct", param.get("tp_pct"))
    spacing_disp = None
    tp_disp = None
    try:
        if spacing_pct is not None:
            spacing_disp = f"{float(spacing_pct) * 100:.1f}%"
    except (TypeError, ValueError):
        spacing_disp = str(spacing_pct) if spacing_pct is not None else None
    try:
        if tp_pct is not None:
            tp_disp = f"{float(tp_pct) * 100:.1f}%"
    except (TypeError, ValueError):
        tp_disp = str(tp_pct) if tp_pct is not None else None
    return {
        "spacing_pct": spacing_pct,
        "spacing_display": spacing_disp,
        "levels": levels,
        "tp_pct": tp_pct,
        "tp_display": tp_disp,
        "session": f"{session.get('start', '09:05')}–{session.get('end', '15:20')}",
        "qty_per_order": grid.get("qty_per_order") or cfg.get("qty_per_order"),
        "max_holdings": limits.get("max_holdings") or cfg.get("max_holdings"),
        "max_new_buys_per_day": limits.get("max_new_buys_per_day"),
        "tick_size": grid.get("tick_size") or cfg.get("tick_size"),
        "slots": cfg.get("slots") or grid.get("slots") or limits.get("slots"),
    }


def list_present_files(root: Path, names: list[str]) -> list[str]:
    out = []
    for name in names:
        if (root / name).exists():
            out.append(name)
    archive = root / "ledger_archive"
    if archive.is_dir():
        out.append("ledger_archive/")
    logs = root / "logs"
    if logs.is_dir():
        out.append("logs/")
    return out


def empty_bot(*, bot_id: str, schema: str, root: Path, code: str, message: str) -> dict:
    return {
        "id": bot_id,
        "schema": schema,
        "ok": False,
        "error": {"code": code, "message": message[:300]},
        "root": str(root),
        "root_exists": root.is_dir(),
        "overview": {
            "symbol": bot_id,
            "symbol_name": None,
            "last_price": None,
            "price_source": None,
            "cash": None,
            "cash_source": None,
            "config_summary": {},
            "in_session": None,
            "mode": None,
            "safety_frozen": False,
            "safety_reasons": [],
        },
        "live_approved": live_approved(root) if root.is_dir() else {
            "present": False, "content": None, "valid_today": False, "today": now_seoul().strftime("%Y-%m-%d")
        },
        "kis": {"ok": False, "skipped": True, "error": None},
        "open_orders": [],
        "positions": [],
        "slots": [],
        "plan": {"present": False},
        "trades": [],
        "pnl": {
            "realized": 0.0,
            "realized_gross": None,
            "mtm": 0.0,
            "total_est": 0.0,
            "cost_basis": 0.0,
            "round_trip_count": 0,
            "round_trips": [],
            "pnl_source": None,
            "cumulative_3d": None,
            "win_rate": {
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
                "empty_reason": "bot_unavailable",
            },
        },
        "files_present": [],
    }


def load_portfolio_config() -> dict:
    """Optional dashboard/portfolio.json or PORTFOLIO_CONFIG path."""
    env_path = os.environ.get("PORTFOLIO_CONFIG")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).resolve().parent.parent / "portfolio.json")
    for path in candidates:
        data = read_json(path, None)
        if isinstance(data, dict):
            return data
    return {}


def resolve_bot_root(bot_id: str, default: Path) -> Path:
    cfg = load_portfolio_config()
    bots = cfg.get("bots") or {}
    entry = bots.get(bot_id) or {}
    if isinstance(entry, dict) and entry.get("root"):
        return Path(os.path.expanduser(str(entry["root"]))).resolve()
    if isinstance(entry, str) and entry:
        return Path(os.path.expanduser(entry)).resolve()
    return Path(default).expanduser().resolve()
