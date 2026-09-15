"""Unit tests for cumulative grid win rate (explicit round_trips only)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASH = ROOT / "dashboard"
MULTI = DASH / "multi"
sys.path.insert(0, str(DASH))
sys.path.insert(0, str(MULTI))

from adapter_091170 import build_091170  # noqa: E402
from build_portfolio_status import build_portfolio_status  # noqa: E402
from build_status import build_status  # noqa: E402
from common import empty_bot, now_seoul  # noqa: E402
from win_rate import (  # noqa: E402
    classify_rt,
    combine_win_rates,
    compute_win_rate,
    rt_pnl_gross,
)


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _rt(pnl=None, **extra) -> dict:
    row = dict(extra)
    if pnl is not None:
        row["pnl_gross"] = pnl
    return row


def _ledger_root(tmp: Path, *, archives: dict[str, list], today_led: dict | None = None) -> Path:
    root = tmp / "bot"
    root.mkdir(parents=True, exist_ok=True)
    for ymd, rts in archives.items():
        iso = f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
        _write(
            root / "ledger_archive" / f"day_ledger-{ymd}.json",
            {"date": iso, "round_trips": rts},
        )
    if today_led is not None:
        _write(root / "day_ledger.json", today_led)
    return root


class TestRtHelpers(unittest.TestCase):
    def test_pnl_gross_prefers_explicit_then_prices(self):
        self.assertEqual(rt_pnl_gross({"pnl_gross": 60, "pnl": 1}), 60.0)
        self.assertEqual(rt_pnl_gross({"pnl": -12}), -12.0)
        self.assertEqual(rt_pnl_gross({"buy": 10000, "sell": 10050, "qty": 2}), 100.0)
        self.assertEqual(rt_pnl_gross({"buy_price": 100, "sell_price": 90}), -10.0)
        self.assertIsNone(rt_pnl_gross({"qty": 1}))
        self.assertIsNone(rt_pnl_gross("nope"))

    def test_classify_excludes_breakeven_from_wins(self):
        self.assertEqual(classify_rt(1), "win")
        self.assertEqual(classify_rt(-0.01), "loss")
        self.assertEqual(classify_rt(0), "breakeven")
        self.assertIsNone(classify_rt(None))


class TestComputeWinRate(unittest.TestCase):
    def test_archive_plus_today_and_live_override(self):
        today = date(2026, 9, 15)
        with tempfile.TemporaryDirectory() as td:
            root = _ledger_root(
                Path(td),
                archives={
                    "20260911": [_rt(60), _rt(60)],
                    "20260912": [_rt(-30), _rt(0)],
                    "20260915": [_rt(99)],  # same-day archive, must lose to live
                },
                today_led={
                    "session_date": "2026-09-15",
                    "round_trips": [_rt(10), _rt(-5), _rt(0)],
                },
            )
            wr = compute_win_rate(root, today=today)
        self.assertTrue(wr["today_included"])
        self.assertEqual(wr["days"], 3)
        self.assertEqual(wr["wins"], 3)  # 2 + 0 + 1 (live)
        self.assertEqual(wr["losses"], 2)  # 1 + 1
        self.assertEqual(wr["breakeven"], 2)
        self.assertEqual(wr["decided"], 5)
        self.assertEqual(wr["round_trips"], 7)
        self.assertAlmostEqual(wr["win_rate"], 3 / 5)
        self.assertEqual(wr["win_rate_pct"], 60.0)
        self.assertEqual(wr["source"], "ledger_archive+today")
        by_date = {r["date"]: r for r in wr["per_day"]}
        self.assertEqual(by_date["2026-09-15"]["source"], "day_ledger.json")
        self.assertEqual(by_date["2026-09-15"]["wins"], 1)
        self.assertEqual(by_date["2026-09-15"]["losses"], 1)
        self.assertNotIn("skipped", wr["per_day"][0])

    def test_fills_without_explicit_rts_do_not_count(self):
        today = date(2026, 9, 15)
        with tempfile.TemporaryDirectory() as td:
            root = _ledger_root(Path(td), archives={})
            _write(
                root / "ledger_archive" / "day_ledger-20260915.json",
                {
                    "date": "2026-09-15",
                    "fills": [
                        {"side": "BUY", "price": 10000, "qty": 5, "slot_id": 1},
                        {"side": "SELL", "price": 10050, "qty": 5, "slot_id": 1},
                    ],
                    "realized_gross": 250,
                },
            )
            wr = compute_win_rate(root, today=today)
        self.assertEqual(wr["days"], 1)
        self.assertEqual(wr["wins"], 0)
        self.assertEqual(wr["losses"], 0)
        self.assertIsNone(wr["win_rate"])
        self.assertEqual(wr["empty_reason"], "no_decided_round_trips")

    def test_undated_live_only_when_calendar_today(self):
        cal = now_seoul().date()
        other_day = date.fromordinal(cal.toordinal() - 1)
        with tempfile.TemporaryDirectory() as td:
            root = _ledger_root(Path(td), archives={})
            _write(root / "day_ledger.json", {"round_trips": [_rt(40)]})
            as_today = compute_win_rate(root, today=cal)
            other = compute_win_rate(root, today=other_day)
        self.assertEqual(as_today["wins"], 1)
        self.assertTrue(as_today["today_included"])
        self.assertEqual(other["wins"], 0)
        self.assertFalse(other["today_included"])
        self.assertEqual(other["empty_reason"], "no_ledgers")

    def test_missing_root(self):
        wr = compute_win_rate(Path("/definitely-missing-win-rate-root"))
        self.assertEqual(wr["empty_reason"], "root_missing")
        self.assertIsNone(wr["win_rate"])


class TestCombineAndAdapters(unittest.TestCase):
    def test_hero_sums_then_recomputes(self):
        bots = [
            {
                "ok": True,
                "id": "367380",
                "pnl": {"win_rate": {"wins": 10, "losses": 2, "breakeven": 1, "round_trips": 13, "decided": 12, "days": 3}},
            },
            {
                "ok": True,
                "id": "091170",
                "pnl": {"win_rate": {"wins": 2, "losses": 2, "breakeven": 0, "round_trips": 4, "decided": 4, "days": 2}},
            },
            {
                "ok": False,
                "id": "dead",
                "pnl": {"win_rate": {"wins": 99, "losses": 0, "decided": 99, "days": 9}},
            },
        ]
        hero = combine_win_rates(bots)
        self.assertEqual(hero["wins"], 12)
        self.assertEqual(hero["losses"], 4)
        self.assertEqual(hero["breakeven"], 1)
        self.assertEqual(hero["decided"], 16)
        self.assertAlmostEqual(hero["win_rate"], 12 / 16)
        self.assertEqual(hero["win_rate_pct"], 75.0)
        self.assertEqual(hero["bots"], ["367380", "091170"])
        self.assertEqual(hero["source"], "hero_sum")

    def test_091170_win_rate_uses_archive_not_fill_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "091170"
            _write(root / "config.json", {"symbol": "091170", "symbol_name": "KODEX 은행"})
            _write(root / "positions.json", {"slots": []})
            _write(root / "last_price.json", {"last": 10000})
            _write(root / "plan.json", {})
            _write(root / "orders_state.json", {"open_orders": []})
            _write(
                root / "day_ledger.json",
                {
                    "session_date": "2026-09-11",
                    "fills": [
                        {"side": "BUY", "price": 10000, "qty": 5, "slot_id": 1},
                        {"side": "SELL", "price": 10050, "qty": 5, "slot_id": 1},
                    ],
                },
            )
            _write(
                root / "ledger_archive" / "day_ledger-20260910.json",
                {"date": "2026-09-10", "round_trips": [_rt(50), _rt(-10)]},
            )
            out = build_091170(root=root, try_kis=False)
        wr = out["pnl"]["win_rate"]
        self.assertEqual(wr["wins"], 1)
        self.assertEqual(wr["losses"], 1)
        self.assertEqual(wr["win_rate_pct"], 50.0)
        # Daily PnL may pair fills; win rate must not.
        self.assertEqual(out["pnl"]["realized_gross"], 250.0)
        self.assertEqual(wr["round_trips"], 2)

    def test_empty_bot_has_win_rate(self):
        bot = empty_bot(
            bot_id="091170",
            schema="slots",
            root=Path("/missing"),
            code="root_missing",
            message="nope",
        )
        self.assertEqual(bot["pnl"]["win_rate"]["empty_reason"], "bot_unavailable")
        self.assertIsNone(bot["pnl"]["win_rate"]["win_rate"])

    def test_portfolio_hero_and_single_bot_untouched(self):
        os.environ["DASHBOARD_SKIP_KIS"] = "1"
        os.environ["GRID_BOT_091170_ROOT"] = "/definitely-missing-grid-bot-091170"
        try:
            port = build_portfolio_status(try_kis=False)
            single = build_status(try_kis=False)
        finally:
            os.environ.pop("GRID_BOT_091170_ROOT", None)
        self.assertIn("win_rate", port["hero"])
        by_id = {b["id"]: b for b in port["bots"]}
        self.assertIn("win_rate", by_id["367380"]["pnl"])
        self.assertIn("win_rate", by_id["091170"]["pnl"])
        self.assertNotIn("win_rate", single.get("pnl") or {})
        dumped = json.dumps(port)
        self.assertNotIn("app_key", dumped)
        self.assertNotIn("app_secret", dumped)


if __name__ == "__main__":
    unittest.main()
