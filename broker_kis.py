"""Live KIS broker via local open-trading-api examples (kis_auth).

Safety:
  - allow_mutations=False (default): submit/cancel raise LiveOrderApprovalError.
  - Price / balance inquiry always allowed after connect().
  - Never log or print app keys, secrets, or tokens.
"""
from __future__ import annotations

import importlib.util
import itertools
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from models import Order, OrderStatus, Side
from state_machine import transition, can_cancel, is_active
from session import SEOUL
from persistence import append_jsonl
from broker_mock import BrokerAdapter

OTA_LLM = Path("/workspace/open-trading-api/examples_llm")
DOMESTIC = OTA_LLM / "domestic_stock"


class LiveOrderApprovalError(RuntimeError):
    """Raised when submit/cancel attempted without live approval."""


class KISApiError(RuntimeError):
    """KIS API call failed after retries."""


def _ensure_ota_path() -> None:
    root = str(OTA_LLM)
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_example(mod_name: str, subdir: str):
    """Load an examples_llm domestic_stock module by file path."""
    _ensure_ota_path()
    path = DOMESTIC / subdir / f"{mod_name}.py"
    # Ensure sibling imports (kis_auth) resolve; also add the example dir
    # so `from inquire_price import ...` style works if needed.
    d = str(path.parent)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(f"kis_ex_{subdir}_{mod_name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


FillCallback = Callable[[Order], None]
LogFn = Callable[[str, str, dict], None]


@dataclass
class LiveKISBroker(BrokerAdapter):
    """BrokerAdapter backed by KIS REST (order_cash / order_rvsecncl / inquire_*).

    Fill polling (optional):
      Call ``poll_fills_via_daily_ccld()`` periodically. It queries
      ``inquire_daily_ccld`` (pd_dv=inner, ccld_dvsn=01 체결) for today's
      fills matching stored ODNO values, then transitions matching open
      orders to FILLED and invokes ``on_fill``. Wire this into a live
      session loop when ready (TODO in main.py).
    """

    symbol: str = "367380"
    allow_mutations: bool = False
    api_retry: int = 3
    env_dv: str = "real"  # real | demo  (maps auth svr prod|vps)
    market_div: str = "J"  # KRX
    excg_id_dvsn_cd: str = "KRX"
    on_fill: Optional[FillCallback] = None
    log: Optional[LogFn] = None
    intent_log_dir: Path = field(default_factory=lambda: Path("/workspace/grid-bot/logs"))
    _orders: dict[str, Order] = field(default_factory=dict)
    _kis_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    # order_id -> {odno, ord_orgno, side, price, qty, status}
    _id_seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    _last_price: int = 0
    _connected: bool = False
    _ka: Any = field(default=None, repr=False)
    _order_cash_fn: Any = field(default=None, repr=False)
    _order_rvsecncl_fn: Any = field(default=None, repr=False)
    _inquire_price_fn: Any = field(default=None, repr=False)
    _inquire_balance_fn: Any = field(default=None, repr=False)
    _inquire_daily_ccld_fn: Any = field(default=None, repr=False)

    # ---- lifecycle ----
    def connect(self) -> None:
        _ensure_ota_path()
        import kis_auth as ka  # noqa: WPS433

        self._ka = ka
        svr = "prod" if self.env_dv == "real" else "vps"
        ka.auth(svr=svr)
        # Lazy-load trading / quotation helpers
        self._order_cash_fn = _load_example("order_cash", "order_cash").order_cash
        self._order_rvsecncl_fn = _load_example("order_rvsecncl", "order_rvsecncl").order_rvsecncl
        self._inquire_price_fn = _load_example("inquire_price", "inquire_price").inquire_price
        self._inquire_balance_fn = _load_example("inquire_balance", "inquire_balance").inquire_balance
        self._inquire_daily_ccld_fn = _load_example(
            "inquire_daily_ccld", "inquire_daily_ccld"
        ).inquire_daily_ccld
        self._connected = True
        self._emit("kis.connect", f"connected env_dv={self.env_dv} symbol={self.symbol}", {})

    def _require_connected(self) -> None:
        if not self._connected or self._ka is None:
            raise RuntimeError("LiveKISBroker.connect() required before API use")

    def _trenv(self):
        self._require_connected()
        return self._ka.getTREnv()

    def _emit(self, kind: str, msg: str, data: dict | None = None) -> None:
        if self.log:
            self.log(kind, msg, data or {})

    def _client_oid(self) -> str:
        return f"LIVE-{next(self._id_seq):06d}"

    def _require_mutations(self, action: str) -> None:
        if not self.allow_mutations:
            raise LiveOrderApprovalError(
                f"REFUSED: live {action} blocked — need --mode live AND "
                f"(--i-approve-live-orders OR LIVE_APPROVED with today's Seoul YYYY-MM-DD). "
                f"Read-only price/balance inquiry is allowed without approval."
            )

    def _with_retry(self, label: str, fn: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.api_retry + 1):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 — retry wrapper
                last = e
                self._emit(
                    "kis.retry",
                    f"{label} attempt {attempt}/{self.api_retry} failed: {type(e).__name__}",
                    {"attempt": attempt},
                )
                if attempt < self.api_retry:
                    time.sleep(0.4 * attempt)
        raise KISApiError(f"{label} failed after {self.api_retry} retries: {last}") from last

    def _intent_path(self) -> Path:
        self.intent_log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(SEOUL).strftime("%Y%m%d")
        return self.intent_log_dir / f"live-intent-{stamp}.jsonl"

    def _log_intent(self, action: str, order: Order, extra: dict | None = None) -> None:
        rec = {
            "ts": datetime.now(SEOUL).isoformat(timespec="seconds"),
            "action": action,
            "order_id": order.order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "price": order.price,
            "qty": order.qty,
            "client_tag": order.client_tag,
            "status": order.status.value,
        }
        if extra:
            rec.update(extra)
        append_jsonl(str(self._intent_path()), rec)
        self._emit("kis.intent", f"{action} {order.side.value} {order.qty}@{order.price}", rec)

    # ---- BrokerAdapter: prices ----
    def set_last_price(self, price: int) -> None:
        """Optional local override (tests); live prefers get_last_price()."""
        self._last_price = int(price)

    def get_last_price(self) -> int:
        self._require_connected()

        def _call():
            df = self._inquire_price_fn(self.env_dv, self.market_div, self.symbol)
            if df is None or getattr(df, "empty", True):
                raise KISApiError("inquire_price returned empty")
            px = int(df.iloc[0]["stck_prpr"])
            if px <= 0:
                raise KISApiError(f"invalid stck_prpr={px}")
            return px

        px = self._with_retry("inquire_price", _call)
        self._last_price = px
        return px

    def inquire_balance_summary(self) -> dict[str, Any]:
        """Read-only cash/holdings summary. Never logs credentials."""
        self._require_connected()
        tr = self._trenv()

        def _call():
            df1, df2 = self._inquire_balance_fn(
                env_dv=self.env_dv,
                cano=tr.my_acct,
                acnt_prdt_cd=tr.my_prod,
                afhr_flpr_yn="N",
                inqr_dvsn="02",
                unpr_dvsn="01",
                fund_sttl_icld_yn="N",
                fncg_amt_auto_rdpt_yn="N",
                prcs_dvsn="01",
            )
            return df1, df2

        df1, df2 = self._with_retry("inquire_balance", _call)
        cash = None
        if df2 is not None and not getattr(df2, "empty", True):
            row = df2.iloc[0]
            raw = row.get("dnca_tot_amt", None)
            if raw is not None and str(raw) != "":
                cash = int(float(str(raw)))
        holdings: list[dict[str, Any]] = []
        if df1 is not None and not getattr(df1, "empty", True):
            for _, r in df1.iterrows():
                pdno = str(r.get("pdno", ""))
                if pdno != self.symbol:
                    continue
                holdings.append(
                    {
                        "pdno": pdno,
                        "hldg_qty": int(float(str(r.get("hldg_qty", 0) or 0))),
                        "pchs_avg_pric": r.get("pchs_avg_pric"),
                        "prpr": r.get("prpr"),
                    }
                )
        return {"dnca_tot_amt": cash, "holdings_symbol": holdings}

    # ---- BrokerAdapter: orders ----
    def submit(self, order: Order) -> Order:
        self._require_mutations("submit")
        self._require_connected()

        # Prevent duplicate active orders at same side+price (local map)
        for existing in self._orders.values():
            if (
                is_active(existing)
                and existing.symbol == order.symbol
                and existing.side == order.side
                and existing.price == order.price
            ):
                raise ValueError(
                    f"duplicate price order blocked: {order.side.value} @{order.price}"
                )

        if not order.order_id:
            order.order_id = self._client_oid()
        order.status = OrderStatus.PENDING
        now = datetime.now(SEOUL).isoformat(timespec="seconds")
        order.created_at = now
        order.updated_at = now

        self._log_intent("submit", order)

        tr = self._trenv()
        ord_dv = "buy" if order.side == Side.BUY else "sell"
        sll_type = "01" if order.side == Side.SELL else ""

        def _call():
            df = self._order_cash_fn(
                env_dv=self.env_dv,
                ord_dv=ord_dv,
                cano=tr.my_acct,
                acnt_prdt_cd=tr.my_prod,
                pdno=order.symbol,
                ord_dvsn="00",
                ord_qty=str(order.qty),
                ord_unpr=str(order.price),
                excg_id_dvsn_cd=self.excg_id_dvsn_cd,
                sll_type=sll_type,
            )
            if df is None or getattr(df, "empty", True):
                raise KISApiError("order_cash returned empty (rejected?)")
            return df

        df = self._with_retry("order_cash", _call)
        row = df.iloc[0]
        # Response keys may be upper or mixed; normalize
        keys = {str(k).upper(): k for k in row.index}
        odno = str(row[keys.get("ODNO", "ODNO")])
        orgno = str(row[keys.get("KRX_FWDG_ORD_ORGNO", "KRX_FWDG_ORD_ORGNO")])

        self._orders[order.order_id] = order
        self._kis_meta[order.order_id] = {
            "odno": odno,
            "ord_orgno": orgno,
            "side": order.side.value,
            "price": order.price,
            "qty": order.qty,
            "status": OrderStatus.OPEN.value,
        }
        transition(order, "submit_ack")
        order.updated_at = datetime.now(SEOUL).isoformat(timespec="seconds")
        self._kis_meta[order.order_id]["status"] = order.status.value
        self._emit(
            "broker.ack",
            f"{order.order_id} -> open odno={odno}",
            {"order_id": order.order_id, "odno": odno, "ord_orgno": orgno},
        )
        return order

    def cancel(self, order_id: str) -> Order:
        self._require_mutations("cancel")
        self._require_connected()
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"unknown order_id={order_id}")
        if not can_cancel(order) and order.status != OrderStatus.CANCELING:
            self._emit(
                "broker.cancel_skip",
                f"cannot cancel {order_id} status={order.status.value}",
                {},
            )
            return order

        meta = self._kis_meta.get(order_id)
        if not meta or not meta.get("odno") or not meta.get("ord_orgno"):
            raise KISApiError(f"missing KIS odno/ord_orgno for {order_id}")

        if order.status != OrderStatus.CANCELING:
            transition(order, "request_cancel")
            order.updated_at = datetime.now(SEOUL).isoformat(timespec="seconds")

        self._log_intent(
            "cancel",
            order,
            {"odno": meta["odno"], "ord_orgno": meta["ord_orgno"]},
        )

        tr = self._trenv()

        def _call():
            df = self._order_rvsecncl_fn(
                env_dv=self.env_dv,
                cano=tr.my_acct,
                acnt_prdt_cd=tr.my_prod,
                krx_fwdg_ord_orgno=str(meta["ord_orgno"]),
                orgn_odno=str(meta["odno"]),
                ord_dvsn="00",
                rvse_cncl_dvsn_cd="02",
                ord_qty=str(order.qty),
                ord_unpr=str(order.price),
                qty_all_ord_yn="Y",
                excg_id_dvsn_cd=self.excg_id_dvsn_cd,
            )
            if df is None or getattr(df, "empty", True):
                raise KISApiError("order_rvsecncl returned empty")
            return df

        self._with_retry("order_rvsecncl", _call)
        if order.status == OrderStatus.CANCELING:
            transition(order, "cancel_ack")
            order.updated_at = datetime.now(SEOUL).isoformat(timespec="seconds")
            self._kis_meta[order_id]["status"] = order.status.value
            self._emit(
                "broker.cancel_ack",
                f"{order_id} -> canceled",
                {"order_id": order_id},
            )
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def list_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        out: list[Order] = []
        for o in self._orders.values():
            if is_active(o) and (symbol is None or o.symbol == symbol):
                out.append(o)
        return out

    def export_kis_meta(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._kis_meta.items()}

    def poll_fills_via_daily_ccld(self) -> list[Order]:
        """Optional fill sync via inquire_daily_ccld (today, 체결 only).

        How to use in a live loop:
          1. Keep orders OPEN in ``_orders`` / ``_kis_meta``.
          2. Every N seconds call this method.
          3. Matching ODNO with filled qty → transition to FILLED + on_fill.
        """
        self._require_connected()
        tr = self._trenv()
        today = datetime.now(SEOUL).strftime("%Y%m%d")

        def _call():
            return self._inquire_daily_ccld_fn(
                env_dv=self.env_dv,
                pd_dv="inner",
                cano=tr.my_acct,
                acnt_prdt_cd=tr.my_prod,
                inqr_strt_dt=today,
                inqr_end_dt=today,
                sll_buy_dvsn_cd="00",
                ccld_dvsn="01",
                inqr_dvsn="00",
                inqr_dvsn_3="00",
                pdno=self.symbol,
                excg_id_dvsn_cd=self.excg_id_dvsn_cd,
            )

        df1, _df2 = self._with_retry("inquire_daily_ccld", _call)
        fills: list[Order] = []
        if df1 is None or getattr(df1, "empty", True):
            return fills

        odno_to_oid = {m["odno"]: oid for oid, m in self._kis_meta.items()}
        for _, row in df1.iterrows():
            keys = {str(k).upper(): k for k in row.index}
            odno_key = keys.get("ODNO") or keys.get("ORGN_ODNO")
            if odno_key is None:
                continue
            odno = str(row[odno_key]).lstrip("0") or "0"
            # also try raw
            candidates = {str(row[odno_key]), odno, str(row[odno_key]).zfill(7)}
            matched_oid = None
            for cand in candidates:
                if cand in odno_to_oid:
                    matched_oid = odno_to_oid[cand]
                    break
                for k, oid in odno_to_oid.items():
                    if str(k).lstrip("0") == odno:
                        matched_oid = oid
                        break
                if matched_oid:
                    break
            if not matched_oid:
                continue
            # Only treat as fill when KIS reports actual executed qty
            ccld_key = keys.get("TOT_CCLD_QTY")
            try:
                ccld_qty = int(float(str(row[ccld_key]))) if ccld_key is not None else 0
            except (TypeError, ValueError):
                ccld_qty = 0
            if ccld_qty <= 0:
                continue
            avg_key = keys.get("AVG_PRVS")
            try:
                avg_px = int(float(str(row[avg_key]))) if avg_key is not None else 0
            except (TypeError, ValueError):
                avg_px = 0
            order = self._orders.get(matched_oid)
            if order is None or order.status == OrderStatus.FILLED:
                continue
            if not is_active(order) and order.status != OrderStatus.CANCELING:
                continue
            try:
                transition(
                    order,
                    "fill",
                    fill_price=avg_px or order.price,
                    fill_qty=min(ccld_qty, order.qty),
                )
            except Exception:  # noqa: BLE001
                continue
            order.updated_at = datetime.now(SEOUL).isoformat(timespec="seconds")
            self._kis_meta[matched_oid]["status"] = order.status.value
            fills.append(order)
            self._emit("broker.fill", f"{matched_oid} filled via daily_ccld", order.to_dict())
            if self.on_fill:
                self.on_fill(order)
        return fills
