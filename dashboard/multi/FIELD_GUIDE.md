# Portfolio field guide — cumulative win rate

Read-only. No secrets. Explicit `round_trips` only — no FIFO and no fill pairing.

## Definition

| Result | Rule |
|--------|------|
| win | `pnl_gross > 0` (else `pnl`, else `(sell − buy) × qty`) |
| loss | `< 0` |
| breakeven | `== 0` — counted, **excluded from the denominator** |
| `win_rate` | `wins / (wins + losses)` |

`win_rate` / `win_rate_pct` are `null` when `decided == 0`.

## Sources (per bot)

- Every `ledger_archive/day_ledger-YYYYMMDD.json`
- Plus today's live `day_ledger.json` when `date` / `session_date` is the Seoul calendar day
- One ledger per date; live today overrides a same-day archive
- An undated live file counts only as calendar-today

## Payload

`bots[].pnl.win_rate` and `hero.win_rate`. Hero **sums** wins/losses across ok bots, then recomputes the rate (`source: hero_sum`).

```json
{
  "wins": 12,
  "losses": 3,
  "breakeven": 0,
  "round_trips": 15,
  "decided": 15,
  "win_rate": 0.8,
  "win_rate_pct": 80.0,
  "days": 4,
  "today_included": true,
  "per_day": [{"date": "2026-09-09", "wins": 7, "losses": 0, "source": "day_ledger-20260909.json"}],
  "source": "ledger_archive+today"
}
```
