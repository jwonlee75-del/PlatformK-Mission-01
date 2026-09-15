"""Unit tests for the two-bot portfolio dashboard (read-only, no KIS)."""
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
from common import redact  # noqa: E402
from cumulative_pnl import last_n_trading_days  # noqa: E402


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, (dict, list)):
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(str(obj), encoding="utf-8")


def _fixture_091170(tmp: Path, *, empty_ledger: bool = False, with_rts: bool = False, secrets: bool = False) -> Path:
    root = tmp / "grid-bot-091170"
    cfg = {
        "symbol": "091170",
        "symbol_name": "KODEX 은행",
        "session": {"start": "09:05", "end": "15:20"},
        "grid": {"levels": 4, "spacing_pct": 0.003, "tp_pct": 0.003, "qty_per_order": 1},
    }
    if secrets:
        cfg["telegram"] = {"chat_id": "9990011222"}
        cfg["safety"] = {"app_key": "SHOULD_NOT_LEAK", "app_secret": "also-secret"}
    _write(root / "config.json", cfg)
    _write(
        root / "positions.json",
        {
            "slots": [
                {"slot": 1, "status": "filled", "buy_price": 10000, "qty": 1, "sell_price": 10030},
                {"slot": 2, "status": "empty"},
                {"slot": 3, "status": "waiting_sell", "buy_price": 9970, "qty": 1, "sell_price": 10000},
            ],
            "meta": {"safety_frozen": True, "safety_reasons": ["ma20_breach"], "base": 10100},
        },
    )
    _write(
        root / "last_price.json",
        {"last": 10020, "base": 10100, "day_high": 10200, "updated_at": "2026-09-11T12:00:00+09:00"},
    )
    _write(
        root / "plan.json",
        {
            "base": 10100,
            "last": 10020,
            "kis_cash": 250000,
            "caps": {"daily_buy": 800000, "order": 100000},
            "buys": [{"slot_id": 2, "price": 9940, "qty": 5, "status": "planned", "odno": "B2"}],
            "tps": [{"slot_id": 1, "price": 10030, "qty": 5, "status": "open", "odno": "S1"}],
        },
    )
    _write(
        root / "orders_state.json",
        {
            "open_orders": [
                {"side": "BUY", "price": 9940, "qty": 5, "status": "open", "slot_id": 2, "order_id": "B2"},
                {"side": "SELL", "price": 10030, "qty": 5, "status": "open", "slot_id": 1, "order_id": "S1"},
            ]
        },
    )
    if empty_ledger:
        _write(root / "day_ledger.json", {"date": "2026-09-11", "fills": [], "base": 10100})
    elif with_rts:
        _write(
            root / "day_ledger.json",
            {
                "date": "2026-09-11",
                "base": 10100,
                "fills": [
                    {"side": "BUY", "price": 10000, "qty": 1, "odno": "1", "tmd": "093000"},
                    {"side": "SELL", "price": 10030, "qty": 1, "odno": "2", "tmd": "100000"},
                ],
                "round_trips": [{"buy": 10000, "sell": 10030, "qty": 1, "pnl_gross": 30}],
                "realized_gross": 30,
                "fees_day_est": 3,
                "tax_est": 5,
                "realized_net_est": 22,
            },
        )
    else:
        # fills present, no RTs / realized_* → must stay zero
        _write(
            root / "day_ledger.json",
            {
                "date": "2026-09-11",
                "base": 10100,
                "fills": [
                    {"side": "BUY", "price": 10000, "qty": 1, "odno": "1", "tmd": "093000"},
                    {"side": "SELL", "price": 10030, "qty": 1, "odno": "2", "tmd": "100000"},
                ],
            },
        )
    _write(root / "LIVE_APPROVED", "1999-01-01\n")
    return root


