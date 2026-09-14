"""Unit tests for portfolio 1m trade-snapshot charts (no live KIS)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MULTI = ROOT / "dashboard" / "multi"
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(MULTI))

from trade_charts import (  # noqa: E402
    attach_charts,
    build_snapshots,
    extract_raw_fills,
    fills_to_markers,
    get_or_build_charts,
    has_morning_cluster,
    ledger_covers_date,
    load_fills_for_bot,
    load_ledger_for_date,
    marker_x_on_bars,
    parse_fill_tmd,
    read_chart_index,
    safe_chart_name,
    seoul_chart_date,
)


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# 1x1 PNG — used as a stand-in renderer so CI never needs matplotlib or KIS.
_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

FIXTURE_BARS = [
    {"date": "20260914", "time": "090000", "open": 10000, "high": 10020, "low": 9990, "close": 10010, "volume": 10},
    {"date": "20260914", "time": "090100", "open": 10010, "high": 10030, "low": 10000, "close": 10025, "volume": 8},
    {"date": "20260914", "time": "090200", "open": 10025, "high": 10040, "low": 10010, "close": 10015, "volume": 12},
    {"date": "20260914", "time": "103000", "open": 10015, "high": 10050, "low": 10000, "close": 10040, "volume": 20},
    {"date": "20260914", "time": "120000", "open": 10040, "high": 10060, "low": 10020, "close": 10030, "volume": 15},
    {"date": "20260914", "time": "150000", "open": 10030, "high": 10035, "low": 10000, "close": 10005, "volume": 9},
]


def _fake_render(bars, markers, out_path, **kwargs):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(_MIN_PNG)
    return True


class TestSeoulChartDate(unittest.TestCase):
    def test_weekday_is_itself(self):
        monday = date(2026, 9, 14)
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(seoul_chart_date(monday), monday)

    def test_weekend_uses_friday(self):
        saturday = date(2026, 9, 12)
        sunday = date(2026, 9, 13)
        friday = date(2026, 9, 11)
        self.assertEqual(seoul_chart_date(saturday), friday)
        self.assertEqual(seoul_chart_date(sunday), friday)


class TestFillParsing(unittest.TestCase):
    def test_367380_today_fills_tmd(self):
        led = {
            "date": "2026-09-14",
            "meta": {
                "today_fills": [
                    {"odno": "1", "side": "BUY", "price": 23915, "qty": 1, "tmd": "083729"},
                    {"odno": "2", "side": "SELL", "price": 23960, "qty": 1, "tmd": "120440"},
                    {"odno": "2", "side": "SELL", "price": 23960, "qty": 1, "tmd": "120440"},
                ]
            },
        }
        raw = extract_raw_fills(led)
        marks = fills_to_markers(raw, date(2026, 9, 14))
        self.assertEqual(len(marks), 2)
        self.assertEqual(marks[0]["side"], "BUY")
        self.assertEqual(marks[0]["tmd"], "083729")
        self.assertEqual(marks[0]["price"], 23915)
        self.assertEqual(marks[1]["side"], "SELL")
        self.assertEqual(marks[1]["tmd"], "120440")

    def test_091170_iso_ts(self):
        led = {
            "session_date": "2026-09-14",
            "fills": [
                {"ts": "2026-09-14T09:06:12+09:00", "side": "BUY", "price": 14265, "qty": 5},
                {"ts": "2026-09-14T09:18:40+09:00", "side": "SELL", "price": 14315, "qty": 5},
            ],
        }
        marks = fills_to_markers(extract_raw_fills(led), date(2026, 9, 14))
        self.assertEqual([m["tmd"] for m in marks], ["090612", "091840"])
        self.assertEqual([m["side"] for m in marks], ["BUY", "SELL"])

    def test_drops_other_day_iso_fills(self):
        fills = [
            {"ts": "2026-09-11T09:06:12+09:00", "side": "BUY", "price": 10000, "qty": 1},
            {"ts": "2026-09-14T10:00:00+09:00", "side": "SELL", "price": 10050, "qty": 1},
        ]
        marks = fills_to_markers(fills, date(2026, 9, 14), ledger_date=date(2026, 9, 14))
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["side"], "SELL")

    def test_parse_tmd_variants(self):
        self.assertEqual(parse_fill_tmd({"tmd": "93000"}, fallback_date=date(2026, 9, 14)), "093000")
        self.assertEqual(parse_fill_tmd({"tmd": "9:30:00"}, fallback_date=date(2026, 9, 14)), "093000")
        self.assertEqual(
            parse_fill_tmd({"ts": "2026-09-14T15:19:05+09:00"}, fallback_date=date(2026, 9, 14)),
            "151905",
        )

    def test_ledger_covers_session_date(self):
        self.assertTrue(ledger_covers_date({"session_date": "2026-09-14"}, date(2026, 9, 14)))
        self.assertFalse(ledger_covers_date({"session_date": "2026-09-11"}, date(2026, 9, 14)))
        self.assertTrue(ledger_covers_date({"date": "20260914"}, date(2026, 9, 14)))

    def test_load_archive_not_live_other_day(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "day_ledger.json",
                {"session_date": "2026-09-11", "fills": [{"side": "BUY", "price": 1, "qty": 1, "tmd": "093000"}]},
            )
            _write(
                root / "ledger_archive" / "day_ledger-20260914.json",
                {
                    "date": "2026-09-14",
                    "meta": {"today_fills": [{"side": "SELL", "price": 2, "qty": 1, "tmd": "120000"}]},
                },
            )
            led = load_ledger_for_date(root, date(2026, 9, 14))
            self.assertIsNotNone(led)
            marks = load_fills_for_bot(root, date(2026, 9, 14))
            self.assertEqual(len(marks), 1)
            self.assertEqual(marks[0]["side"], "SELL")


class TestMarkerMapping(unittest.TestCase):
    def test_buy_sell_and_x(self):
        marks = fills_to_markers(
            [
                {"side": "BUY", "price": 10010, "qty": 1, "tmd": "090100"},
                {"side": "SELL", "price": 10040, "qty": 1, "tmd": "103000"},
            ],
            date(2026, 9, 14),
        )
        self.assertEqual(marker_x_on_bars("090100", FIXTURE_BARS), 1.0)
        self.assertEqual(marker_x_on_bars("103000", FIXTURE_BARS), 3.0)
        self.assertLess(marker_x_on_bars("083000", FIXTURE_BARS), 0)
        self.assertEqual([m["side"] for m in marks], ["BUY", "SELL"])

    def test_morning_cluster(self):
        clustered = [
            {"tmd": "090612", "side": "BUY", "price": 1, "qty": 1},
            {"tmd": "091200", "side": "BUY", "price": 1, "qty": 1},
            {"tmd": "120000", "side": "SELL", "price": 1, "qty": 1},
        ]
        self.assertTrue(has_morning_cluster(clustered))
        self.assertFalse(has_morning_cluster([{"tmd": "120000", "side": "SELL", "price": 1, "qty": 1}]))


class TestSafeChartName(unittest.TestCase):
    def test_whitelist(self):
        self.assertEqual(safe_chart_name("367380_trades_1m.png"), "367380_trades_1m.png")
        self.assertEqual(safe_chart_name("091170_trades_1m_am.png"), "091170_trades_1m_am.png")
        self.assertEqual(safe_chart_name("367380_trades_1m_20260914.png"), "367380_trades_1m_20260914.png")
        self.assertIsNone(safe_chart_name("../etc/passwd"))
        self.assertIsNone(safe_chart_name("index.json"))
        self.assertIsNone(safe_chart_name("367380_trades_1m.svg"))


class TestBuildSnapshots(unittest.TestCase):
    def test_fixture_bars_write_png_and_index(self):
        chart_day = date(2026, 9, 14)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root_367 = base / "g367"
            root_091 = base / "g091"
            cache = base / "charts"
            _write(
                root_367 / "day_ledger.json",
                {
                    "date": "2026-09-14",
                    "meta": {
                        "today_fills": [
                            {"side": "BUY", "price": 10010, "qty": 1, "tmd": "090100", "odno": "A"},
                            {"side": "SELL", "price": 10040, "qty": 1, "tmd": "103000", "odno": "B"},
                        ]
                    },
                },
            )
            _write(
                root_091 / "day_ledger.json",
                {
                    "session_date": "2026-09-14",
                    "fills": [
                        {"ts": "2026-09-14T09:06:12+09:00", "side": "BUY", "price": 10015, "qty": 5},
                        {"ts": "2026-09-14T09:12:00+09:00", "side": "SELL", "price": 10040, "qty": 5},
                        {"ts": "2026-09-14T12:00:00+09:00", "side": "BUY", "price": 10030, "qty": 5},
                    ],
                },
            )

            def fetch(_symbol, _d):
                return list(FIXTURE_BARS)

            idx = build_snapshots(
                chart_date=chart_day,
                charts_dir_path=cache,
                roots={"367380": root_367, "091170": root_091},
                fetch_bars=fetch,
                skip_kis=True,
                render_fn=_fake_render,
            )
            self.assertTrue(idx["ok"])
            self.assertEqual(idx["date"], "2026-09-14")
            s367 = idx["symbols"]["367380"]
            s091 = idx["symbols"]["091170"]
            self.assertTrue(s367["available"])
            self.assertEqual(s367["buy_count"], 1)
            self.assertEqual(s367["sell_count"], 1)
            self.assertEqual(s367["url"], "/charts/367380_trades_1m.png")
            self.assertTrue((cache / "367380_trades_1m.png").is_file())
            self.assertTrue(s091["available"])
            self.assertEqual(s091["buy_count"], 2)
            self.assertEqual(s091["sell_count"], 1)
            self.assertEqual(s091["zoom_url"], "/charts/091170_trades_1m_am.png")
            self.assertTrue((cache / "091170_trades_1m_am.png").is_file())

            disk = read_chart_index(cache)
            self.assertTrue(disk["symbols"]["367380"]["available"])

            payload = {"bots": [{"id": "367380"}, {"id": "091170"}]}
            attach_charts(payload, cache)
            self.assertEqual(payload["bots"][0]["chart"]["buy_count"], 1)
            self.assertTrue(payload["charts"]["ok"])

    def test_skip_kis_without_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "charts"
            root = Path(td) / "empty"
            root.mkdir()
            idx = build_snapshots(
                chart_date=date(2026, 9, 14),
                charts_dir_path=cache,
                roots={"367380": root, "091170": root},
                skip_kis=True,
                render_fn=_fake_render,
            )
            self.assertFalse(idx["ok"])
            self.assertEqual(idx["symbols"]["367380"]["empty_reason"], "no_cached_bars")

    def test_get_or_build_reuses_fresh_index(self):
        chart_day = date(2026, 9, 14)
        calls = {"n": 0}
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "charts"
            root = Path(td) / "bot"
            _write(
                root / "day_ledger.json",
                {"date": "2026-09-14", "fills": [{"side": "BUY", "price": 1, "qty": 1, "tmd": "090100"}]},
            )

            def fetch(_s, _d):
                calls["n"] += 1
                return list(FIXTURE_BARS)

            first = get_or_build_charts(
                refresh=True,
                skip_kis=True,
                charts_dir_path=cache,
                fetch_bars=fetch,
                render_fn=_fake_render,
                chart_date=chart_day,
                roots={"367380": root, "091170": root},
            )
            self.assertTrue(first["generated_at"])
            n_after_first = calls["n"]
            second = get_or_build_charts(
                refresh=False,
                skip_kis=True,
                charts_dir_path=cache,
                fetch_bars=fetch,
                render_fn=_fake_render,
                chart_date=chart_day,
                roots={"367380": root, "091170": root},
            )
            self.assertEqual(second["generated_at"], first["generated_at"])
            self.assertEqual(calls["n"], n_after_first)

    def test_portfolio_aggregator_unchanged_without_charts_dir(self):
        """Existing portfolio JSON still builds when charts are absent."""
        os.environ["DASHBOARD_SKIP_KIS"] = "1"
        os.environ["GRID_BOT_091170_ROOT"] = "/definitely-missing-grid-bot-091170"
        try:
            from build_portfolio_status import build_portfolio_status

            port = build_portfolio_status(try_kis=False)
        finally:
            os.environ.pop("GRID_BOT_091170_ROOT", None)
        self.assertIn("bots", port)
        self.assertNotIn("charts", port)


if __name__ == "__main__":
    unittest.main()
