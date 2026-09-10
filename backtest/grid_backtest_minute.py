#!/usr/bin/env python3
"""1-minute bar grid-bot parameter search for ETF 367380 (last 10 sessions)."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MINUTE_PATH = Path("/workspace/grid-bot/backtest/minute_367380_10d.json")
DAILY_RESULTS = Path("/workspace/grid-bot/backtest/results.csv")
RESULTS_CSV = Path("/workspace/grid-bot/backtest/results_minute_10d.csv")
RESULTS_MD = Path("/workspace/grid-bot/backtest/RESULTS_MINUTE_10d.md")

TICK = 5
FEE_SIDE = 0.00015
TAX_PCT = 0.154
SESSION_START = "090500"
SESSION_END = "152000"


def round_to_tick(value: float, tick: int = TICK) -> int:
    if value <= 0:
        return tick
    n = int(math.floor(value / tick + 0.5))
    return max(n * tick, tick)


@dataclass
class BuyOrder:
    price: int
    ratchet: bool = False
    recycle: bool = False


@dataclass
class Position:
    buy_price: int
    qty: int = 1
    sell_price: int = 0


@dataclass
class Stats:
    realized_pnl: float = 0.0
    round_trips: int = 0
    buy_fills: int = 0
    max_inventory: int = 0
    final_inventory: int = 0
    capital_used_sum: float = 0.0
    capital_days: int = 0
    equity_curve: list[float] = field(default_factory=list)
    initial_cash: float = 0.0
    final_equity: float = 0.0
    final_cash: float = 0.0
    mtm_pnl: float = 0.0
    inventory_cost: float = 0.0


class GridBacktestMinute:
    def __init__(
        self,
        bars: list[dict],
        spacing_pct: float,
        levels: int,
        tp_pct: float,
    ) -> None:
        self.bars = bars
        self.spacing_pct = spacing_pct
        self.levels = levels
        self.tp_pct = tp_pct
        self.max_holdings = levels * 3
        self.max_new_buys_per_day = levels
        self.qty = 1

        self.cash = 0.0
        self.positions: list[Position] = []
        self.buys: list[BuyOrder] = []
        self.buy_lines: list[int] = []
        self.spacing = 0
        self.tp = 0
        self.day_high = 0
        self.new_buys_today = 0
        self.price = 0
        self.stats = Stats()

    def holdings(self) -> int:
        return sum(p.qty for p in self.positions)

    def inventory_cost(self) -> float:
        return float(sum(p.buy_price * p.qty for p in self.positions))

    def equity(self, mark: int) -> float:
        return self.cash + sum(p.qty * mark for p in self.positions)

    def _try_fill_buy(self, order: BuyOrder) -> bool:
        if self.holdings() >= self.max_holdings:
            return False
        if not order.ratchet and self.new_buys_today >= self.max_new_buys_per_day:
            return False
        cost = order.price * self.qty
        if self.cash < cost:
            return False

        self.cash -= cost
        if not order.ratchet:
            self.new_buys_today += 1
        pos = Position(
            buy_price=order.price,
            qty=self.qty,
            sell_price=order.price + self.tp,
        )
        self.positions.append(pos)
        self.stats.buy_fills += 1
        self.stats.max_inventory = max(self.stats.max_inventory, self.holdings())
        return True

    def _try_fill_sell(self, pos: Position) -> bool:
        sell = pos.sell_price
        buy = pos.buy_price
        qty = pos.qty
        gross = (sell - buy) * qty
        notional_fees = sell * qty * FEE_SIDE + buy * qty * FEE_SIDE
        tax = max(gross, 0.0) * TAX_PCT
        net = gross - notional_fees - tax
        self.cash += sell * qty - notional_fees - tax
        self.stats.realized_pnl += net
        self.stats.round_trips += 1
        self.positions.remove(pos)
        recycle = BuyOrder(price=buy, ratchet=False, recycle=True)
        self.buys.append(recycle)
        if self.price <= recycle.price:
            if self._try_fill_buy(recycle):
                self.buys.remove(recycle)
        return True

    def _process_fills_at(self, px: int) -> None:
        sold = True
        while sold:
            sold = False
            for pos in list(self.positions):
                if px >= pos.sell_price:
                    self._try_fill_sell(pos)
                    sold = True
                    break

        progressed = True
        while progressed:
            progressed = False
            candidates = sorted(
                [b for b in self.buys if px <= b.price],
                key=lambda b: (-b.price, 0 if b.ratchet else 1),
            )
            for order in candidates:
                if order not in self.buys:
                    continue
                if self._try_fill_buy(order):
                    self.buys.remove(order)
                    progressed = True
                    break
                if self.holdings() >= self.max_holdings:
                    break
                if not order.ratchet and self.new_buys_today >= self.max_new_buys_per_day:
                    continue

    def _maybe_ratchet(self) -> None:
        guard = 0
        while guard < 30:
            guard += 1
            if not self.buy_lines:
                break
            top = max(self.buy_lines)
            trigger = top + 2 * self.spacing
            if self.day_high <= trigger:
                break
            self.buys.clear()
            self.buy_lines = [px + self.spacing for px in self.buy_lines]
            if self.holdings() < self.max_holdings:
                for px in self.buy_lines:
                    if any(b.price == px for b in self.buys):
                        continue
                    self.buys.append(BuyOrder(price=px, ratchet=True, recycle=False))
            self._process_fills_at(self.price)

    def _at_price(self, px: int) -> None:
        self.price = px
        self.day_high = max(self.day_high, px)
        self._process_fills_at(px)
        self._maybe_ratchet()

    def _walk_to(self, target: int) -> None:
        if target == self.price:
            self._at_price(self.price)
            return
        step = 1 if target > self.price else -1
        while self.price != target:
            self.price += step
            self._at_price(self.price)

    def _day_start(self, open_px: int) -> None:
        self.spacing = round_to_tick(open_px * self.spacing_pct)
        self.tp = round_to_tick(open_px * self.tp_pct)
        self.buy_lines = [open_px - i * self.spacing for i in range(1, self.levels + 1)]
        self.new_buys_today = 0
        self.day_high = open_px
        self.price = open_px
        self.buys.clear()

        for p in self.positions:
            p.sell_price = p.buy_price + self.tp

        if self.holdings() < self.max_holdings and self.new_buys_today < self.max_new_buys_per_day:
            for px in self.buy_lines:
                if any(b.price == px for b in self.buys):
                    continue
                self.buys.append(BuyOrder(price=px, ratchet=False, recycle=False))

        self._at_price(open_px)

    def _day_end(self, close_px: int) -> None:
        self.buys.clear()
        eq = self.equity(close_px)
        self.stats.equity_curve.append(eq)
        self.stats.capital_used_sum += self.inventory_cost()
        self.stats.capital_days += 1
        self.stats.max_inventory = max(self.stats.max_inventory, self.holdings())

    def _process_bar(self, bar: dict, is_day_first: bool) -> None:
        o = int(bar["open"])
        h = int(bar["high"])
        l = int(bar["low"])
        c = int(bar["close"])
        if is_day_first:
            self._day_start(o)
            path = [o, l, h, c] if c >= o else [o, h, l, c]
            for px in path[1:]:
                self._walk_to(px)
        else:
            # continue from prior close into this bar's path
            if c >= o:
                path = [o, l, h, c]
            else:
                path = [o, h, l, c]
            for px in path:
                self._walk_to(px)

    def run(self) -> dict:
        by_day: dict[str, list[dict]] = defaultdict(list)
        for b in self.bars:
            t = b["time"]
            if SESSION_START <= t <= SESSION_END:
                by_day[b["date"]].append(b)

        dates = sorted(by_day.keys())
        assert dates, "no session bars"

        first_open = int(by_day[dates[0]][0]["open"])
        self.stats.initial_cash = self.max_holdings * first_open * 1.05
        self.cash = self.stats.initial_cash

        for d in dates:
            day_bars = sorted(by_day[d], key=lambda x: x["time"])
            for i, bar in enumerate(day_bars):
                self._process_bar(bar, is_day_first=(i == 0))
            self._day_end(int(day_bars[-1]["close"]))

        last_close = int(by_day[dates[-1]][-1]["close"])
        self.stats.final_cash = self.cash
        self.stats.final_equity = self.equity(last_close)
        self.stats.final_inventory = self.holdings()
        self.stats.inventory_cost = self.inventory_cost()
        self.stats.mtm_pnl = sum((last_close - p.buy_price) * p.qty for p in self.positions)

        initial = self.stats.initial_cash
        total_return_pct = (self.stats.final_equity - initial) / initial * 100.0
        avg_capital = (
            self.stats.capital_used_sum / self.stats.capital_days
            if self.stats.capital_days
            else 0.0
        )
        max_dd = max_drawdown_pct(self.stats.equity_curve)

        return {
            "spacing_pct": self.spacing_pct,
            "levels": self.levels,
            "tp_pct": self.tp_pct,
            "max_holdings": self.max_holdings,
            "max_new_buys_per_day": self.max_new_buys_per_day,
            "initial_cash": round(initial, 2),
            "final_equity": round(self.stats.final_equity, 2),
            "total_return_pct": round(total_return_pct, 4),
            "realized_pnl": round(self.stats.realized_pnl, 2),
            "mtm_pnl": round(self.stats.mtm_pnl, 2),
            "round_trips": self.stats.round_trips,
            "buy_fills": self.stats.buy_fills,
            "max_inventory": self.stats.max_inventory,
            "max_drawdown_pct": round(max_dd, 4),
            "final_inventory": self.stats.final_inventory,
            "avg_capital_used": round(avg_capital, 2),
            "risk_adjusted": round(total_return_pct / max(max_dd, 0.01), 4),
        }


def max_drawdown_pct(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def buy_hold_return(bars: list[dict]) -> float:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        if SESSION_START <= b["time"] <= SESSION_END:
            by_day[b["date"]].append(b)
    dates = sorted(by_day.keys())
    first = int(sorted(by_day[dates[0]], key=lambda x: x["time"])[0]["open"])
    last = int(sorted(by_day[dates[-1]], key=lambda x: x["time"])[-1]["close"])
    return (last - first) / first * 100.0


def load_daily_ranking() -> list[tuple[float, int, float]]:
    """Return list of (spacing, levels, tp) in daily-OHLC return rank order."""
    if not DAILY_RESULTS.exists():
        return []
    rows = []
    with DAILY_RESULTS.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(
                (float(row["spacing_pct"]), int(row["levels"]), float(row["tp_pct"]))
            )
    return rows


def main() -> int:
    bars = json.loads(MINUTE_PATH.read_text(encoding="utf-8"))
    bars = sorted(bars, key=lambda x: (x["date"], x["time"]))
    session_bars = [b for b in bars if SESSION_START <= b["time"] <= SESSION_END]
    n_bars = len(session_bars)
    dates = sorted({b["date"] for b in session_bars})
    date_from, date_to = dates[0], dates[-1]

    spacing_grid = [0.002, 0.004, 0.006]
    levels_grid = [5, 10]
    tp_grid = [0.002, 0.004, 0.006]

    results: list[dict] = []
    for sp in spacing_grid:
        for lv in levels_grid:
            for tp in tp_grid:
                bt = GridBacktestMinute(bars, spacing_pct=sp, levels=lv, tp_pct=tp)
                results.append(bt.run())

    results_sorted = sorted(results, key=lambda r: r["total_return_pct"], reverse=True)

    fieldnames = [
        "spacing_pct",
        "levels",
        "tp_pct",
        "max_holdings",
        "max_new_buys_per_day",
        "initial_cash",
        "final_equity",
        "total_return_pct",
        "realized_pnl",
        "mtm_pnl",
        "round_trips",
        "buy_fills",
        "max_inventory",
        "max_drawdown_pct",
        "final_inventory",
        "avg_capital_used",
        "risk_adjusted",
    ]
    with RESULTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results_sorted:
            w.writerow(r)

    assert len(results_sorted) == 18

    best_ret = results_sorted[0]
    best_realized = max(results, key=lambda r: r["realized_pnl"])
    best_risk = max(results, key=lambda r: r["risk_adjusted"])
    bh = buy_hold_return(bars)
    rec = best_ret

    def fmt_row(r: dict, rank: int) -> str:
        return (
            f"| {rank} | {r['spacing_pct']:.3f} | {r['levels']} | {r['tp_pct']:.3f} "
            f"| {r['total_return_pct']:.4f}% | {r['realized_pnl']:.0f} | {r['mtm_pnl']:.0f} "
            f"| {r['round_trips']} | {r['max_inventory']} | {r['max_drawdown_pct']:.4f}% "
            f"| {r['risk_adjusted']:.4f} |"
        )

    top5 = "\n".join(fmt_row(r, i + 1) for i, r in enumerate(results_sorted[:5]))

    # compare vs daily ranking
    daily_rank = load_daily_ranking()
    minute_key_order = [
        (r["spacing_pct"], r["levels"], r["tp_pct"]) for r in results_sorted
    ]
    compare_lines = []
    if daily_rank:
        compare_lines.append("| 파라미터 | 일봉 순위 | 분봉 순위 | 분봉 return% |")
        compare_lines.append("|----------|-----------|-----------|--------------|")
        daily_pos = {k: i + 1 for i, k in enumerate(daily_rank)}
        minute_pos = {k: i + 1 for i, k in enumerate(minute_key_order)}
        # show top5 minute + note daily top3
        shown = set()
        for k in minute_key_order[:5] + daily_rank[:3]:
            if k in shown:
                continue
            shown.add(k)
            r = next(
                x
                for x in results_sorted
                if (x["spacing_pct"], x["levels"], x["tp_pct"]) == k
            )
            compare_lines.append(
                f"| sp={k[0]}, lv={k[1]}, tp={k[2]} | {daily_pos.get(k, '-')} | "
                f"{minute_pos.get(k, '-')} | {r['total_return_pct']:.4f}% |"
            )
        # Spearman-ish: top overlap
        daily_top5 = set(daily_rank[:5])
        minute_top5 = set(minute_key_order[:5])
        overlap = len(daily_top5 & minute_top5)
        rank_note = (
            f"일봉 Top5 ∩ 분봉 Top5 교집합: **{overlap}/5**. "
            f"일봉 1위 {daily_rank[0]} vs 분봉 1위 {minute_key_order[0]}."
        )
    else:
        rank_note = "일봉 results.csv 없음 — 비교 생략."
        compare_lines = ["(비교 데이터 없음)"]

    compare_table = "\n".join(compare_lines)

    md = f"""# ETF 367380 그리드 봇 1분봉 10거래일 파라미터 서치 결과