class TestRedact(unittest.TestCase):
    def test_strips_secret_keys(self):
        blob = redact({"ok": True, "chat_id": "123", "telegram": {"token": "x"}, "nested": {"app_key": "k"}})
        dumped = json.dumps(blob)
        self.assertNotIn("123", dumped)
        self.assertNotIn("token", dumped.lower())
        self.assertNotIn("app_key", dumped)
        self.assertTrue(blob.get("ok"))


class TestAdapter091170(unittest.TestCase):
    def test_missing_root(self):
        out = build_091170(root=Path("/no/such/grid-bot-091170"), try_kis=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "root_missing")
        self.assertEqual(out["pnl"]["realized"], 0.0)

    def test_slots_and_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td))
            out = build_091170(root=root, try_kis=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["overview"]["symbol"], "091170")
        self.assertEqual(out["overview"]["last_price"], 10020)
        self.assertTrue(out["overview"]["safety_frozen"])
        self.assertEqual(len(out["positions"]), 2)
        self.assertEqual(len(out["slots"]), 3)
        sides = {o["side"] for o in out["open_orders"]}
        self.assertIn("BUY", sides)
        self.assertIn("SELL", sides)
        self.assertTrue(out["plan"]["present"])
        self.assertEqual(out["overview"]["base"], 10100)
        self.assertFalse(out["live_approved"]["valid_today"])
        self.assertTrue(out["live_approved"]["present"])

    def test_empty_ledger_zeros(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), empty_ledger=True)
            out = build_091170(root=root, try_kis=False)
        self.assertEqual(out["pnl"]["realized"], 0.0)
        self.assertEqual(out["pnl"]["realized_gross"], 0.0)
        self.assertEqual(out["pnl"]["round_trip_count"], 0)
        self.assertEqual(out["trades"], [])

    def test_fills_without_rts_do_not_invent_pnl(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), with_rts=False)
            out = build_091170(root=root, try_kis=False)
        self.assertEqual(out["pnl"]["realized"], 0.0)
        self.assertEqual(out["pnl"]["realized_gross"], 0.0)
        self.assertEqual(out["pnl"]["round_trip_count"], 0)
        self.assertGreaterEqual(len(out["trades"]), 2)

    def test_ledger_round_trips_used(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), with_rts=True)
            out = build_091170(root=root, try_kis=False)
        self.assertEqual(out["pnl"]["realized"], 22)
        self.assertEqual(out["pnl"]["realized_gross"], 30)
        self.assertEqual(out["pnl"]["round_trip_count"], 1)

    def test_cumulative_from_archive_or_omit(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), empty_ledger=True)
            out = build_091170(root=root, try_kis=False)
            self.assertIsNone(out["pnl"]["cumulative_3d"])
            # weekday archive
            d = date(2026, 9, 11)
            _write(
                root / "ledger_archive" / f"day_ledger-{d.strftime('%Y%m%d')}.json",
                {"date": d.isoformat(), "realized_gross": 10, "realized_net_est": 7},
            )
            out2 = build_091170(root=root, try_kis=False)
        # May or may not include 2026-09-11 depending on "today"; either omit or available
        cum = out2["pnl"]["cumulative_3d"]
        if cum is not None:
            self.assertIn("realized_net_est", cum)

    def test_cumulative_falls_back_to_today_day_ledger(self):
        """No ledger_archive: matching session_date on root day_ledger.json still counts."""
        as_of = last_n_trading_days(3)[-1]
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), empty_ledger=True)
            arch = root / "ledger_archive"
            if arch.is_dir():
                for p in arch.iterdir():
                    p.unlink()
                arch.rmdir()
            self.assertFalse(arch.exists())
            _write(
                root / "day_ledger.json",
                {
                    "session_date": as_of.isoformat(),
                    "fills": [
                        {"side": "BUY", "price": 10000, "qty": 5, "slot_id": 1, "odno": "1", "tmd": "093000"},
                        {"side": "SELL", "price": 10050, "qty": 5, "slot_id": 1, "odno": "2", "tmd": "100000"},
                    ],
                    "realized_gross": 250,
                    "realized_net_est": 200,
                },
            )
            out = build_091170(root=root, try_kis=False)
        cum = out["pnl"]["cumulative_3d"]
        self.assertIsNotNone(cum)
        self.assertEqual(cum["realized_net_est"], 200)
        self.assertEqual(cum["realized_gross"], 250)
        self.assertEqual(cum["available_days"], 1)
        today_rec = next(r for r in cum["per_day"] if r["date"] == as_of.isoformat())
        self.assertTrue(today_rec["available"])
        self.assertEqual(today_rec["source"], "day_ledger.json")
        self.assertEqual(today_rec["realized_net_est"], 200)
        for r in cum["per_day"]:
            if r["date"] != as_of.isoformat():
                self.assertFalse(r["available"])
                self.assertTrue(r["gap"])
                self.assertIsNone(r["realized_net_est"])

    def test_cumulative_from_today_fills_without_realized_fields(self):
        """session_date + pairable fills, no realized_* and no archive."""
        as_of = last_n_trading_days(3)[-1]
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), empty_ledger=True)
            arch = root / "ledger_archive"
            if arch.is_dir():
                for p in arch.iterdir():
                    p.unlink()
                arch.rmdir()
            _write(
                root / "day_ledger.json",
                {
                    "session_date": as_of.isoformat(),
                    "fills": [
                        {"side": "BUY", "price": 10000, "qty": 5, "slot_id": 1, "odno": "1", "tmd": "093000"},
                        {"side": "SELL", "price": 10050, "qty": 5, "slot_id": 1, "odno": "2", "tmd": "100000"},
                    ],
                },
            )
            out = build_091170(root=root, try_kis=False)
        cum = out["pnl"]["cumulative_3d"]
        self.assertIsNotNone(cum)
        self.assertEqual(cum["realized_gross"], 250.0)
        self.assertEqual(cum["available_days"], 1)
        rec = next(r for r in cum["per_day"] if r["date"] == as_of.isoformat())
        self.assertTrue(rec["available"])
        self.assertEqual(rec["source"], "day_ledger.json")

    def test_cumulative_does_not_copy_ledger_onto_other_days(self):
        """A dated ledger must not fill unrelated days in the 3-day window."""
        days = last_n_trading_days(3)
        as_of = days[-1]
        other = days[0]
        if other == as_of:
            self.skipTest("need at least two distinct trading days")
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), empty_ledger=True)
            _write(
                root / "day_ledger.json",
                {
                    "session_date": as_of.isoformat(),
                    "realized_gross": 99,
                    "realized_net_est": 80,
                },
            )
            out = build_091170(root=root, try_kis=False)
        cum = out["pnl"]["cumulative_3d"]
        self.assertIsNotNone(cum)
        by_date = {r["date"]: r for r in cum["per_day"]}
        self.assertTrue(by_date[as_of.isoformat()]["available"])
        self.assertFalse(by_date[other.isoformat()]["available"])
        self.assertIsNone(by_date[other.isoformat()]["realized_net_est"])

    def test_last_price_json_and_slot_fill_pnl(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), empty_ledger=True)
            _write(
                root / "day_ledger.json",
                {
                    "session_date": "2026-09-11",
                    "daily_buy_notional": 50000,
                    "safety_frozen": False,
                    "freeze_reasons": [],
                    "ma20": 9900,
                    "ratchet_steps": 1,
                    "fills": [
                        {"side": "BUY", "price": 10000, "qty": 5, "slot_id": 1, "odno": "1", "tmd": "093000"},
                        {"side": "SELL", "price": 10050, "qty": 5, "slot_id": 1, "odno": "2", "tmd": "100000"},
                    ],
                },
            )
            out = build_091170(root=root, try_kis=False)
        self.assertEqual(out["overview"]["last_price"], 10020)
        self.assertEqual(out["overview"]["price_source"], "last_price.json")
        self.assertEqual(out["overview"]["base"], 10100)
        self.assertEqual(out["overview"]["day_high"], 10200)
        self.assertEqual(out["overview"]["cash"], 250000)
        self.assertEqual(out["pnl"]["realized_gross"], 250.0)
        self.assertEqual(out["pnl"]["round_trip_count"], 1)
        self.assertEqual(out["pnl"]["capital_used_today"], 50000)
        self.assertTrue(any(o.get("slot") == 2 for o in out["open_orders"]))

    def test_ops_summary_build_text_pnl(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), empty_ledger=True)
            (root / "ops_summary.py").write_text(
                "def build_text(led=None):\n"
                "    return '종목 091170\\n  실현(총차익): +180원\\n  실현(순익): +120원\\n'\n",
                encoding="utf-8",
            )
            out = build_091170(root=root, try_kis=False)
        self.assertEqual(out["pnl"]["realized"], 120)
        self.assertEqual(out["pnl"]["realized_gross"], 180)
        self.assertEqual(out["pnl"]["pnl_source"], "ops_summary.build_text")
        self.assertIn("091170", out["pnl"].get("ops_summary_text") or "")
        cfg = out["overview"]["config_summary"]
        self.assertEqual(cfg.get("slot_offsets"), [-75, -180, -330, -525])
        self.assertEqual(cfg.get("daily_buy_cap"), 800000)

    def test_secrets_not_in_payload(self):
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), secrets=True)
            out = build_091170(root=root, try_kis=False)
        dumped = json.dumps(out)
        self.assertNotIn("9990011222", dumped)
        self.assertNotIn("SHOULD_NOT_LEAK", dumped)
        self.assertNotIn("also-secret", dumped)


