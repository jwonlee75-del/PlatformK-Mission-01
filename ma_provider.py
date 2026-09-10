"""Daily SMA providers for safety_freeze (injectable / stub-friendly)."""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, Sequence


class MaProvider(Protocol):
    def sma(self, period: int = 20) -> Optional[float]:
        """Return SMA of daily closes, or None if unavailable."""


class StubMaProvider:
    """Fixed or callable SMA for tests / dry-run."""

    def __init__(self, value: Optional[float] = None, *, getter: Optional[Callable[[], Optional[float]]] = None) -> None:
        self._value = value
        self._getter = getter

    def set(self, value: Optional[float]) -> None:
        self._value = value

    def sma(self, period: int = 20) -> Optional[float]:
        if self._getter is not None:
            return self._getter()
        return self._value


def sma_from_closes(closes: Sequence[float], period: int = 20) -> Optional[float]:
    if period <= 0 or len(closes) < period:
        return None
    window = [float(x) for x in closes[-period:]]
    return sum(window) / float(period)


class SequenceMaProvider:
    """SMA from an in-memory daily close series (tests / offline)."""

    def __init__(self, closes: Sequence[float] | None = None) -> None:
        self.closes: list[float] = list(closes or [])

    def sma(self, period: int = 20) -> Optional[float]:
        return sma_from_closes(self.closes, period)


class KisDailySmaProvider:
    """Fetch daily closes via KIS inquire_daily_itemchartprice when available.

    Failures return None (freeze rule skipped for MA until data exists).
    """

    def __init__(
        self,
        symbol: str,
        *,
        env_dv: str = "real",
        fetcher: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.symbol = str(symbol)
        self.env_dv = env_dv
        self._fetcher = fetcher
        self._cached: Optional[float] = None

    def _load_fetcher(self) -> Callable[..., Any]:
        if self._fetcher is not None:
            return self._fetcher
        from pathlib import Path
        import sys

        ota = Path("/workspace/open-trading-api/examples_llm")
        if str(ota) not in sys.path:
            sys.path.insert(0, str(ota))
        from domestic_stock.inquire_daily_itemchartprice.inquire_daily_itemchartprice import (
            inquire_daily_itemchartprice,
        )

        self._fetcher = inquire_daily_itemchartprice
        return self._fetcher

    def sma(self, period: int = 20) -> Optional[float]:
        if self._cached is not None:
            return self._cached
        try:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            end = datetime.now(ZoneInfo("Asia/Seoul")).date()
            start = end - timedelta(days=max(40, period * 3))
            fn = self._load_fetcher()
            _df1, df2 = fn(
                self.env_dv,
                "J",
                self.symbol,
                start.strftime("%Y%m%d"),
                end.strftime("%Y%m%d"),
                "D",
                "0",
            )
            if df2 is None or getattr(df2, "empty", True):
                return None
            # KIS output2 typically has stck_clpr (close); tolerate column variants
            col = None
            for cand in ("stck_clpr", "close", "f_prdy_clpr", "prdy_clpr"):
                if cand in df2.columns:
                    col = cand
                    break
            if col is None:
                # last numeric-looking column fallback is unsafe — bail
                return None
            closes = [float(x) for x in df2[col].tolist() if x is not None and str(x) != ""]
            # API often returns newest-first; chronological for SMA
            if len(closes) >= 2 and closes[0] != closes[-1]:
                # If date column exists and is descending, reverse
                for dcol in ("stck_bsop_date", "date"):
                    if dcol in df2.columns:
                        dates = [str(x) for x in df2[dcol].tolist()]
                        if dates and dates[0] > dates[-1]:
                            closes = list(reversed(closes))
                        break
            val = sma_from_closes(closes, period)
            if val is not None:
                self._cached = val
            return val
        except Exception:
            return None
