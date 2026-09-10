"""Domain models for the paper grid bot."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional
from datetime import datetime, date


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    CANCELING = "canceling"
    FILLED = "filled"
    CANCELED = "canceled"


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    price: int
    qty: int
    status: OrderStatus = OrderStatus.PENDING
    client_tag: str = ""  # e.g. "grid_buy_L1", "tp_sell@12345"
    created_at: str = ""
    updated_at: str = ""
    fill_price: Optional[int] = None
    fill_qty: int = 0
    linked_buy_price: Optional[int] = None  # for sells: original buy price
    is_ratchet_reorder: bool = False
    is_recycle_rebuy: bool = False  # sell-fill rebuy at original price

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["status"] = self.status.value
        return d


@dataclass
class Position:
    buy_price: int
    qty: int
    date: str  # ISO date when acquired
    sell_order_id: Optional[str] = None
    grid_line: Optional[int] = None  # original limit/grid line; recycle rebuys here

    def to_dict(self) -> dict[str, Any]:
        return {
            "buy_price": self.buy_price,
            "qty": self.qty,
            "date": self.date,
            "sell_order_id": self.sell_order_id,
            "grid_line": self.grid_line,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        gl = d.get("grid_line")
        return cls(
            buy_price=int(d["buy_price"]),
            qty=int(d["qty"]),
            date=str(d["date"]),
            sell_order_id=d.get("sell_order_id"),
            grid_line=int(gl) if gl is not None else None,
        )


@dataclass
class GridSnapshot:
    """Intraday grid parameters recalculated from reference price."""
    ref_price: int
    spacing: int
    buy_lines: list[int]  # descending: highest first (top_buy_line = buy_lines[0])
    day_high: int = 0
    new_buys_filled_today: int = 0

    @property
    def top_buy_line(self) -> Optional[int]:
        return self.buy_lines[0] if self.buy_lines else None


@dataclass
class Event:
    ts: str
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "message": self.message, "data": self.data}
