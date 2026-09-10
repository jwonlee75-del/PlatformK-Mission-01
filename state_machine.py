"""Order state machine: pending -> open -> canceling -> filled/canceled."""
from __future__ import annotations

from typing import Optional

from models import Order, OrderStatus


class OrderStateError(Exception):
    pass


ALLOWED = {
    # event -> {from_status: to_status}
    "submit_ack": {
        OrderStatus.PENDING: OrderStatus.OPEN,
    },
    "request_cancel": {
        OrderStatus.PENDING: OrderStatus.CANCELING,
        OrderStatus.OPEN: OrderStatus.CANCELING,
    },
    "cancel_ack": {
        OrderStatus.CANCELING: OrderStatus.CANCELED,
    },
    "fill": {
        OrderStatus.PENDING: OrderStatus.FILLED,
        OrderStatus.OPEN: OrderStatus.FILLED,
        OrderStatus.CANCELING: OrderStatus.FILLED,  # fill wins over cancel
    },
}


def transition(order: Order, event: str, *, fill_price: Optional[int] = None, fill_qty: int = 0) -> Order:
    """Apply a state transition. Mutates and returns the order."""
    mapping = ALLOWED.get(event)
    if mapping is None:
        raise OrderStateError(f"unknown event: {event}")
    nxt = mapping.get(order.status)
    if nxt is None:
        raise OrderStateError(
            f"illegal transition: {order.status.value} --{event}--> ? "
            f"(order_id={order.order_id})"
        )
    order.status = nxt
    if event == "fill":
        order.fill_price = fill_price if fill_price is not None else order.price
        order.fill_qty = fill_qty or order.qty
    return order


def can_cancel(order: Order) -> bool:
    return order.status in (OrderStatus.PENDING, OrderStatus.OPEN)


def is_active(order: Order) -> bool:
    return order.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.CANCELING)
