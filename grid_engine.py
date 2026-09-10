"""Grid trading engine: spacing, placement, fills, ratchet, session end, guards."""
from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Callable, Optional, Set

from models import Order, OrderStatus, Position, Side, GridSnapshot, Event
from state_machine import is_active
from broker_mock import BrokerAdapter
from persistence import load_positions, save_positions
from session import in_session, session_date, ensure_aware, parse_hhmm


LogFn = Callable[[str, str, dict], None]
AlertFn = Callable[[str], None]
MaFn = Callable[[], Optional[float]]  # returns SMA20 daily close or None


def calc_spacing(price: int, pct: float = 0.002, tick: int = 5) -> int:
    """round(price * pct / tick) * tick — daily recalc, tick-aligned."""
    raw = price * pct / tick
    rounded = int(round(raw))
    spacing = rounded * tick
    return max(spacing, tick)  # at least one tick


def build_buy_lines(last: int, spacing: int, levels: int) -> list[int]:
    """First buy = last - 1*spacing; then down for `levels` lines. Highest first."""
    lines = []
    for i in range(1, levels + 1):
        lines.append(last - i * spacing)
    return lines  # [last-s, last-2s, ...] already top-first


class GridEngine:
    def __init__(
        self,
        config: dict[str, Any],
        broker: BrokerAdapter,
        *,
        log: Optional[LogFn] = None,
        positions_path: Optional[str] = None,
        alert: Optional[AlertFn] = None,
        sma20_provider: Optional[MaFn] = None,
    ) -> None:
        self.cfg = config
        self.broker = broker
        self.log_fn = log
        self.alert_fn = alert
        self.sma20_provider = sma20_provider
        self.symbol = config["symbol"]
        pers = config.get("persistence", {})
        self.positions_path = positions_path or pers.get("file", "/workspace/grid-bot/positions.json")
        self.positions: list[Position] = []
        self.grid: Optional[GridSnapshot] = None
        self.events: list[Event] = []
        self._session_started = False
        self._session_ended = False
        self._today: Optional[date] = None

        # Ops guards (cooldown + safety freeze)
        self._clock: Optional[datetime] = None
        self.session_base_0905: Optional[int] = None  # session-start ref for day_drop rule
        self.sma20_daily: Optional[float] = None
        self.safety_frozen: bool = False
        self.freeze_events: list[dict[str, Any]] = []
        self._cooldown_until: dict[int, datetime] = {}  # grid_line -> until (Asia/Seoul)
        self._pending_recycles: Set[int] = set()
        self.cooldown_blocks: int = 0
        self.freeze_blocks: int = 0

    # ---- logging / alerts ----
    def _log(self, kind: str, message: str, data: dict | None = None) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        ev = Event(ts=ts, kind=kind, message=message, data=data or {})
        self.events.append(ev)
        if self.log_fn:
            self.log_fn(kind, message, data or {})
        print(f"[{ts}] {kind:28s} {message}")

    def _alert(self, message: str) -> None:
        if self.alert_fn:
            try:
                self.alert_fn(message)
            except Exception as e:  # noqa: BLE001
                self._log("engine.alert_fail", f"{type(e).__name__}", {})

    def set_sma20(self, value: Optional[float]) -> None:
        """Inject / override SMA20 daily close (tests & stubs)."""
        self.sma20_daily = float(value) if value is not None else None

    def _refresh_sma20(self) -> Optional[float]:
        if self.sma20_provider is not None:
            try:
                v = self.sma20_provider()
                if v is not None:
                    self.sma20_daily = float(v)
            except Exception as e:  # noqa: BLE001
                self._log("engine.sma20_fail", f"{type(e).__name__}", {})
        return self.sma20_daily

    # ---- spacing / grid ----
    def recalc_grid(self, ref_price: int) -> GridSnapshot:
        gcfg = self.cfg["grid"]
        spacing = calc_spacing(ref_price, gcfg["spacing_pct"], gcfg["tick_size"])
        lines = build_buy_lines(ref_price, spacing, gcfg["levels"])
        day_high = ref_price
        if self.grid:
            day_high = max(self.grid.day_high, ref_price)
            nb = self.grid.new_buys_filled_today
        else:
            nb = 0
        self.grid = GridSnapshot(
            ref_price=ref_price,
            spacing=spacing,
            buy_lines=lines,
            day_high=day_high,
            new_buys_filled_today=nb,
        )
        self._log(
            "grid.recalc",
            f"ref={ref_price} spacing={spacing} lines={lines}",
            {"ref": ref_price, "spacing": spacing, "buy_lines": lines},
        )
        return self.grid

    def holdings_qty(self) -> int:
        return sum(p.qty for p in self.positions)

    def can_place_new_buy(self) -> bool:
        limits = self.cfg["limits"]
        if self.holdings_qty() >= limits["max_holdings"]:
            return False
        if self.grid and self.grid.new_buys_filled_today >= limits["max_new_buys_per_day"]:
            return False
        return True

    def _active_buy_prices(self) -> set[int]:
        prices = set()
        for o in self.broker.list_open_orders(self.symbol):
            if o.side == Side.BUY and is_active(o):
                prices.add(o.price)
        return prices

    def _active_sell_prices(self) -> set[int]:
        prices = set()
        for o in self.broker.list_open_orders(self.symbol):
            if o.side == Side.SELL and is_active(o):
                prices.add(o.price)
        return prices

    def _cooldown_enabled(self) -> bool:
        return bool(self.cfg.get("cooldown", {}).get("enabled", False))

    def _freeze_enabled(self) -> bool:
        return bool(self.cfg.get("safety_freeze", {}).get("enabled", False))

    def _now(self) -> datetime:
        tz = self.cfg.get("timezone", "Asia/Seoul")
        if self._clock is not None:
            return ensure_aware(self._clock, tz)
        return ensure_aware(datetime.now(), tz)

    def _is_cooling(self, price: int, now: Optional[datetime] = None) -> bool:
        if not self._cooldown_enabled():
            return False
        until = self._cooldown_until.get(int(price))
        if until is None:
            return False
        now = ensure_aware(now or self._now(), self.cfg.get("timezone", "Asia/Seoul"))
        until = ensure_aware(until, self.cfg.get("timezone", "Asia/Seoul"))
        return now < until

    def place_buy(
        self,
        price: int,
        *,
        tag: str = "",
        ratchet: bool = False,
        recycle: bool = False,
        bootstrap: bool = False,
    ) -> Optional[Order]:
        price = int(price)
        now = self._now()

        # Safety freeze: block ALL new buy submits (bootstrap / recycle / ratchet)
        if self._freeze_enabled() and self.safety_frozen:
            self.freeze_blocks += 1
            self._log(
                "engine.freeze_block",
                f"BUY @{price} blocked (safety_freeze) tag={tag or 'buy'}",
                {"price": price, "tag": tag, "ratchet": ratchet, "recycle": recycle, "bootstrap": bootstrap},
            )
            return None

        # Cooldown: block recycle / new buy at same grid_line price (NOT bootstrap)
        if not bootstrap and self._is_cooling(price, now):
            self.cooldown_blocks += 1
            until = self._cooldown_until[price]
            self._log(
                "cooldown_block",
                f"BUY @{price} blocked (cooldown until {until.isoformat(timespec='seconds')})",
                {
                    "price": price,
                    "grid_line": price,
                    "until": until.isoformat(timespec="seconds"),
                    "tag": tag,
                    "recycle": recycle,
                    "ratchet": ratchet,
                },
            )
            return None

        if not recycle and not ratchet and not self.can_place_new_buy():
            self._log(
                "engine.skip_buy",
                f"limit: holdings={self.holdings_qty()} new_buys={self.grid.new_buys_filled_today if self.grid else 0}",
                {},
            )
            return None
        # recycle / ratchet rebuys also blocked by max holdings for NEW inventory,
        # but recycle restores after sell so holdings already decreased.
        if not recycle and self.holdings_qty() >= self.cfg["limits"]["max_holdings"]:
            self._log("engine.skip_buy", "max holdings — manage sells only", {})
            return None
        if price in self._active_buy_prices():
            self._log("engine.skip_dup", f"BUY @{price} already active", {})
            return None
        order = Order(
            order_id="",
            symbol=self.symbol,
            side=Side.BUY,
            price=price,
            qty=self.cfg["grid"]["qty_per_order"],
            client_tag=tag or f"grid_buy@{price}",
            is_ratchet_reorder=ratchet,
            is_recycle_rebuy=recycle,
        )
        try:
            return self.broker.submit(order)
        except ValueError as e:
            self._log("engine.submit_fail", str(e), {})
            return None

    def place_sell(self, buy_price: int, *, tag: str = "") -> Optional[Order]:
        assert self.grid is not None
        sell_price = buy_price + self.grid.spacing
        if sell_price in self._active_sell_prices():
            self._log("engine.skip_dup", f"SELL @{sell_price} already active", {})
            return None
        order = Order(
            order_id="",
            symbol=self.symbol,
            side=Side.SELL,
            price=sell_price,
            qty=self.cfg["grid"]["qty_per_order"],
            client_tag=tag or f"tp_sell@{buy_price}",
            linked_buy_price=buy_price,
        )
        try:
            o = self.broker.submit(order)
            # link to position
            for p in self.positions:
                if p.buy_price == buy_price and p.sell_order_id is None:
                    p.sell_order_id = o.order_id
                    break
            return o
        except ValueError as e:
            self._log("engine.submit_fail", str(e), {})
            return None

    def place_initial_grid(self) -> list[Order]:
        assert self.grid is not None
        placed: list[Order] = []
        if self._freeze_enabled() and self.safety_frozen:
            self._log("engine.grid_skip", "cannot place new buys (safety_freeze)", {})
            return placed
        if not self.can_place_new_buy():
            self._log("engine.grid_skip", "cannot place new buys (limits)", {})
            return placed
        for i, px in enumerate(self.grid.buy_lines):
            if not self.can_place_new_buy():
                break
            # pending order count doesn't reduce daily fill budget; we place all levels
            # but fills are capped. Still respect max holdings roughly by not over-placing.
            remaining_slots = self.cfg["limits"]["max_holdings"] - self.holdings_qty()
            open_buys = len([o for o in self.broker.list_open_orders(self.symbol) if o.side == Side.BUY])
            if open_buys >= remaining_slots and remaining_slots >= 0:
                # still allow placing up to levels; holdings check on fill
                pass
            # bootstrap=True: morning grid is NOT blocked by cooldown
            o = self.place_buy(px, tag=f"grid_L{i+1}@{px}", bootstrap=True)
            if o:
                placed.append(o)
        self._log("engine.grid_placed", f"placed {len(placed)} buy orders", {"ids": [o.order_id for o in placed]})
        return placed

    # ---- safety freeze ----
    def _freeze_reasons(self, last: int) -> list[str]:
        if not self._freeze_enabled():
            return []
        rules = self.cfg.get("safety_freeze", {}).get("rules", [])
        reasons: list[str] = []
        base = self.session_base_0905
        sma = self._refresh_sma20()
        for rule in rules:
            rid = rule.get("id", "")
            if rid == "day_drop_from_base":
                if base is not None and last <= int(base * 0.97):
                    reasons.append(
                        f"day_drop last={last} <= base*0.97={int(base * 0.97)} (base={base})"
                    )
            elif rid == "ma20_breach":
                if sma is not None and last <= int(float(sma) * 0.98):
                    reasons.append(
                        f"ma20_breach last={last} <= sma20*0.98={int(float(sma) * 0.98)} (sma20={sma})"
                    )
        return reasons

    def check_safety_freeze(self, last: int, now: Optional[datetime] = None) -> bool:
        """Evaluate OR freeze rules; auto-clear when all false. Returns frozen state."""
        if now is not None:
            self._clock = ensure_aware(now, self.cfg.get("timezone", "Asia/Seoul"))
        if not self._freeze_enabled():
            if self.safety_frozen:
                self.safety_frozen = False
                self._log("engine.safety_unfreeze", "freeze disabled in config", {"last": last})
            return False
        reasons = self._freeze_reasons(int(last))
        was = self.safety_frozen
        self.safety_frozen = bool(reasons)
        if self.safety_frozen and not was:
            evt = {
                "reasons": reasons,
                "last": int(last),
                "session_base_0905": self.session_base_0905,
                "sma20": self.sma20_daily,
            }
            self.freeze_events.append(evt)
            self._log("engine.safety_freeze", "; ".join(reasons), evt)
            self._alert(
                f"🧊 safety freeze ON ({self.symbol}): " + "; ".join(reasons)
            )
        elif (not self.safety_frozen) and was:
            evt = {
                "reasons": [],
                "last": int(last),
                "session_base_0905": self.session_base_0905,
                "sma20": self.sma20_daily,
            }
            self.freeze_events.append(evt)
            self._log(
                "engine.safety_unfreeze",
                f"cleared last={last} base={self.session_base_0905} sma20={self.sma20_daily}",
                evt,
            )
            self._alert(
                f"✅ safety freeze OFF ({self.symbol}): last={last} "
                f"base={self.session_base_0905} sma20={self.sma20_daily}"
            )
        return self.safety_frozen

    # ---- cooldown / pending recycle ----
    def _start_cooldown(self, grid_line: int, now: datetime) -> datetime:
        secs = float(self.cfg.get("cooldown", {}).get("after_tp_seconds", 60))
        until = ensure_aware(now, self.cfg.get("timezone", "Asia/Seoul")) + timedelta(seconds=secs)
        self._cooldown_until[int(grid_line)] = until
        self._pending_recycles.add(int(grid_line))
        self._log(
            "engine.cooldown_start",
            f"grid_line={grid_line} until={until.isoformat(timespec='seconds')} (+{secs}s)",
            {"grid_line": int(grid_line), "until": until.isoformat(timespec="seconds"), "seconds": secs},
        )
        return until

    def _flush_pending_recycles(self, now: datetime) -> None:
        if self._session_ended or not self._session_started:
            return
        if not self._pending_recycles:
            return
        now = ensure_aware(now, self.cfg.get("timezone", "Asia/Seoul"))
        for px in list(self._pending_recycles):
            until = self._cooldown_until.get(px)
            if until is not None and now < ensure_aware(until, self.cfg.get("timezone", "Asia/Seoul")):
                continue
            if self._freeze_enabled() and self.safety_frozen:
                # keep pending until unfrozen
                continue
            # session / limits checked inside place_buy / can_place_new_buy (recycle bypasses daily new-buy cap)
            o = self.place_buy(px, tag=f"recycle@{px}", recycle=True)
            self._cooldown_until.pop(px, None)
            if o is not None or not (self._freeze_enabled() and self.safety_frozen):
                self._pending_recycles.discard(px)

    # ---- session lifecycle ----
    def on_session_start(self, ref_price: int, now: datetime) -> None:
        now = ensure_aware(now, self.cfg.get("timezone", "Asia/Seoul"))
        self._clock = now
        self._today = session_date(now, self.cfg.get("timezone", "Asia/Seoul"))
        self._session_started = True
        self._session_ended = False
        self.positions = load_positions(self.positions_path)
        # New session: clear prior-day cooldowns; set session base for day_drop rule
        self._cooldown_until.clear()
        self._pending_recycles.clear()
        self.session_base_0905 = int(ref_price)
        self.safety_frozen = False
        self._log(
            "session.start",
            f"date={self._today} holdings={len(self.positions)} ref={ref_price} "
            f"session_base_0905={self.session_base_0905}",
            {
                "date": str(self._today),
                "holdings": len(self.positions),
                "session_base_0905": self.session_base_0905,
            },
        )
        self.recalc_grid(ref_price)
        # Restore sells for existing positions (allowed even if freeze)
        for p in self.positions:
            self.place_sell(p.buy_price, tag=f"restore_sell@{p.buy_price}")
        # Freeze check at session start BEFORE bootstrap buys
        self.check_safety_freeze(ref_price, now)
        self.place_initial_grid()
        self._persist()

    def resume_from_state(
        self,
        *,
        buy_lines: list[int],
        spacing: int,
        ref_price: int,
        day_high: int | None = None,
        new_buys_filled_today: int = 0,
        now: datetime | None = None,
        restored_order_ids: list[str] | None = None,
        session_base_0905: int | None = None,
    ) -> None:
        """Mid-session hydrate: set grid + flags WITHOUT place_initial_grid / on_session_start."""
        tz = self.cfg.get("timezone", "Asia/Seoul")
        now = ensure_aware(now or datetime.now(), tz)
        self._clock = now
        self._today = session_date(now, tz)
        self._session_started = True
        self._session_ended = False
        self.positions = load_positions(self.positions_path)
        ref = int(ref_price)
        sp = int(spacing)
        lines = [int(x) for x in buy_lines]
        dh = max(int(day_high or 0), ref)
        nb = int(new_buys_filled_today)
        self.grid = GridSnapshot(
            ref_price=ref,
            spacing=sp,
            buy_lines=lines,
            day_high=dh,
            new_buys_filled_today=nb,
        )
        # Prefer explicit base; else use stored ref as session base proxy
        self.session_base_0905 = int(session_base_0905) if session_base_0905 is not None else ref
        ids = list(restored_order_ids or [])
        self._log(
            "session.resume",
            (
                f"date={self._today} buy_lines={lines} spacing={sp} "
                f"ref={ref} day_high={dh} new_buys={nb} restored={len(ids)} "
                f"session_base_0905={self.session_base_0905}"
            ),
            {
                "date": str(self._today),
                "buy_lines": lines,
                "spacing": sp,
                "ref_price": ref,
                "day_high": dh,
                "new_buys_filled_today": nb,
                "restored_order_ids": ids,
                "holdings": len(self.positions),
                "session_base_0905": self.session_base_0905,
            },
        )

    def on_session_end(self, now: datetime) -> None:
        if self._session_ended:
            return
        self._session_ended = True
        self._clock = ensure_aware(now, self.cfg.get("timezone", "Asia/Seoul"))
        canceled = []
        for o in list(self.broker.list_open_orders(self.symbol)):
            if o.side == Side.BUY and is_active(o):
                self.broker.cancel(o.order_id)
                canceled.append(o.order_id)
        self._log(
            "session.end",
            f"canceled {len(canceled)} unfilled buys; sells kept",
            {"canceled_ids": canceled},
        )
        self._persist()

    # ---- price tick ----
    def on_price(self, price: int, now: datetime) -> None:
        now = ensure_aware(now, self.cfg.get("timezone", "Asia/Seoul"))
        self._clock = now
        sess = self.cfg["session"]
        tz = self.cfg.get("timezone", "Asia/Seoul")

        if not in_session(now, sess["start"], sess["end"], tz):
            if self._session_started and not self._session_ended:
                if now.time() > parse_hhmm(sess["end"]):
                    self.on_session_end(now)
            return

        if not self._session_started:
            self.on_session_start(price, now)
            # continue to process this tick after start

        if self._session_ended:
            return

        assert self.grid is not None
        self.grid.day_high = max(self.grid.day_high, price)
        self.broker.set_last_price(price)

        # Safety freeze each session tick (OR rules; auto unfreeze)
        self.check_safety_freeze(price, now)

        # Match fills via broker
        fills = []
        if hasattr(self.broker, "match_on_price"):
            fills = self.broker.match_on_price(price)  # type: ignore[attr-defined]
        for o in fills:
            self._handle_fill(o, now)

        # Ratchet after price update (skipped while frozen — would cancel without replace)
        self._maybe_ratchet()

        # After cooldown expires: rebuy at grid_line if session/limits/not frozen
        self._flush_pending_recycles(now)

    def _handle_fill(self, order: Order, now: datetime) -> None:
        assert self.grid is not None
        if order.side == Side.BUY:
            # Count as new buy only if not ratchet reorder and not recycle rebuy
            counts = (not order.is_ratchet_reorder) and (not order.is_recycle_rebuy)
            if counts:
                # Check limits at fill time
                if self.grid.new_buys_filled_today >= self.cfg["limits"]["max_new_buys_per_day"]:
                    self._log(
                        "engine.fill_over_daily",
                        f"BUY fill {order.order_id} but daily new-buy cap reached — still keep position",
                        order.to_dict(),
                    )
                if self.holdings_qty() >= self.cfg["limits"]["max_holdings"]:
                    self._log(
                        "engine.fill_over_holdings",
                        f"BUY fill {order.order_id} but max holdings — record anyway",
                        order.to_dict(),
                    )
                self.grid.new_buys_filled_today += 1

            # buy_price = actual fill (for TP); grid_line = limit/grid line (for recycle)
            pos = Position(
                buy_price=order.fill_price or order.price,
                qty=order.fill_qty or order.qty,
                date=str(self._today or session_date(now)),
                grid_line=order.price,
            )
            self.positions.append(pos)
            self._log(
                "engine.buy_fill",
                f"bought {pos.qty}@{pos.buy_price} id={order.order_id} "
                f"new_buys_today={self.grid.new_buys_filled_today} "
                f"ratchet={order.is_ratchet_reorder} recycle={order.is_recycle_rebuy}",
                {"order_id": order.order_id, "pos": pos.to_dict()},
            )
            # Place TP sell
            self.place_sell(pos.buy_price)
            self._persist()

        elif order.side == Side.SELL:
            buy_px = order.linked_buy_price
            removed = None
            for i, p in enumerate(self.positions):
                if buy_px is not None and p.buy_price == buy_px:
                    removed = self.positions.pop(i)
                    break
                if p.sell_order_id == order.order_id:
                    removed = self.positions.pop(i)
                    break
            if removed is None and self.positions:
                # fallback: match by sell price = buy + spacing
                target_buy = (order.fill_price or order.price) - self.grid.spacing
                for i, p in enumerate(self.positions):
                    if p.buy_price == target_buy:
                        removed = self.positions.pop(i)
                        break
            self._log(
                "engine.sell_fill",
                f"sold @{order.fill_price} id={order.order_id} removed={removed.to_dict() if removed else None}",
                {"order_id": order.order_id},
            )
            self._persist()
            # Recycle: rebuy at original grid line (limit), fallback fill buy_price
            if removed is not None:
                px = int(removed.grid_line or removed.buy_price)
                if self._cooldown_enabled() and self.cfg.get("cooldown", {}).get(
                    "blocks_recycle_rebuy", True
                ):
                    self._start_cooldown(px, now)
                    # Immediate attempt → cooldown_block within 60s
                    self.place_buy(px, tag=f"recycle@{px}", recycle=True)
                else:
                    self.place_buy(px, tag=f"recycle@{px}", recycle=True)

    def _maybe_ratchet(self) -> None:
        rcfg = self.cfg.get("ratchet", {})
        if not rcfg.get("enabled", True) or self.grid is None:
            return
        # While frozen: do not cancel+replace (freeze blocks ratchet_replace_buys;
        # canceling without replace would wrongly remove open buys).
        if self._freeze_enabled() and self.safety_frozen:
            return
        spacing = self.grid.spacing
        safety = 0
        while True:
            safety += 1
            if safety > 20:
                self._log("engine.ratchet_guard", "broke after 20 iterations", {})
                break
            top = self.grid.top_buy_line
            if top is None:
                break
            trigger = top + 2 * spacing
            if self.grid.day_high <= trigger:
                break
            self._log(
                "engine.ratchet",
                f"day_high={self.grid.day_high} > top={top}+2*sp={trigger} → shift buys up by {spacing}",
                {"day_high": self.grid.day_high, "top": top, "trigger": trigger},
            )
            # Cancel pending/open buys only
            canceled_prices: list[int] = []
            for o in list(self.broker.list_open_orders(self.symbol)):
                if o.side == Side.BUY and is_active(o):
                    self.broker.cancel(o.order_id)
                    canceled_prices.append(o.price)
            # Move buy lines up by 1 spacing
            new_lines = [px + spacing for px in self.grid.buy_lines]
            self.grid.buy_lines = new_lines
            self._log(
                "engine.ratchet_lines",
                f"new buy lines={new_lines} canceled={canceled_prices}",
                {"buy_lines": new_lines, "canceled": canceled_prices},
            )
            # Re-place buys (ratchet reorder — does NOT count as new buy)
            # Cooldown only blocks same grid_line price; other lines OK.
            if self.holdings_qty() < self.cfg["limits"]["max_holdings"]:
                for i, px in enumerate(new_lines):
                    self.place_buy(px, tag=f"ratchet_L{i+1}@{px}", ratchet=True)
            if not rcfg.get("repeat_while_condition_holds", True):
                break

    def _persist(self) -> None:
        meta = {
            "symbol": self.symbol,
            "spacing": self.grid.spacing if self.grid else None,
            "buy_lines": self.grid.buy_lines if self.grid else None,
            "new_buys_filled_today": self.grid.new_buys_filled_today if self.grid else 0,
            "day_high": self.grid.day_high if self.grid else None,
            "session_base_0905": self.session_base_0905,
            "safety_frozen": self.safety_frozen,
        }
        save_positions(self.positions, self.positions_path, meta=meta)