## 데이터
- 종목: ACE 미국나스닥100 (`367380`)
- 기간: `{date_from}` ~ `{date_to}` ({len(dates)} 거래일)
- 분봉 수: **{n_bars}** (세션 09:05–15:20 KST 필터 후; 원본 저장 `{MINUTE_PATH.name}` 총 {len(bars)} bars)
- 소스: KIS `inquire_time_dailychartprice` (env_dv=real, 일별 분봉, 페이지네이션)
- Buy&Hold (첫 세션봉 시가→마지막 세션봉 종가, 1주): **{bh:.4f}%**

## 시뮬레이션 가정
- 일별 **첫 세션봉(≥09:05) 시가** 기준 spacing/tp 1회 산정 (tick=5, half-up, TP≠spacing 독립)
- 매수선: `open - i*spacing` (i=1..N), 수량 1
- `max_holdings = levels * 3`, `max_new_buys_per_day = levels`
- 분봉 경로: 종가≥시가이면 O→L→H→C, 아니면 O→H→L→C (1원 walk); 이후 분봉은 이전 종가에서 이어감
- 세션: 09:05–15:20 (그 외 봉은 백테스트 미사용)
- 체결/리사이클/래칫/수수료·세금: 일봉 백테스트와 동일
- 장 종료: 미체결 매수 취소, 포지션 오버나이트 유지, 일말 종가 MTM
- 초기현금: `max_holdings * first_session_open * 1.05`

