#!/usr/bin/env python3
"""Daily-OHLC grid-bot parameter search for ETF 367380.

Assumptions note: reconstructed O→L→H→C / O→H→L→C paths overestimate
fill probability vs true tick/intraday data.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

OHLC_PATH = Path("/workspace/grid-bot/backtest/ohlc_367380_3m.json")
RESULTS_CSV = Path("/workspace/grid-bot/backtest/results.csv")
RESULTS_MD = Path("/workspace/grid-bot/backtest/RESULTS.md")

TICK = 5
FEE_SIDE = 0.00015  # 0.015% each side → 0.03% round-trip
TAX_PCT = 0.154


def round_to_tick(value: float, tick: int = TICK) -> int:
    """Half-up round to nearest tick multiple; at least one tick."""
    if value <= 0:
        return tick
    # positive half-up
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


class GridBacktest:
    def __init__(
        self,
        ohlc: list[dict],
        spacing_pct: float,
        levels: int,
        tp_pct: float,
    ) -> None:
        self.ohlc = ohlc
        self.spacing_pct = spacing_pct
        self.levels = levels
        self.tp_pct = tp_pct
        self.max_holdings = levels * 3  # noted: 15 for N=5, 30 for N=10
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

    def _can_count_buy(self, order: BuyOrder) -> bool:
        if order.ratchet:
            return True  # ratchet doesn't consume daily cap
        return self.new_buys_today < self.max_new_buys_per_day

    def _try_fill_buy(self, order: BuyOrder) -> bool:
        if self.holdings() >= self.max_holdings:
            return False
        if not order.ratchet and self.new_buys_today >= self.max_new_buys_per_day:
            return False
        # cash check (soft — should have enough initial cash)
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
        # recycle buy at original buy price (counts toward daily cap if filled)
        recycle = BuyOrder(price=buy, ratchet=False, recycle=True)
        self.buys.append(recycle)
        # immediate fill if price already <= recycle limit
        if self.price <= recycle.price:
            if self._try_fill_buy(recycle):
                self.buys.remove(recycle)
        return True

    def _process_fills_at(self, px: int) -> None:
        # Sells first (price >= sell_limit)
        sold = True
        while sold:
            sold = False
            for pos in list(self.positions):
                if px >= pos.sell_price:
                    self._try_fill_sell(pos)
                    sold = True
                    break

        # Buys (price <= buy_limit), highest first
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
                # if blocked by caps, leave order; stop trying lower same-loop once stuck
                # but other buys at different prices might still work if holdings free — if holdings full, stop
                if self.holdings() >= self.max_holdings:
                    break
                if not order.ratchet and self.new_buys_today >= self.max_new_buys_per_day:
                    # may still fill ratchet orders
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
            # cancel unfilled buys only
            self.buys.clear()
            # shift lines up by 1 spacing
            self.buy_lines = [px + self.spacing for px in self.buy_lines]
            # re-place as ratchet (fills do not count toward daily new-buy cap)
            if self.holdings() < self.max_holdings:
                for px in self.buy_lines:
                    # avoid duplicate prices
                    if any(b.price == px for b in self.buys):
                        continue
                    # skip if we already hold at this exact buy price with pending? allow multi
                    self.buys.append(BuyOrder(price=px, ratchet=True, recycle=False))
            # immediate fills if price already through new lines
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

        # restore sells for open positions at buy_price + today's tp
        for p in self.positions:
            p.sell_price = p.buy_price + self.tp

        # place buys on empty lines if under holdings / daily caps (placement)
        if self.holdings() < self.max_holdings and self.new_buys_today < self.max_new_buys_per_day:
            for px in self.buy_lines:
                if any(b.price == px for b in self.buys):
                    continue
                self.buys.append(BuyOrder(price=px, ratchet=False, recycle=False))

        # fills at open
        self._at_price(open_px)

    def _day_end(self, close_px: int) -> None:
        self.buys.clear()  # cancel unfilled buys
        eq = self.equity(close_px)
        self.stats.equity_curve.append(eq)
        self.stats.capital_used_sum += self.inventory_cost()
        self.stats.capital_days += 1
        self.stats.max_inventory = max(self.stats.max_inventory, self.holdings())

    def run(self) -> dict:
        first_open = int(self.ohlc[0]["open"])
        self.stats.initial_cash = self.max_holdings * first_open * 1.05
        self.cash = self.stats.initial_cash

        for bar in self.ohlc:
            o, h, l, c = int(bar["open"]), int(bar["high"]), int(bar["low"]), int(bar["close"])
            self._day_start(o)
            if c >= o:
                path = [o, l, h, c]
            else:
                path = [o, h, l, c]
            # already at open; walk remaining
            for px in path[1:]:
                self._walk_to(px)
            self._day_end(c)

        last_close = int(self.ohlc[-1]["close"])
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
            "risk_adjusted": round(
                total_return_pct / max(max_dd, 0.01), 4
            ),
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


def buy_hold_return(ohlc: list[dict]) -> float:
    """Buy max_holdings-comparable: simple 1-share from first open to last close."""
    first = int(ohlc[0]["open"])
    last = int(ohlc[-1]["close"])
    return (last - first) / first * 100.0


def main() -> int:
    ohlc = json.loads(OHLC_PATH.read_text(encoding="utf-8"))
    assert ohlc, "empty OHLC"
    ohlc = sorted(ohlc, key=lambda x: x["date"])

    spacing_grid = [0.002, 0.004, 0.006]
    levels_grid = [5, 10]
    tp_grid = [0.002, 0.004, 0.006]

    results: list[dict] = []
    for sp in spacing_grid:
        for lv in levels_grid:
            for tp in tp_grid:
                bt = GridBacktest(ohlc, spacing_pct=sp, levels=lv, tp_pct=tp)
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

    assert len(results_sorted) == 18, len(results_sorted)

    best_ret = results_sorted[0]
    best_realized = max(results, key=lambda r: r["realized_pnl"])
    best_risk = max(results, key=lambda r: r["risk_adjusted"])
    bh = buy_hold_return(ohlc)
    n_days = len(ohlc)
    date_from, date_to = ohlc[0]["date"], ohlc[-1]["date"]

    def fmt_row(r: dict, rank: int) -> str:
        return (
            f"| {rank} | {r['spacing_pct']:.3f} | {r['levels']} | {r['tp_pct']:.3f} "
            f"| {r['total_return_pct']:.4f}% | {r['realized_pnl']:.0f} | {r['mtm_pnl']:.0f} "
            f"| {r['round_trips']} | {r['max_inventory']} | {r['max_drawdown_pct']:.4f}% "
            f"| {r['risk_adjusted']:.4f} |"
        )

    top5 = "\n".join(fmt_row(r, i + 1) for i, r in enumerate(results_sorted[:5]))

    # Recommendation: prefer risk-adjusted if return is competitive, else best return
    # Clear winner = best total_return; mention if risk-adjusted differs
    rec = best_ret
    if best_risk["risk_adjusted"] > best_ret["risk_adjusted"] * 1.15 and best_risk[
        "total_return_pct"
    ] > 0:
        # only switch if risk-adj clearly better and still positive return
        pass
    # Primary recommendation: best total_return; note risk-adjusted alternative

    md = f"""# ETF 367380 그리드 봇 3개월 파라미터 그리드 서치 결과

