"""Mock exchange adapter for paper trading. No live broker calls."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
import itertools

from models import Order, OrderStatus, Side
from state_machine import transition, can_cancel, is_active, OrderStateError


class BrokerAdapter(ABC):
    """Interface: MockBroker (paper) / LiveKISBroker (live, see broker_kis)."""

    @abstractmethod
    def submit(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel(self, order_id: str) -> Order: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]: ...

    @abstractmethod
    def list_open_orders(self, symbol: Optional[str] = None) -> list[Order]: ...

    @abstractmethod
    def set_last_price(self, price: int) -> None: ...

    @abstractmethod
    def get_last_price(self) -> int: ...


# LiveKISStub deprecated: use broker_kis.LiveKISBroker (lazy re-export).
def __getattr__(name: str):
    if name in ("LiveKISStub", "LiveKISBroker", "LiveOrderApprovalError"):
        from broker_kis import LiveKISBroker, LiveOrderApprovalError

        if name == "LiveKISStub":
            return LiveKISBroker
        if name == "LiveKISBroker":
            return LiveKISBroker
        return LiveOrderApprovalError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


FillCallback = Callable[[Order], None]


@dataclass
class MockBroker(BrokerAdapter):
    """Simulated exchange: immediate ack, price-crossing fills."""

    symbol: str = "367380"
    last_price: int = 0
    _orders: dict[str, Order] = field(default_factory=dict)
    _id_seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    on_fill: Optional[FillCallback] = None
    log: Optional[Callable[[str, str, dict], None]] = None

    def _oid(self) -> str:
        return f"MOCK-{next(self._id_seq):06d}"

    def _emit(self, kind: str, msg: str, data: dict | None = None) -> None:
        if self.log:
            self.log(kind, msg, data or {})

    def set_last_price(self, price: int) -> None:
        self.last_price = int(price)

    def get_last_price(self) -> int:
        return self.last_price

    def submit(self, order: Order) -> Order:
        # Prevent duplicate active orders at same side+price
        for existing in self._orders.values():
            if (
                is_active(existing)
                and existing.symbol == order.symbol
                and existing.side == order.side
                and existing.price == order.price
            ):
                self._emit(
                    "broker.reject_dup",
                    f"duplicate {order.side.value} @{order.price} blocked",
                    {"existing_id": existing.order_id},
                )
                raise ValueError(
                    f"duplicate price order blocked: {order.side.value} @{order.price}"
                )

        if not order.order_id:
            order.order_id = self._oid()
        order.status = OrderStatus.PENDING
        now = datetime.now().isoformat(timespec="seconds")
        order.created_at = now
        order.updated_at = now
        self._orders[order.order_id] = order
        self._emit(
            "broker.submit",
            f"{order.side.value} {order.qty}@{order.price} id={order.order_id}",
            order.to_dict(),
        )
        # Immediate ack -> open
        transition(order, "submit_ack")
        order.updated_at = datetime.now().isoformat(timespec="seconds")
        self._emit(
            "broker.ack",
            f"{order.order_id} -> open",
            {"order_id": order.order_id, "status": order.status.value},
        )
        # Maybe immediate fill if price already crossed
        self._try_fill(order)
        return order

    def cancel(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"unknown order_id={order_id}")
        if not can_cancel(order) and order.status != OrderStatus.CANCELING:
            self._emit(
                "broker.cancel_skip",
                f"cannot cancel {order_id} status={order.status.value}",
                {},
            )
            return order
        if order.status != OrderStatus.CANCELING:
            transition(order, "request_cancel")
            order.updated_at = datetime.now().isoformat(timespec="seconds")
            self._emit(
                "broker.cancel_req",
                f"{order_id} -> canceling",
                {"order_id": order_id, "status": order.status.value},
            )
        # Mock: immediate cancel ack (unless already filled)
        if order.status == OrderStatus.CANCELING:
            transition(order, "cancel_ack")
            order.updated_at = datetime.now().isoformat(timespec="seconds")
            self._emit(
                "broker.cancel_ack",
                f"{order_id} -> canceled",
                {"order_id": order_id, "status": order.status.value},
            )
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def list_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        out = []
        for o in self._orders.values():
            if is_active(o) and (symbol is None or o.symbol == symbol):
                out.append(o)
        return out

    def match_on_price(self, price: int) -> list[Order]:
        """Advance last price and fill any crossed active orders. Returns fills."""
        self.last_price = int(price)
        fills: list[Order] = []
        for o in list(self._orders.values()):
            if not is_active(o):
                continue
            before = o.status
            self._try_fill(o)
            if o.status == OrderStatus.FILLED and before != OrderStatus.FILLED:
                fills.append(o)
        return fills

    def _try_fill(self, order: Order) -> None:
        if not is_active(order):
            return
        px = self.last_price
        if px <= 0:
            return
        hit = False
        if order.side == Side.BUY and px <= order.price:
            hit = True
        elif order.side == Side.SELL and px >= order.price:
            hit = True
        if not hit:
            return
        try:
            transition(order, "fill", fill_price=order.price, fill_qty=order.qty)
        except OrderStateError:
            return
        order.updated_at = datetime.now().isoformat(timespec="seconds")
        self._emit(
            "broker.fill",
            f"{order.order_id} {order.side.value} filled @{order.fill_price}",
            order.to_dict(),
        )
        if self.on_fill:
            self.on_fill(order)


@dataclass
class ShadowBroker(MockBroker):
    """Dry-loop broker: same fill matching as MockBroker, but order ids and
    intent logs are clearly marked ``shadow`` so operators can grep that no
    live order_cash / order_rvsecncl ran.

    Drive this with live inquire_price ticks; never wires to KIS mutations.
    """

    intent_log_path: Optional[str] = None

    def _oid(self) -> str:
        return f"SHADOW-{next(self._id_seq):06d}"

    def _shadow_intent(self, action: str, order: Order) -> None:
        from persistence import append_jsonl
        from session import SEOUL

        rec = {
            "ts": datetime.now(SEOUL).isoformat(timespec="seconds"),
            "action": f"shadow_{action}",
            "order_id": order.order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "price": order.price,
            "qty": order.qty,
            "client_tag": order.client_tag,
            "status": order.status.value,
            "note": "dry-loop only — no KIS order API",
        }
        self._emit("shadow.intent", f"{action} {order.side.value} {order.qty}@{order.price}", rec)
        if self.intent_log_path:
            append_jsonl(self.intent_log_path, rec)

    def submit(self, order: Order) -> Order:
        o = super().submit(order)
        self._shadow_intent("submit", o)
        return o

    def cancel(self, order_id: str) -> Order:
        o = super().cancel(order_id)
        self._shadow_intent("cancel", o)
        return o