## Top 5 (total_return_pct 기준)

| 순위 | spacing_pct | levels | tp_pct | total_return% | realized_pnl | mtm_pnl | RTs | max_inv | max_DD% | ret/DD |
|------|-------------|--------|--------|---------------|--------------|---------|-----|---------|---------|--------|
{top5}

## 최고 기록
- **수익률 1위**: spacing={best_ret['spacing_pct']}, levels={best_ret['levels']}, tp={best_ret['tp_pct']} → return **{best_ret['total_return_pct']:.4f}%**, realized={best_ret['realized_pnl']:.2f}, max_DD={best_ret['max_drawdown_pct']:.4f}%, RTs={best_ret['round_trips']}, final_inv={best_ret['final_inventory']}
- **실현손익 1위**: spacing={best_realized['spacing_pct']}, levels={best_realized['levels']}, tp={best_realized['tp_pct']} → realized **{best_realized['realized_pnl']:.2f}** (return {best_realized['total_return_pct']:.4f}%)
- **위험조정 1위**: spacing={best_risk['spacing_pct']}, levels={best_risk['levels']}, tp={best_risk['tp_pct']} → RA **{best_risk['risk_adjusted']:.4f}** (return {best_risk['total_return_pct']:.4f}%, DD {best_risk['max_drawdown_pct']:.4f}%)