## 데이터
- 종목: ACE 미국나스닥100 (`367380`)
- 기간: `{date_from}` ~ `{date_to}` ({n_days} 거래일)
- 소스: KIS `inquire_daily_itemchartprice` (env_dv=real, 일봉, 비가중조정 `fid_org_adj_prc=0`)
- Buy&Hold (시가→종가, 1주 기준): **{bh:.4f}%**

## 시뮬레이션 가정
- 일별 시가 기준 `spacing = round_to_tick(open * spacing_pct)`, `tp = round_to_tick(open * tp_pct)` (tick=5, 반올림 half-up, **TP는 spacing과 독립**)
- 매수선: `open - i*spacing` (i=1..N), 수량 1
- `max_holdings = levels * 3` (N=5→15, N=10→30), `max_new_buys_per_day = levels`
- 일중 경로: 종가≥시가이면 O→L→H→C, 아니면 O→H→L→C (1원 단위 walk)
- 체결: 매수는 가격≤지정가, 매도는 가격≥지정가 (touch)
- 매도 체결 후 원래 매수가에 리사이클 매수 (일일 신규매수 카운트 **포함**); 래칫 재배치 체결은 카운트 **제외**
- 수수료: 왕복 0.03% (편도 0.015%), 세금: 양(+)의 (매도-매수) 금액의 15.4%
- 래칫: `day_high > top_buy_line + 2*spacing` 시 미체결 매수만 취소 후 1 spacing 상향
- 장 종료: 미체결 매수 취소, 포지션 유지, 종가 MTM
- 초기현금: `max_holdings * first_open * 1.05`

