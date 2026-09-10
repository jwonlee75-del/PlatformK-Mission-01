#!/usr/bin/env python3
"""Fetch 1-min bars for ETF 367380 for last 10 trading days via open-trading-api."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/workspace/open-trading-api/examples_llm")
import kis_auth as ka  # noqa: E402

API_URL = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
TR_ID = "FHKST03010230"
ISCD = "367380"
OUT = Path("/workspace/grid-bot/backtest/minute_367380_10d.json")
OHLC = Path("/workspace/grid-bot/backtest/ohlc_367380_3m.json")


def fetch_page(date: str, hour: str) -> list[dict]:
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ISCD,
        "FID_INPUT_HOUR_1": hour,
        "FID_INPUT_DATE_1": date,
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": "N",
    }
    res = ka._url_fetch(API_URL, TR_ID, "", params)
    if not res.isOK():
        try:
            res.printError(url=API_URL)
        except Exception:
            pass
        body = getattr(res, "getErrorMessage", lambda: str(res))()
        raise RuntimeError(f"API fail date={date} hour={hour}: {body}")
    body = res.getBody()
    out2 = getattr(body, "output2", None) or []
    if isinstance(out2, dict):
        out2 = [out2]
    return list(out2)


def hour_minus_one(hhmmss: str) -> str:
    h = int(hhmmss[0:2])
    m = int(hhmmss[2:4])
    s = int(hhmmss[4:6]) if len(hhmmss) >= 6 else 0
    total = h * 3600 + m * 60 + s - 60
    if total < 0:
        return "000000"
    return f"{total // 3600:02d}{(total % 3600) // 60:02d}{total % 60:02d}"


def fetch_day(date: str) -> list[dict]:
    """Paginate backward from 153000 until before 090000."""
    seen: set[str] = set()
    rows: list[dict] = []
    hour = "153000"
    for _ in range(20):  # safety
        time.sleep(0.35)
        page = fetch_page(date, hour)
        if not page:
            break
        new_count = 0
        oldest = None
        for r in page:
            if r.get("stck_bsop_date") != date:
                continue
            t = r.get("stck_cntg_hour", "")
            if not t or t in seen:
                continue
            seen.add(t)
            new_count += 1
            rows.append(
                {
                    "date": date,
                    "time": t,
                    "open": int(float(r["stck_oprc"])),
                    "high": int(float(r["stck_hgpr"])),
                    "low": int(float(r["stck_lwpr"])),
                    "close": int(float(r["stck_prpr"])),
                    "volume": int(float(r.get("cntg_vol") or 0)),
                }
            )
            if oldest is None or t < oldest:
                oldest = t
        print(f"  {date} hour={hour} page={len(page)} new={new_count} oldest={oldest}", flush=True)
        if new_count == 0 or oldest is None:
            break
        if oldest <= "090100":
            break
        hour = hour_minus_one(oldest)
        if hour >= oldest:
            break
    rows.sort(key=lambda x: x["time"])
    return rows


def main() -> int:
    ohlc = json.loads(OHLC.read_text(encoding="utf-8"))
    dates = [x["date"] for x in sorted(ohlc, key=lambda x: x["date"])][-10:]
    print("dates:", dates, flush=True)

    ka.auth(svr="prod")  # real
    all_bars: list[dict] = []
    for d in dates:
        day_bars = fetch_day(d)
        print(f"day {d}: {len(day_bars)} bars", flush=True)
        if not day_bars:
            print(f"WARNING: no bars for {d}", flush=True)
        all_bars.extend(day_bars)

    all_bars.sort(key=lambda x: (x["date"], x["time"]))
    OUT.write_text(json.dumps(all_bars, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT} n={len(all_bars)}", flush=True)
    # summary
    by = {}
    for b in all_bars:
        by.setdefault(b["date"], 0)
        by[b["date"]] += 1
    for d in dates:
        print(f"  {d}: {by.get(d, 0)}", flush=True)
    return 0 if all_bars else 1


if __name__ == "__main__":
    raise SystemExit(main())