## 권장 파라미터
- **권장**: `spacing_pct={rec['spacing_pct']}`, `levels={rec['levels']}`, `tp_pct={rec['tp_pct']}`
- 근거: 18조합 중 total_return_pct 최고 (10거래일 1분봉).
- Buy&Hold {bh:.4f}% 대비 그리드 {rec['total_return_pct']:.4f}%.

## 일봉 OHLC 서치와 비교
{rank_note}

{compare_table}

- 일봉 서치는 ~3개월, 본 분봉 서치는 **최근 10거래일**이라 기간·해상도가 다름. 순위 불일치는 정상일 수 있음.

## 주의사항 (Caveats)
1. 분봉 내 O-L-H-C(또는 O-H-L-C) 경로는 여전히 가정이며, 실제 틱 순서와 다를 수 있음 (일봉보다 과대계상은 줄어듦).
2. 슬리피지·호가잔량·부분체결·지연 미반영.
3. 수수료 왕복 0.03%, 세금 15.4%는 가정값.
4. 세션 컷 09:05–15:20; 동시호가·시간외는 제외. 일부 날은 15:20 봉이 없고 15:19 등으로 끝날 수 있음.
5. 10거래일은 표본이 짧아 파라미터 안정성이 낮음 — 라이브 적용 전 더 긴 구간·워크포워드 권장.
6. 라이브 엔진 TP=spacing 결합 가능; 본 서치는 tp_pct 독립.