class TestPortfolioAggregator(unittest.TestCase):
    def test_missing_091170_still_returns_367380(self):
        os.environ["DASHBOARD_SKIP_KIS"] = "1"
        os.environ["GRID_BOT_091170_ROOT"] = "/definitely-missing-grid-bot-091170"
        try:
            port = build_portfolio_status(try_kis=False)
        finally:
            os.environ.pop("GRID_BOT_091170_ROOT", None)
        self.assertIn("hero", port)
        self.assertEqual(len(port["bots"]), 2)
        by_id = {b["id"]: b for b in port["bots"]}
        self.assertTrue(by_id["367380"]["ok"])
        self.assertFalse(by_id["091170"]["ok"])
        self.assertIn("091170", port["errors"])
        self.assertIn("overview", by_id["367380"])
        self.assertIn("safety_frozen", by_id["367380"]["overview"])

    def test_both_bots_with_fixture(self):
        os.environ["DASHBOARD_SKIP_KIS"] = "1"
        with tempfile.TemporaryDirectory() as td:
            root = _fixture_091170(Path(td), with_rts=True)
            os.environ["GRID_BOT_091170_ROOT"] = str(root)
            try:
                port = build_portfolio_status(try_kis=False)
            finally:
                os.environ.pop("GRID_BOT_091170_ROOT", None)
        by_id = {b["id"]: b for b in port["bots"]}
        self.assertTrue(by_id["367380"]["ok"])
        self.assertTrue(by_id["091170"]["ok"])
        self.assertEqual(by_id["091170"]["pnl"]["realized"], 22)
        self.assertGreaterEqual(port["hero"]["open_orders"], 1)
        dumped = json.dumps(port)
        self.assertNotIn("app_key", dumped)

    def test_existing_single_bot_status_still_works(self):
        os.environ["DASHBOARD_SKIP_KIS"] = "1"
        st = build_status(try_kis=False)
        self.assertIn("overview", st)
        self.assertIn("pnl", st)
        self.assertIn("open_orders", st)
        self.assertEqual(st["overview"].get("symbol") or "367380", "367380")
        self.assertIn("safety_frozen", st["overview"])
        self.assertNotIn("/api/portfolio", json.dumps(st))


if __name__ == "__main__":
    unittest.main()
