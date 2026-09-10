"""Persist holdings to positions.json."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from models import Position


DEFAULT_PATH = "/workspace/grid-bot/positions.json"


def load_positions(path: str = DEFAULT_PATH) -> list[Position]:
    p = Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    items = raw.get("positions", raw if isinstance(raw, list) else [])
    return [Position.from_dict(x) for x in items]


def save_positions(positions: list[Position], path: str = DEFAULT_PATH, meta: dict[str, Any] | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "positions": [pos.to_dict() for pos in positions],
        "count": len(positions),
        "total_qty": sum(pos.qty for pos in positions),
    }
    if meta:
        payload["meta"] = meta
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def append_jsonl(path: str, record: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