## 파일
- 분봉: `minute_367380_10d.json` ({len(bars)} rows raw / {n_bars} session)
- CSV: `results_minute_10d.csv` (18 rows)
"""
    RESULTS_MD.write_text(md, encoding="utf-8")

    print("=" * 60)
    print(f"date_range={date_from}..{date_to} n_days={len(dates)} n_session_bars={n_bars} n_raw={len(bars)}")
    print(f"buy_hold_return_pct={bh:.4f}")
    print("-" * 60)
    print("TOP 3 by total_return_pct:")
    for i, r in enumerate(results_sorted[:3], 1):
        print(
            f"  {i}. sp={r['spacing_pct']} lv={r['levels']} tp={r['tp_pct']} "
            f"ret={r['total_return_pct']:.4f}% real={r['realized_pnl']:.2f} "
            f"mtm={r['mtm_pnl']:.2f} RTs={r['round_trips']} "
            f"maxInv={r['max_inventory']} DD={r['max_drawdown_pct']:.4f}% "
            f"RA={r['risk_adjusted']:.4f} finInv={r['final_inventory']}"
        )
    print("-" * 60)
    print(
        f"RECOMMENDED: spacing_pct={rec['spacing_pct']} levels={rec['levels']} tp_pct={rec['tp_pct']}"
    )
    print(f"wrote {RESULTS_CSV} and {RESULTS_MD}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
