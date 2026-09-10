# DRY RUN RESULT — paper demo

**Command:** `python3 /workspace/grid-bot/main.py --mode paper --scenario demo`  
**Exit code:** 0  
**When:** 2026-09-08 ~22:15 KST  
**Ref price:** 25,000 → **spacing:** 50 → initial buy lines `[24950, 24900, 24850, 24800, 24750]`

## What the demo proved

| Event | Evidence |
|-------|----------|
| Grid placement | 09:05 session start → 5 BUY orders (`MOCK-000001`…`000005`), states `pending→open` |
| Buy fill | 10:00 price=24950 → `MOCK-000001` BUY filled; TP SELL @25000 placed |
| TP sell | 11:00 price=25000 → sell filled; position removed; recycle BUY @24950 |
| Ratchet | 12:00 day_high=25100 > 24950+2×50=25050 → cancel open buys only, lines → `[25000,24950,24900,24850,24800]`, re-place (ratchet flag; does not count as new buy) |
| Session-end cancel | 15:21 → canceled **5** unfilled buys; sells (if any) kept |
| Order IDs / FSM | jsonl shows `broker.submit` → `broker.ack` → `broker.fill` / `cancel_req` → `cancel_ack` |
| Persistence | `positions.json` written throughout; demo ended flat (0 positions after full round-trip) |

## Counters (latest successful run)

- buy_fills: **1** (new buy; recycle/ratchet not counted toward daily cap)
- sell_fills: **1**
- ratchets: **1**
- session_end_buy_cancels: **5**
- cancel_acks (incl. ratchet): **10**

## Artifacts

- `/workspace/grid-bot/positions.json`
- `/workspace/grid-bot/logs/run-*.jsonl`

## Open TODOs (live wiring)

- Implement `LiveKISStub` order/cancel/query via KIS MCP (no secrets in logs)
- Read-only live price poll → engine `on_price`
- Explicit approval gate before any live submit
- Production scheduler for session start/end in Asia/Seoul
