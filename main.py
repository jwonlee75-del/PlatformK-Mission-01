#!/usr/bin/env python3
"""KRX ETF grid bot CLI — paper / live-dry / live (gated).

Usage:
  python3 main.py --mode paper --scenario demo
  python3 main.py --mode live-dry
  python3 main.py --mode live --bootstrap-grid --i-approve-live-orders
  python3 morning_after_approve.py --i-approve-live-orders
"""
from __future__ import annotations

import argparse
import os
import time
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from broker_mock import MockBroker, ShadowBroker
from broker_kis import LiveKISBroker, LiveOrderApprovalError, KISApiError
from grid_engine import GridEngine, calc_spacing, build_buy_lines
from models import Order, OrderStatus, Side
from persistence import append_jsonl, save_positions, load_positions
from session import SEOUL, in_session, parse_hhmm, ensure_aware


CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
LIVE_APPROVED_PATH = ROOT / "LIVE_APPROVED"
ORDERS_STATE_PATH = ROOT / "orders_state.json"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_run_log_path(prefix: str = "run") -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(SEOUL).strftime("%Y%m%d-%H%M%S")
    return LOG_DIR / f"{prefix}-{stamp}.jsonl"


def today_seoul() -> str:
    return datetime.now(SEOUL).strftime("%Y-%m-%d")


def live_approval_ok(args: argparse.Namespace, cfg: dict[str, Any]) -> tuple[bool, str]:
    """Require BOTH --mode live (caller) AND flag or LIVE_APPROVED file."""
    safety = cfg.get("safety", {})
    if not safety.get("live_orders_require_explicit_approval", True):
        # Still require one of the gates for defense in depth
        pass
    if getattr(args, "i_approve_live_orders", False):
        return True, "cli --i-approve-live-orders"
    path = Path(safety.get("live_approved_file", str(LIVE_APPROVED_PATH)))
    if path.exists():
        content = path.read_text(encoding="utf-8").strip().splitlines()
        first = content[0].strip() if content else ""
        today = today_seoul()
        if first == today:
            return True, f"file {path} date={today}"
        return False, (
            f"LIVE_APPROVED present but date mismatch: got {first!r}, need Seoul {today}"
        )
    return False, (
        "missing approval: pass --i-approve-live-orders OR write today's Seoul "
        f"YYYY-MM-DD ({today_seoul()}) into {LIVE_APPROVED_PATH}"
    )


