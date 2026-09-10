"""Acceptance tests for cooldown + safety_freeze (367380 grid_line scope).

Covers ops_guards_proposed.json acceptance_tests:
1. TP sell fill then immediate recycle within 60s → blocked; after 60s → rebuy if not frozen
2. last <= base*0.97 → no new buys; open TP sells remain
3. last <= sma20*0.98 → no new buys including ratchet replace
4. freeze clears when both conditions false → recycle/ratchet may resume
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from broker_mock import MockBroker
from grid_engine import GridEngine, calc_spacing, build_buy_lines
from models import Side
from persistence import save_positions
from state_machine import is_active

SEOUL = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent


def _base_cfg(**overrides) -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["cooldown"] = {
        "enabled": True,
        "after_tp_seconds": 60,
        "scope": "same_grid_line",
        "trigger": "tp_sell_fill",
        "blocks_recycle_rebuy": True,
    }
    cfg["safety_freeze"] = {
        "enabled": True,
        "combine": "or",
        "rules": [
            {
                "id": "ma20_breach",
                "condition": "last <= ma20_daily_close * 0.98",
                "action": "freeze_new_buys",
                "ma": {"type": "sma", "period": 20, "bar": "daily", "price": "close"},
            },
            {
                "id": "day_drop_from_base",
                "condition": "last <= session_base_0905 * 0.97",
                "baseline": "session_base_0905",
                "action": "freeze_new_buys",
            },
        ],
        "unfreeze": "auto_when_rules_clear_or_next_session",
        "does_not_cancel_existing_tp_sells": True,
    }
    cfg.update(overrides)
    return cfg


def _engine(cfg, *, ref: int = 25000, sma20=None, positions_path=None):
    events: list[dict] = []
    alerts: list[str] = []

    def log_fn(kind, message, data):
        events.append({"kind": kind, "message": message, "data": data})

    path = positions_path or tempfile.mktemp(suffix="-positions.json")
    save_positions([], path, meta={"note": "test reset"})
    broker = MockBroker(symbol=cfg["symbol"], last_price=ref, log=log_fn)
    eng = GridEngine(
        cfg,
        broker,
        log=log_fn,
        positions_path=path,
        alert=lambda m: alerts.append(m),
        sma20_provider=(None if sma20 is None else (lambda: sma20)),
    )
    if sma20 is not None:
        eng.set_sma20(sma20)
    eng._test_events = events  # type: ignore[attr-defined]
    eng._test_alerts = alerts  # type: ignore[attr-defined]
    return eng, broker, events, alerts


def _t(h, m, s=0, day=11) -> datetime:
    return datetime(2026, 9, day, h, m, s, tzinfo=SEOUL)


class TestCooldown(unittest.TestCase):
    def test_tp_sell_recycle_blocked_60s_then_allowed(self):
        """TP sell fill → immediate recycle blocked; after 60s → rebuy if not frozen."""
        cfg = _base_cfg()
        # Soften freeze so day_drop doesn't interfere (sma high / base far)
        ref = 25000
        eng, broker, events, _ = _engine(cfg, ref=ref, sma20=20000)

        eng.on_session_start(ref, _t(9, 5))
        spacing = eng.grid.spacing
        buy1 = ref - spacing
        self.assertTrue(any(o.price == buy1 and o.side == Side.BUY for o in broker.list_open_orders()))

        # Fill buy
        eng.on_price(buy1, _t(10, 0))
        self.assertTrue(any(e["kind"] == "engine.buy_fill" for e in events))
        sells = [o for o in broker.list_open_orders() if o.side == Side.SELL]
        self.assertTrue(sells)
        tp = sells[0].price

        # Fill TP sell
        eng.on_price(tp, _t(10, 30, 0))
        self.assertTrue(any(e["kind"] == "engine.sell_fill" for e in events))
        self.assertTrue(any(e["kind"] == "engine.cooldown_start" for e in events))
        self.assertTrue(any(e["kind"] == "cooldown_block" for e in events))

        # Still within 60s — no recycle buy at grid_line
        eng.on_price(tp, _t(10, 30, 30))
        open_buys_at_line = [
            o for o in broker.list_open_orders()
            if o.side == Side.BUY and o.price == buy1 and is_active(o)
        ]
        self.assertEqual(open_buys_at_line, [], "recycle must be blocked within 60s")

        # After 60s — rebuy allowed
        eng.on_price(tp - 5, _t(10, 31, 5))
        open_buys_at_line = [
            o for o in broker.list_open_orders()
            if o.side == Side.BUY and o.price == buy1 and is_active(o)
        ]
        self.assertTrue(open_buys_at_line, "recycle rebuy after 60s")
        recycle = open_buys_at_line[0]
        self.assertTrue(recycle.is_recycle_rebuy)

    def test_cooldown_does_not_block_other_grid_lines(self):
        cfg = _base_cfg()
        ref = 25000
        eng, broker, events, _ = _engine(cfg, ref=ref, sma20=20000)
        eng.on_session_start(ref, _t(9, 5))
        spacing = eng.grid.spacing
        buy1 = ref - spacing
        buy2 = ref - 2 * spacing

        eng.on_price(buy1, _t(10, 0))
        tp = buy1 + spacing
        eng.on_price(tp, _t(10, 30, 0))
        self.assertTrue(any(e["kind"] == "cooldown_block" for e in events))

        # Other line still has its open buy (never canceled by cooldown)
        other = [
            o for o in broker.list_open_orders()
            if o.side == Side.BUY and o.price == buy2 and is_active(o)
        ]
        self.assertTrue(other, "other grid_line buys unaffected")


class TestSafetyFreezeDayDrop(unittest.TestCase):
    def test_day_drop_blocks_new_buys_keeps_tp_sells(self):
        """last <= base*0.97 → no new buys; open TP sells remain."""
        cfg = _base_cfg()
        ref = 25000
        # sma very low so only day_drop fires
        eng, broker, events, alerts = _engine(cfg, ref=ref, sma20=10000)
        eng.on_session_start(ref, _t(9, 5))
        spacing = eng.grid.spacing
        buy1 = ref - spacing

        # Fill one buy → TP sell open
        eng.on_price(buy1, _t(10, 0))
        tp_sells_before = [o for o in broker.list_open_orders() if o.side == Side.SELL]
        self.assertTrue(tp_sells_before)
        tp_ids = {o.order_id for o in tp_sells_before}

        # Cancel remaining unfilled buys so a deep freeze tick does not fill them
        for o in list(broker.list_open_orders()):
            if o.side == Side.BUY and is_active(o):
                broker.cancel(o.order_id)

        freeze_px = int(ref * 0.97)
        # Evaluate freeze without relying on matcher side-effects for other lines
        eng.check_safety_freeze(freeze_px, _t(11, 0))
        self.assertTrue(eng.safety_frozen)
        self.assertTrue(any(e["kind"] == "engine.safety_freeze" for e in events))
        self.assertTrue(any("safety freeze ON" in a for a in alerts))

        # Existing TP sells remain (freeze must not cancel them)
        tp_sells_after = [o for o in broker.list_open_orders() if o.side == Side.SELL]
        self.assertEqual({o.order_id for o in tp_sells_after}, tp_ids)

        # New buy submit blocked
        blocked = eng.place_buy(buy1 - spacing, tag="manual_test")
        self.assertIsNone(blocked)
        self.assertTrue(any(e["kind"] == "engine.freeze_block" for e in events))

        # Recycle also blocked
        before_blocks = eng.freeze_blocks
        eng._pending_recycles.add(buy1)
        eng._cooldown_until.clear()
        eng._flush_pending_recycles(_t(11, 1))
        self.assertGreaterEqual(eng.freeze_blocks, before_blocks)
        self.assertIn(buy1, eng._pending_recycles)


class TestSafetyFreezeMa20(unittest.TestCase):
    def test_ma20_blocks_ratchet_replace(self):
        """last <= sma20*0.98 → no new buys including ratchet replace."""
        cfg = _base_cfg()
        ref = 25000
        # sma20 such that sma*0.98 is just below a rally price we will use... 
        # We want: start normal, place grid, then set last low vs sma to freeze,
        # then try ratchet.
        sma20 = 26000  # sma*0.98 = 25480
        eng, broker, events, _ = _engine(cfg, ref=ref, sma20=sma20)
        eng.on_session_start(ref, _t(9, 5))
        # At ref=25000 < 25480 → should already be frozen at session start
        self.assertTrue(eng.safety_frozen)
        # Bootstrap buys blocked
        open_buys = [o for o in broker.list_open_orders() if o.side == Side.BUY]
        self.assertEqual(open_buys, [], "bootstrap blocked when ma20 freeze at start")

        # Even if we force unfreeze and place, then re-freeze, ratchet replace blocked
        eng.safety_frozen = False
        eng.place_initial_grid()
        open_buys = [o for o in broker.list_open_orders() if o.side == Side.BUY]
        self.assertTrue(open_buys)
        buy_ids = {o.order_id for o in open_buys}

        # Re-enter freeze via price tick
        eng.on_price(int(sma20 * 0.98), _t(10, 0))
        self.assertTrue(eng.safety_frozen)

        # Force ratchet condition: bump day_high high while frozen
        eng.grid.day_high = eng.grid.top_buy_line + 2 * eng.grid.spacing + 100
        before_lines = list(eng.grid.buy_lines)
        eng._maybe_ratchet()
        # While frozen, ratchet should no-op (no cancel/replace)
        after_buys = [o for o in broker.list_open_orders() if o.side == Side.BUY]
        self.assertEqual({o.order_id for o in after_buys}, buy_ids)
        self.assertEqual(eng.grid.buy_lines, before_lines)

        # Direct ratchet place_buy also blocked
        self.assertIsNone(eng.place_buy(before_lines[0] + eng.grid.spacing, ratchet=True))


class TestFreezeClear(unittest.TestCase):
    def test_unfreeze_when_both_rules_false(self):
        """Freeze clears when both conditions false → recycle/ratchet may resume."""
        cfg = _base_cfg()
        ref = 25000
        sma20 = 20000  # sma*0.98=19600 — day_drop is binding (base*0.97=24250)
        eng, broker, events, alerts = _engine(cfg, ref=ref, sma20=sma20)
        eng.on_session_start(ref, _t(9, 5))
        self.assertFalse(eng.safety_frozen)

        # Cancel open buys so freeze check does not fill the whole grid
        for o in list(broker.list_open_orders()):
            if o.side == Side.BUY and is_active(o):
                broker.cancel(o.order_id)

        freeze_px = int(ref * 0.97)
        eng.check_safety_freeze(freeze_px, _t(11, 0))
        self.assertTrue(eng.safety_frozen)

        # Recover above both thresholds
        recover = ref - 10  # > base*0.97 and > sma*0.98
        eng.check_safety_freeze(recover, _t(11, 30))
        self.assertFalse(eng.safety_frozen)
        self.assertTrue(any(e["kind"] == "engine.safety_unfreeze" for e in events))
        self.assertTrue(any("safety freeze OFF" in a for a in alerts))

        # New buys allowed again (reset daily counter so limits do not mask unfreeze)
        eng.grid.new_buys_filled_today = 0
        free = min(eng.grid.buy_lines) - eng.grid.spacing
        o = eng.place_buy(free, tag="post_unfreeze")
        self.assertIsNotNone(o)

        # Ratchet can run again when condition holds
        eng.grid.day_high = eng.grid.top_buy_line + 2 * eng.grid.spacing + 50
        eng._maybe_ratchet()
        self.assertTrue(any(e["kind"] == "engine.ratchet" for e in events))


class TestSessionEndAndBootstrap(unittest.TestCase):
    def test_session_end_cancels_buys_keeps_sells_during_freeze(self):
        cfg = _base_cfg()
        ref = 25000
        eng, broker, _, _ = _engine(cfg, ref=ref, sma20=10000)
        eng.on_session_start(ref, _t(9, 5))
        buy1 = ref - eng.grid.spacing
        eng.on_price(buy1, _t(10, 0))
        eng.on_price(int(ref * 0.97), _t(11, 0))
        self.assertTrue(eng.safety_frozen)
        eng.on_session_end(_t(15, 21))
        buys = [o for o in broker.list_open_orders() if o.side == Side.BUY]
        sells = [o for o in broker.list_open_orders() if o.side == Side.SELL]
        self.assertEqual(buys, [])
        self.assertTrue(sells)

    def test_bootstrap_not_blocked_by_stale_cooldown(self):
        cfg = _base_cfg()
        ref = 25000
        eng, broker, _, _ = _engine(cfg, ref=ref, sma20=10000)
        # Stale cooldown from "yesterday"
        eng._cooldown_until[ref - 50] = _t(9, 4) + timedelta(seconds=30)
        eng.on_session_start(ref, _t(9, 5))
        # Session start clears cooldowns and places grid
        self.assertEqual(eng._cooldown_until, {})
        buys = [o for o in broker.list_open_orders() if o.side == Side.BUY]
        self.assertEqual(len(buys), cfg["grid"]["levels"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