## Top 5 (total_return_pct 기준)

| 순위 | spacing_pct | levels | tp_pct | total_return% | realized_pnl | mtm_pnl | RTs | max_inv | max_DD% | ret/DD |
|------|-------------|--------|--------|---------------|--------------|---------|-----|---------|---------|--------|
{top5}

## 최고 기록
- **수익률 1위**: spacing={best_ret['spacing_pct']}, levels={best_ret['levels']}, tp={best_ret['tp_pct']} → return **{best_ret['total_return_pct']:.4f}%**, realized={best_ret['realized_pnl']:.2f}, max_DD={best_ret['max_drawdown_pct']:.4f}%, RTs={best_ret['round_trips']}, final_inv={best_ret['final_inventory']}
- **실현손익 1위**: spacing={best_realized['spacing_pct']}, levels={best_realized['levels']}, tp={best_realized['tp_pct']} → realized **{best_realized['realized_pnl']:.2f}** (return {best_realized['total_return_pct']:.4f}%)
- **위험조정 1위** (return / max(DD,0.01)): spacing={best_risk['spacing_pct']}, levels={best_risk['levels']}, tp={best_risk['tp_pct']} → RA **{best_risk['risk_adjusted']:.4f}** (return {best_risk['total_return_pct']:.4f}%, DD {best_risk['max_drawdown_pct']:.4f}%)

## 권장 파라미터
- **권장**: `spacing_pct={rec['spacing_pct']}`, `levels={rec['levels']}`, `tp_pct={rec['tp_pct']}`
- 근거: 18조합 중 total_return_pct 최고. max_holdings={rec['max_holdings']}, max_new_buys_per_day={rec['max_new_buys_per_day']}
- 현재 `config.json`은 spacing=tp=0.002, levels=5 (TP=spacing 결합). 본 서치는 TP 독립. config 자동 변경은 하지 않음 — 적용 시 사용자 확인 필요.
- Buy&Hold {bh:.4f}% 대비 그리드 수익률 {rec['total_return_pct']:.4f}% (하락장에서 그리드가 매수 적립·부분 실현하는 구조).

## 주의사항 (Caveats)
1. **일봉 OHLC 경로 재구성은 실제 장중 체결보다 낙관적**입니다. O-L-H-C 가정이 고저점을 모두 터치한다고 보아 체결이 과대계상될 수 있습니다.
2. 슬리피지·호가 잔량·부분체결·주문 지연은 미반영.
3. 수수료/세금은 config 가정값(왕복 0.03%, 세금 15.4%)이며 계좌·상품별 실측과 다를 수 있습니다.
4. 하락 추세({bh:.2f}%)에서 재고(final_inventory)와 MTM 손실이 결과에 크게 영향을 줍니다.
5. `max_holdings = levels * 3` 으로 N=5/10 공정 비교; 라이브 5레벨 봇의 고정 15와 N=5일 때 동일.
6. 라이브 엔진은 현재 TP=spacing 결합이나, 본 백테스트는 tp_pct 독립 스윕입니다.

## 파일
- OHLC: `ohlc_367380_3m.json` ({n_days} rows)
- CSV: `results.csv` (18 rows, return 내림차순)
"""
    RESULTS_MD.write_text(md, encoding="utf-8")

    print("=" * 60)
    print(f"date_range={date_from}..{date_to} n_days={n_days}")
    print(f"buy_hold_return_pct={bh:.4f}")
    print(f"max_holdings_rule=levels*3  (noted)")
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
    print(
        f"best_realized: sp={best_realized['spacing_pct']} lv={best_realized['levels']} tp={best_realized['tp_pct']} pnl={best_realized['realized_pnl']:.2f}"
    )
    print(
        f"best_risk_adj: sp={best_risk['spacing_pct']} lv={best_risk['levels']} tp={best_risk['tp_pct']} ra={best_risk['risk_adjusted']:.4f}"
    )
    print(f"wrote {RESULTS_CSV} and {RESULTS_MD}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