def build_demo_path(ref: int, spacing: int) -> list[tuple[str, int]]:
    """Synthetic intraday path proving grid, buy fill, TP sell, ratchet, session-end cancel."""
    buy1 = ref - spacing
    ratchet_px = ref + 2 * spacing
    return [
        ("09:05:00", ref),
        ("09:10:00", ref),
        ("09:30:00", buy1 + spacing // 2),
        ("10:00:00", buy1),
        ("10:05:00", buy1),
        ("10:30:00", ref - spacing // 2),
        ("11:00:00", ref),
        ("11:05:00", ref),
        ("11:30:00", ref + spacing),
        ("12:00:00", ratchet_px),
        ("12:30:00", ratchet_px),
        ("13:00:00", ref + spacing),
        ("14:00:00", ref + spacing // 2),
        ("15:00:00", ref + spacing // 2),
        ("15:20:00", ref + spacing // 2),
        ("15:21:00", ref + spacing // 2),
    ]


def run_paper_demo(cfg: dict[str, Any]) -> dict[str, Any]:
    run_log = make_run_log_path()
    records: list[dict[str, Any]] = []

    def log_fn(kind: str, message: str, data: dict) -> None:
        rec = {
            "ts": datetime.now(SEOUL).isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            "data": data,
        }
        records.append(rec)
        append_jsonl(str(run_log), rec)

    ref = 25000
    spacing = calc_spacing(ref, cfg["grid"]["spacing_pct"], cfg["grid"]["tick_size"])
    lines = build_buy_lines(ref, spacing, cfg["grid"]["levels"])

    print("=" * 72)
    print(f"PAPER DEMO  symbol={cfg['symbol']} ({cfg.get('symbol_name', '')})")
    print(f"ref={ref}  spacing={spacing}  buy_lines={lines}")
    print(f"run_log={run_log}")
    print("=" * 72)

    broker = MockBroker(symbol=cfg["symbol"], last_price=ref, log=log_fn)
    engine = GridEngine(cfg, broker, log=log_fn)

    save_positions([], cfg["persistence"]["file"], meta={"note": "demo reset"})

    base_day = datetime(2026, 9, 8, tzinfo=SEOUL)
    ticks = build_demo_path(ref, spacing)

    for hhmmss, px in ticks:
        h, m, s = map(int, hhmmss.split(":"))
        now = base_day.replace(hour=h, minute=m, second=s)
        print(f"\n--- tick {hhmmss} KST  price={px} ---")
        log_fn("sim.tick", f"price={px}", {"price": px, "time": hhmmss})
        engine.on_price(px, now)

    def count(kind: str) -> int:
        return sum(1 for r in records if r["kind"] == kind)

    summary = {
        "buy_fills": count("engine.buy_fill"),
        "sell_fills": count("engine.sell_fill"),
        "ratchets": count("engine.ratchet"),
        "cancels": count("broker.cancel_ack"),
        "session_end_cancels": 0,
        "grid_placed": count("engine.grid_placed") > 0,
        "order_ids_seen": [],
    }
    for r in records:
        if r["kind"] == "session.end":
            summary["session_end_cancels"] = len(r["data"].get("canceled_ids", []))
        oid = r.get("data", {}).get("order_id")
        if oid:
            summary["order_ids_seen"].append(oid)
        ids = r.get("data", {}).get("ids")
        if ids:
            summary["order_ids_seen"].extend(ids)

    open_orders = broker.list_open_orders(cfg["symbol"])
    print("\n" + "=" * 72)
    print("DEMO COMPLETE")
    print(
        f"  buy_fills={summary['buy_fills']}  sell_fills={summary['sell_fills']}  "
        f"ratchets={summary['ratchets']}  cancel_acks={summary['cancels']}  "
        f"session_end_buy_cancels={summary['session_end_cancels']}"
    )
    print(f"  grid_placed={summary['grid_placed']}")
    print(f"  positions={len(engine.positions)}  open_orders={len(open_orders)}")
    for o in open_orders:
        print(
            f"    OPEN {o.side.value} {o.qty}@{o.price} "
            f"id={o.order_id} status={o.status.value}"
        )
    for p in engine.positions:
        print(
            f"    POS  buy={p.buy_price} qty={p.qty} date={p.date} "
            f"sell_oid={p.sell_order_id}"
        )
    print(f"  positions_file={cfg['persistence']['file']}")
    print(f"  run_log={run_log}")
    print("=" * 72)

    summary["open_orders"] = [o.to_dict() for o in open_orders]
    summary["positions"] = [p.to_dict() for p in engine.positions]
    summary["run_log"] = str(run_log)
    summary["ref"] = ref
    summary["spacing"] = spacing
    summary["buy_lines_initial"] = lines
    return summary


def _make_live_broker(
    cfg: dict[str, Any], *, allow_mutations: bool, log_fn=None
) -> LiveKISBroker:
    safety = cfg.get("safety", {})
    broker = LiveKISBroker(
        symbol=cfg["symbol"],
        allow_mutations=allow_mutations,
        api_retry=int(safety.get("api_retry", 3)),
        env_dv=str(safety.get("kis_env_dv", "real")),
        log=log_fn,
        intent_log_dir=LOG_DIR,
    )
    broker.connect()
    return broker


def run_live_dry(cfg: dict[str, Any]) -> int:
    """Read-only: auth + inquire_price + inquire_balance + print grid lines. No orders."""
    run_log = make_run_log_path("live-dry")

    def log_fn(kind: str, message: str, data: dict) -> None:
        append_jsonl(
            str(run_log),
            {
                "ts": datetime.now(SEOUL).isoformat(timespec="seconds"),
                "kind": kind,
                "message": message,
                "data": data,
            },
        )

    print("=" * 72)
    print("LIVE-DRY  (read-only KIS APIs — NO orders)")
    print(f"symbol={cfg['symbol']} ({cfg.get('symbol_name', '')})")
    print(f"run_log={run_log}")
    print("=" * 72)

    broker = _make_live_broker(cfg, allow_mutations=False, log_fn=log_fn)
    price = broker.get_last_price()
    bal = broker.inquire_balance_summary()
    cash = bal.get("dnca_tot_amt")
    spacing = calc_spacing(price, cfg["grid"]["spacing_pct"], cfg["grid"]["tick_size"])
    lines = build_buy_lines(price, spacing, cfg["grid"]["levels"])

    print(f"price (stck_prpr) = {price}")
    print(f"cash  (dnca_tot_amt) = {cash}")
    print(f"spacing = {spacing}  (pct={cfg['grid']['spacing_pct']} tick={cfg['grid']['tick_size']})")
    print(f"buy_lines ({len(lines)}) = {lines}")
    if bal.get("holdings_symbol"):
        print(f"holdings[{cfg['symbol']}] = {bal['holdings_symbol']}")
    else:
        print(f"holdings[{cfg['symbol']}] = (none)")

    # Prove mutation gate without sending anything
    probe = Order(
        order_id="",
        symbol=cfg["symbol"],
        side=Side.BUY,
        price=lines[0] if lines else price,
        qty=cfg["grid"]["qty_per_order"],
        client_tag="live-dry-probe",
    )
    try:
        broker.submit(probe)
        print("ERROR: submit should have been refused in live-dry", file=sys.stderr)
        return 1
    except LiveOrderApprovalError as e:
        print(f"mutation_gate: OK refused submit ({e.__class__.__name__})")

    print("=" * 72)
    print("LIVE-DRY OK — no orders placed")
    print("=" * 72)
    log_fn(
        "live_dry.summary",
        "ok",
        {"price": price, "cash": cash, "spacing": spacing, "buy_lines": lines},
    )
    return 0


def save_orders_state(
    broker: LiveKISBroker,
    placed: list[Order],
    *,
    ref: int,
    spacing: int,
    lines: list[int],
) -> None:
    payload = {
        "updated_at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        "ref_price": ref,
        "spacing": spacing,
        "buy_lines": lines,
        "orders": [o.to_dict() for o in placed],
        "kis_meta": broker.export_kis_meta(),
    }
    ORDERS_STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def run_live_bootstrap(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """Place initial 5 buy limits once (requires approval), save state, exit."""
    ok, reason = live_approval_ok(args, cfg)
    if not ok:
        print("LIVE BOOTSTRAP REFUSED — no orders will be sent.", file=sys.stderr)
        print(f"  reason: {reason}", file=sys.stderr)
        print(
            "  To approve today (Seoul): "
            f"echo {today_seoul()} > {LIVE_APPROVED_PATH}",
            file=sys.stderr,
        )
        print(
            "  Or: python3 main.py --mode live --bootstrap-grid --i-approve-live-orders",
            file=sys.stderr,
        )
        return 2

    run_log = make_run_log_path("live-bootstrap")

    def log_fn(kind: str, message: str, data: dict) -> None:
        append_jsonl(
            str(run_log),
            {
                "ts": datetime.now(SEOUL).isoformat(timespec="seconds"),
                "kind": kind,
                "message": message,
                "data": data,
            },
        )
        print(f"[{kind}] {message}")

    print("=" * 72)
    print("LIVE BOOTSTRAP GRID  (REAL ORDERS)")
    print(f"approval={reason}")
    print("=" * 72)

    try:
        broker = _make_live_broker(cfg, allow_mutations=True, log_fn=log_fn)
        engine = GridEngine(cfg, broker, log=log_fn)
        price = broker.get_last_price()
        engine.recalc_grid(price)
        assert engine.grid is not None
        placed = engine.place_initial_grid()
    except Exception as e:  # noqa: BLE001
        msg = f"❌ 그리드 실주문 실패\n\n{type(e).__name__}: {e}"
        print(msg, file=sys.stderr)
        telegram_alert(msg, cfg=cfg)
        return 1
    save_orders_state(
        broker,
        placed,
        ref=engine.grid.ref_price,
        spacing=engine.grid.spacing,
        lines=list(engine.grid.buy_lines),
    )
    # Do NOT wipe overnight holdings. Prefer morning_after_approve.py for
    # full restore (TP + ledger + missing buys). Bootstrap only records buy meta
    # when positions file is empty.
    pos_path = Path(cfg["persistence"]["file"])
    existing_pos = []
    if pos_path.exists():
        try:
            existing_pos = (json.loads(pos_path.read_text(encoding="utf-8")).get("positions") or [])
        except Exception:  # noqa: BLE001
            existing_pos = []
    if existing_pos:
        print(
            f"bootstrap: keeping {len(existing_pos)} existing positions "
            f"(use morning_after_approve.py to restore TPs)"
        )
    else:
        save_positions(
            engine.positions,
            cfg["persistence"]["file"],
            meta={
                "note": "live bootstrap — buys resting, no fills yet",
                "ref_price": engine.grid.ref_price,
                "spacing": engine.grid.spacing,
                "buy_lines": list(engine.grid.buy_lines),
                "order_ids": [o.order_id for o in placed],
            },
        )
    print(f"placed={len(placed)} orders_state={ORDERS_STATE_PATH}")
    lines_out: list[str] = []
    for o in placed:
        meta = broker.export_kis_meta().get(o.order_id, {})
        line = (
            f"  {o.order_id} BUY {o.qty}@{o.price} "
            f"odno={meta.get('odno')} orgno={meta.get('ord_orgno')}"
        )
        print(line)
        lines_out.append(
            f"• {o.qty}주 @{o.price:,} (odno={meta.get('odno') or o.order_id})"
        )
    print("Bootstrap done — exiting (no session loop).")
    # Prefer morning_after_approve.py for overnight TP restore + ledger sync.
    levels_n = int(cfg.get("grid", {}).get("levels", 5))
    try:
        if len(placed) >= levels_n:
            telegram_alert(
                "✅ 그리드 실주문 정상 접수\n\n"
                f"종목: {cfg.get('symbol')} ({cfg.get('symbol_name', '')})\n"
                f"기준가: {engine.grid.ref_price:,}\n"
                f"간격: {engine.grid.spacing}\n"
                f"접수 {len(placed)}건:\n" + "\n".join(lines_out),
                cfg=cfg,
            )
        else:
            telegram_alert(
                "⚠️ 그리드 실주문 부분 접수\n\n"
                f"기대 {levels_n}건 중 {len(placed)}건만 접수.\n"
                + ("\n".join(lines_out) if lines_out else "(주문 없음)"),
                cfg=cfg,
            )

        try:
            import subprocess
            subprocess.run(
                ["python3", str(ROOT / "dashboard" / "telegram_summary.py"), "--kis"],
                check=False,
                timeout=120,
            )
        except Exception as _e:  # noqa: BLE001
            print(f"[telegram_summary] skip: {type(_e).__name__}")

        if len(placed) < levels_n:
            return 1 if not placed else 0
    except Exception as e:  # noqa: BLE001
        telegram_alert(f"❌ 그리드 알림 전송 실패: {type(e).__name__}", cfg=cfg)
    return 0


def _load_telegram_creds(cfg: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return (token, chat_id). Never log token. Prefer env, then box-secrets + config."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or ""
    chat = os.environ.get("TELEGRAM_CHAT_ID") or ""
    if not token:
        try:
            secrets = json.loads(Path("/home/box/agent-data/box-secrets.json").read_text(encoding="utf-8")).get("secrets") or {}
            token = str(secrets.get("TELEGRAM_BOT_TOKEN") or "")
        except Exception:  # noqa: BLE001
            token = ""
    if not chat:
        tg = (cfg or {}).get("telegram") or {}
        chat = str(tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "")
        if not chat:
            try:
                secrets = json.loads(Path("/home/box/agent-data/box-secrets.json").read_text(encoding="utf-8")).get("secrets") or {}
                chat = str(secrets.get("TELEGRAM_CHAT_ID") or "")
            except Exception:
                chat = ""
    return token, chat


def telegram_alert(message: str, *, cfg: dict[str, Any] | None = None) -> None:
    """Send Telegram alert. Logs always; never prints bot token.

    Loads token from env or /home/box/agent-data/box-secrets.json;
    chat_id from TELEGRAM_CHAT_ID / box-secrets / config.telegram.chat_id (no hardcoded default).
    """
    print(f"[telegram_alert] {message[:200]}")
    token, chat = _load_telegram_creds(cfg)
    if not token or not chat:
        print("[telegram_alert] TELEGRAM not configured — logged only (token not printed)")
        return
    try:
        import urllib.parse
        import urllib.request

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": message[:3500]}
        ).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            _ = resp.read(64)
        print("[telegram_alert] sent (credentials not printed)")
    except Exception as e:  # noqa: BLE001
        print(f"[telegram_alert] send failed: {type(e).__name__}")


def _session_orders_state_path() -> Path:
    return ORDERS_STATE_PATH


def save_session_orders_state(
    *,
    broker: Any,
    engine: GridEngine,
    mode: str,
    extra: dict[str, Any] | None = None,
) -> None:
    open_orders = broker.list_open_orders(engine.symbol)
    payload: dict[str, Any] = {
        "updated_at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        "mode": mode,
        "ref_price": engine.grid.ref_price if engine.grid else None,
        "spacing": engine.grid.spacing if engine.grid else None,
        "buy_lines": list(engine.grid.buy_lines) if engine.grid else None,
        "day_high": engine.grid.day_high if engine.grid else None,
        "new_buys_filled_today": (
            engine.grid.new_buys_filled_today if engine.grid else 0
        ),
        "orders": [o.to_dict() for o in open_orders],
        "all_orders": [
            o.to_dict()
            for o in getattr(broker, "_orders", {}).values()
        ],
    }
    if hasattr(broker, "export_kis_meta"):
        payload["kis_meta"] = broker.export_kis_meta()
    if extra:
        payload.update(extra)
    ORDERS_STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _virtual_session_now(tick_idx: int, cfg: dict[str, Any]) -> datetime:
    """Fabricate Asia/Seoul wall times inside the session for after-hours dry tests."""
    start = parse_hhmm(cfg["session"]["start"])
    base = datetime.now(SEOUL).replace(
        hour=start.hour, minute=start.minute, second=0, microsecond=0
    )
    # Advance ~1 minute per tick so ratchet/session-end can still be exercised
    # over a long max_ticks run; short runs stay near open.
    return base + timedelta(minutes=tick_idx)


def run_live_session(
    cfg: dict[str, Any],
    *,
    dry: bool = True,
    approve: bool = False,
    interval_sec: float = 5.0,
    max_ticks: int | None = None,
    wait_for_open: bool = False,
) -> int:
    """Intraday session loop.

    dry=True (default / --dry-loop):
      LiveKISBroker for price (+ balance once) only; ShadowBroker drives GridEngine.
      No order_cash / order_rvsecncl. Intents logged as shadow_*.

    dry=False and approve=True:
      LiveKISBroker with mutations for real submits/cancels + fill polling.
      Do NOT enable in verification.
    """
    sess = cfg["session"]
    tz_name = cfg.get("timezone", "Asia/Seoul")
    run_log = make_run_log_path("live-session-dry" if dry else "live-session")
    prices_seen: list[int] = []
    shadow_intents: list[dict[str, Any]] = []

    def log_fn(kind: str, message: str, data: dict) -> None:
        rec = {
            "ts": datetime.now(SEOUL).isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            "data": data,
        }
        append_jsonl(str(run_log), rec)
        if kind.startswith("shadow.") or data.get("action", "").startswith("shadow_"):
            shadow_intents.append(rec)

    print("=" * 72)
    if dry:
        print("LIVE SESSION DRY-LOOP  (live prices + ShadowBroker — NO real orders)")
    else:
        print("LIVE SESSION  (REAL ORDERS — approval gated)")
    print(f"symbol={cfg['symbol']}  interval={interval_sec}s  max_ticks={max_ticks}")
    print(f"session={sess['start']}–{sess['end']} Asia/Seoul  run_log={run_log}")
    print("=" * 72)

    if not dry:
        if not approve:
            print("LIVE SESSION REFUSED — need approval for non-dry path.", file=sys.stderr)
            return 2
        live = _make_live_broker(cfg, allow_mutations=True, log_fn=log_fn)
        engine_broker: Any = live
        price_feed = live
    else:
        # Price feed: read-only live broker. Engine: shadow mock.
        live = _make_live_broker(cfg, allow_mutations=False, log_fn=log_fn)
        intent_path = str(LOG_DIR / f"live-intent-{datetime.now(SEOUL).strftime('%Y%m%d')}.jsonl")
        engine_broker = ShadowBroker(
            symbol=cfg["symbol"],
            last_price=0,
            log=log_fn,
            intent_log_path=intent_path,
        )
        price_feed = live

    # Balance once (read-only)
    try:
        bal = price_feed.inquire_balance_summary()
        cash = bal.get("dnca_tot_amt")
        print(f"balance cash(dnca_tot_amt)={cash}")
        log_fn("session.balance", "ok", {"cash": cash, "holdings": bal.get("holdings_symbol")})
    except Exception as e:  # noqa: BLE001
        telegram_alert(f"grid-bot balance fail: {type(e).__name__}: {e}", cfg=cfg)
        log_fn("session.balance_fail", str(e), {"error": type(e).__name__})
        print(f"WARNING: balance inquiry failed: {type(e).__name__}: {e}")

    def _alert(msg: str) -> None:
        telegram_alert(msg, cfg=cfg)

    sma20_fn = None
    try:
        from ma_provider import KisDailySmaProvider, StubMaProvider

        if cfg.get("safety_freeze", {}).get("enabled"):
            # Prefer KIS daily chart when connected; fall back to injectable stub.
            kis_sma = KisDailySmaProvider(
                cfg["symbol"],
                env_dv=str(cfg.get("safety", {}).get("kis_env_dv", "real")),
            )
            stub = StubMaProvider(getter=lambda: kis_sma.sma(20))

            def sma20_fn() -> float | None:
                return stub.sma(20)
    except Exception as e:  # noqa: BLE001
        print(f"[sma20] provider init skipped: {type(e).__name__}")
        sma20_fn = None

    engine = GridEngine(
        cfg,
        engine_broker,
        log=log_fn,
        alert=_alert,
        sma20_provider=sma20_fn,
    )

    # Wire live fill polling into engine when not dry
    if not dry:

        def _on_fill(order: Order) -> None:
            engine._handle_fill(order, datetime.now(SEOUL))

        live.on_fill = _on_fill

        # Restore known open orders from orders_state if present (post-bootstrap)
        if ORDERS_STATE_PATH.exists():
            try:
                st = json.loads(ORDERS_STATE_PATH.read_text(encoding="utf-8"))
                kis_meta = st.get("kis_meta") or {}
                for od in st.get("orders") or []:
                    o = Order(
                        order_id=od["order_id"],
                        symbol=od.get("symbol", cfg["symbol"]),
                        side=Side(od["side"]),
                        price=int(od["price"]),
                        qty=int(od["qty"]),
                        client_tag=od.get("client_tag", ""),
                        is_ratchet_reorder=bool(od.get("is_ratchet_reorder", False)),
                        is_recycle_rebuy=bool(od.get("is_recycle_rebuy", False)),
                        linked_buy_price=od.get("linked_buy_price"),
                    )
                    from state_machine import transition as _tr

                    o.status = OrderStatus.PENDING
                    live._orders[o.order_id] = o
                    _tr(o, "submit_ack")
                    meta = kis_meta.get(o.order_id) or {}
                    if meta:
                        live._kis_meta[o.order_id] = dict(meta)
                # Avoid LIVE-00000N collisions with restored ids on later submits
                max_n = 0
                for oid in live._orders:
                    if isinstance(oid, str) and oid.startswith("LIVE-"):
                        try:
                            max_n = max(max_n, int(oid.split("-", 1)[1]))
                        except ValueError:
                            pass
                if max_n > 0:
                    live._id_seq = __import__("itertools").count(max_n + 1)
                log_fn(
                    "session.restore_orders",
                    f"restored {len(live._orders)} from orders_state",
                    {"ids": list(live._orders), "next_live_seq": max_n + 1},
                )
                # Mid-session resume: hydrate GridEngine WITHOUT recalc+place_initial_grid
                # (would duplicate buys against existing open recycles / residual grid).
                buy_lines = [int(x) for x in (st.get("buy_lines") or [])]
                spacing = int(st.get("spacing") or 0)
                ref_price = int(st.get("ref_price") or 0)
                if buy_lines and spacing > 0 and ref_price > 0:
                    new_buys = 0
                    ledger_path = ROOT / "day_ledger.json"
                    if ledger_path.exists():
                        try:
                            led = json.loads(
                                ledger_path.read_text(encoding="utf-8")
                            )
                            new_buys = len(led.get("round_trips") or [])
                        except Exception:  # noqa: BLE001
                            new_buys = 0
                    if new_buys <= 0:
                        # Fallback: filled BUY in all_orders that are not recycle
                        for od in st.get("all_orders") or []:
                            if str(od.get("side", "")).upper() != "BUY":
                                continue
                            if str(od.get("status", "")).lower() != "filled":
                                continue
                            if od.get("is_recycle_rebuy"):
                                continue
                            new_buys += 1
                    engine.resume_from_state(
                        buy_lines=buy_lines,
                        spacing=spacing,
                        ref_price=ref_price,
                        day_high=st.get("day_high"),
                        new_buys_filled_today=new_buys,
                        now=datetime.now(SEOUL),
                        restored_order_ids=list(live._orders),
                    )
                else:
                    log_fn(
                        "session.resume_skip",
                        "orders_state missing buy_lines/spacing/ref — "
                        "will fall through to on_session_start on first tick",
                        {},
                    )
            except Exception as e:  # noqa: BLE001
                log_fn("session.restore_fail", str(e), {"error": type(e).__name__})

    now = datetime.now(SEOUL)
    use_virtual_clock = False
    if not in_session(now, sess["start"], sess["end"], tz_name):
        t = now.time()
        if t < parse_hhmm(sess["start"]):
            msg = (
                f"before session start ({sess['start']} Seoul); now={now.isoformat(timespec='seconds')}"
            )
            print(msg)
            if wait_for_open and not dry:
                # Live path can wait; dry verification usually uses --max-ticks
                while True:
                    now = datetime.now(SEOUL)
                    if in_session(now, sess["start"], sess["end"], tz_name):
                        break
                    print(f"waiting for open… {now.strftime('%H:%M:%S')} Seoul")
                    time.sleep(min(30.0, max(5.0, interval_sec)))
            elif max_ticks is not None and dry:
                print("dry-loop + max_ticks: using VIRTUAL session clock for engine decisions")
                use_virtual_clock = True
            else:
                print("exit: not in session (pass --max-ticks with --dry-loop to verify off-hours)")
                return 0
        else:
            # After session end
            msg = (
                f"after session end ({sess['end']} Seoul); now={now.isoformat(timespec='seconds')}"
            )
            print(msg)
            if max_ticks is not None and dry:
                print("dry-loop + max_ticks: using VIRTUAL session clock for engine decisions")
                use_virtual_clock = True
            else:
                print("exit: session already ended")
                return 0

    tick = 0
    try:
        while True:
            if max_ticks is not None and tick >= max_ticks:
                print(f"reached max_ticks={max_ticks}")
                break

            wall_now = datetime.now(SEOUL)
            if not use_virtual_clock and not in_session(
                wall_now, sess["start"], sess["end"], tz_name
            ):
                if wall_now.time() > parse_hhmm(sess["end"]):
                    print(f"session end reached at {wall_now.strftime('%H:%M:%S')} Seoul")
                    # Final cancel unfilled buys
                    engine.on_session_end(wall_now)
                    break
                print("left session window unexpectedly")
                break

            engine_now = (
                _virtual_session_now(tick, cfg) if use_virtual_clock else wall_now
            )

            try:
                px = int(price_feed.get_last_price())
            except Exception as e:  # noqa: BLE001
                telegram_alert(
                    f"grid-bot inquire_price fail: {type(e).__name__}: {e}", cfg=cfg
                )
                log_fn("session.price_fail", str(e), {"error": type(e).__name__, "tick": tick})
                print(f"API fail on price — stopping: {type(e).__name__}: {e}")
                return 1

            prices_seen.append(px)
            print(
                f"\n--- tick {tick+1}"
                f"{'' if max_ticks is None else f'/{max_ticks}'}  "
                f"wall={wall_now.strftime('%H:%M:%S')} Seoul  "
                f"engine_t={engine_now.strftime('%H:%M:%S')}  price={px} ---"
            )
            log_fn(
                "session.tick",
                f"price={px}",
                {
                    "tick": tick,
                    "price": px,
                    "engine_time": engine_now.isoformat(timespec="seconds"),
                    "dry": dry,
                },
            )

            # Drive engine with live (or virtual-clock) time
            engine.on_price(px, engine_now)

            if not dry:
                try:
                    fills = live.poll_fills_via_daily_ccld()
                    if fills:
                        print(f"  live fills detected: {[f.order_id for f in fills]}")
                except Exception as e:  # noqa: BLE001
                    telegram_alert(
                        f"grid-bot fill poll fail: {type(e).__name__}: {e}", cfg=cfg
                    )
                    log_fn(
                        "session.fill_poll_fail",
                        str(e),
                        {"error": type(e).__name__, "tick": tick},
                    )
                    print(f"API fail on fill poll — stopping: {type(e).__name__}: {e}")
                    return 1

            # Persist each tick
            save_session_orders_state(
                broker=engine_broker,
                engine=engine,
                mode="live-session-dry" if dry else "live-session",
                extra={"prices_seen_sample": prices_seen[-5:], "tick": tick},
            )
            # positions.json already via engine._persist on fills / session

            tick += 1
            if max_ticks is not None and tick >= max_ticks:
                # If virtual clock and we never hit real session end, optionally
                # demonstrate would-cancel of unfilled buys on final tick.
                if dry and use_virtual_clock and not engine._session_ended:
                    end_t = parse_hhmm(sess["end"])
                    end_now = datetime.now(SEOUL).replace(
                        hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0
                    )
                    # nudge past end so on_price triggers session end, or call directly
                    past = end_now + timedelta(minutes=1)
                    print(
                        f"dry max_ticks done — demonstrating session-end "
                        f"would-cancel at virtual {past.strftime('%H:%M')}"
                    )
                    engine.on_session_end(past)
                    save_session_orders_state(
                        broker=engine_broker,
                        engine=engine,
                        mode="live-session-dry",
                        extra={"note": "post session-end cancel demo", "tick": tick},
                    )
                break

            time.sleep(max(0.0, float(interval_sec)))

    except KeyboardInterrupt:
        print("\ninterrupted — persisting state")
        save_session_orders_state(
            broker=engine_broker,
            engine=engine,
            mode="live-session-dry" if dry else "live-session",
            extra={"note": "interrupted"},
        )

    open_orders = engine_broker.list_open_orders(cfg["symbol"])
    print("\n" + "=" * 72)
    print("SESSION LOOP DONE" + (" (DRY)" if dry else ""))
    print(f"  ticks={tick}  prices_seen={prices_seen}")
    if prices_seen:
        print(f"  price min/max/last = {min(prices_seen)}/{max(prices_seen)}/{prices_seen[-1]}")
    print(f"  positions={len(engine.positions)}  open_orders={len(open_orders)}")
    for o in open_orders:
        print(
            f"    OPEN {o.side.value} {o.qty}@{o.price} "
            f"id={o.order_id} status={o.status.value} tag={o.client_tag}"
        )
    # Summarize shadow intents from this run's broker orders
    all_orders = list(getattr(engine_broker, "_orders", {}).values())
    submits = [o for o in all_orders]
    print(f"  broker_orders_tracked={len(submits)}")
    for o in submits[:20]:
        print(
            f"    {o.status.value:10s} {o.side.value} {o.qty}@{o.price} "
            f"id={o.order_id} tag={o.client_tag}"
        )
    print(f"  orders_state={ORDERS_STATE_PATH}")
    print(f"  positions_file={cfg['persistence']['file']}")
    print(f"  run_log={run_log}")
    if dry:
        print("  NO real orders placed (ShadowBroker / allow_mutations=False)")
    print("=" * 72)
    log_fn(
        "session.summary",
        "done",
        {
            "ticks": tick,
            "prices_seen": prices_seen,
            "dry": dry,
            "open_orders": [o.to_dict() for o in open_orders],
            "positions": [p.to_dict() for p in engine.positions],
        },
    )
    return 0


def run_live_session_from_args(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """CLI adapter: dry-loop by default; live mutations only with explicit approval."""
    dry = bool(getattr(args, "dry_loop", False))
    # Default dry unless user explicitly asks for live orders
    if not dry and not getattr(args, "i_approve_live_orders", False):
        # --run-session without --dry-loop and without approval → treat as dry
        # unless they thought they wanted live (then refuse)
        if getattr(args, "force_live_session", False):
            ok, reason = live_approval_ok(args, cfg)
            if not ok:
                print("LIVE SESSION REFUSED — no orders.", file=sys.stderr)
                print(f"  reason: {reason}", file=sys.stderr)
                return 2
            dry = False
            approve = True
        else:
            print(
                "NOTE: --run-session defaults to dry-loop (no real orders). "
                "Pass --dry-loop explicitly, or --i-approve-live-orders for live."
            )
            dry = True
            approve = False
    elif dry:
        approve = False
    else:
        ok, reason = live_approval_ok(args, cfg)
        if not ok:
            print("LIVE SESSION REFUSED — no orders.", file=sys.stderr)
            print(f"  reason: {reason}", file=sys.stderr)
            return 2
        approve = True
        dry = False
        print(f"LIVE SESSION approved via: {reason}")

    interval = float(getattr(args, "interval", 5.0) or 5.0)
    max_ticks = getattr(args, "max_ticks", None)
    return run_live_session(
        cfg,
        dry=dry,
        approve=approve,
        interval_sec=interval,
        max_ticks=max_ticks,
        wait_for_open=bool(getattr(args, "wait_for_open", False)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRX ETF grid bot")
    parser.add_argument(
        "--mode",
        choices=["paper", "live-dry", "live", "live-session-dry"],
        default="paper",
        help="paper=mock; live-dry=read-only snapshot; live=gated; "
        "live-session-dry=alias for live --run-session --dry-loop",
    )
    parser.add_argument("--scenario", choices=["demo", "idle"], default="demo")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--i-approve-live-orders",
        action="store_true",
        help="Explicit approval for live submit/cancel (required with --mode live)",
    )
    parser.add_argument(
        "--bootstrap-grid",
        action="store_true",
        help="Live only: place initial buy grid once, save order ids, exit",
    )
    parser.add_argument(
        "--run-session",
        action="store_true",
        help="Live session loop (default dry unless --i-approve-live-orders)",
    )
    parser.add_argument(
        "--dry-loop",
        action="store_true",
        help="With --run-session: live prices + ShadowBroker, no real orders",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Stop after N price polls (dry verification / tests)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between price polls (default 5)",
    )
    parser.add_argument(
        "--wait-for-open",
        action="store_true",
        help="If before 09:05 Seoul, sleep until open (live path)",
    )
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))

    if args.mode == "live-session-dry":
        args.run_session = True
        args.dry_loop = True
        return run_live_session_from_args(cfg, args)

    if args.mode == "live-dry":
        return run_live_dry(cfg)

    if args.mode == "live":
        if args.bootstrap_grid:
            return run_live_bootstrap(cfg, args)
        if args.run_session:
            return run_live_session_from_args(cfg, args)
        # Default live without subcommand: refuse mutations, explain
        ok, reason = live_approval_ok(args, cfg)
        print("LIVE mode requires an action flag.", file=sys.stderr)
        print("  First ship: --bootstrap-grid (places 5 buy limits once, then exits)", file=sys.stderr)
        print("  Session:    --run-session --dry-loop [--max-ticks N] [--interval S]", file=sys.stderr)
        print("  Live loop:  --run-session --i-approve-live-orders  (REAL ORDERS)", file=sys.stderr)
        print(f"  Approval status: {'OK — ' + reason if ok else 'BLOCKED — ' + reason}", file=sys.stderr)
        return 2

    if args.scenario == "demo":
        summary = run_paper_demo(cfg)
        ok = (
            summary["grid_placed"]
            and summary["buy_fills"] >= 1
            and summary["sell_fills"] >= 1
            and summary["ratchets"] >= 1
            and summary["session_end_cancels"] >= 1
        )
        if not ok:
            print("WARNING: demo did not hit all expected events:", summary, file=sys.stderr)
            return 1
        print("\nSUCCESS: demo proved grid, buy fill, TP sell, ratchet, session-end cancel.")
        return 0

    print("idle scenario: config loaded, nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
