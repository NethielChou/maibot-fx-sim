"""订单引擎：内存状态机 + 撮合（P0：市价 / 限价 / 止损 / 持仓 OCO）。

职责边界（`docs/INTERFACES.md` + Lead 集成约定）：
- **不写 DB、不读写文件、不拉行情**。`process_tick` 只读 `account` / `positions`
  （由 scheduler 提供），返回 `FillResult`，由 scheduler 调 `ledger` 落库并应用持仓/余额。
- 新規成交的 `Fill.position_id` 由引擎生成：`POS-<order_id>`；
  平仓成交填目标持仓 id（用户显式 `position_id` 或同货币对反向 FIFO）。
- 强制平仓的**实际强平由 scheduler 调 accounting 执行**；引擎只负责：
  ① S1→S2 用户平仓单先撮合；② 之后重算维持率，若仍跌破线则**取消本 tick 剩余新规挂单**
  并跳过 S4/S5（`last_loss_cut_triggered=True` 供 scheduler 判定）。
- 拒单返回 `Order(status="rejected", reject_reason=原因码)`，**不抛异常**；
  挂起写 `suspend_reason`，不丢单、不改状态，恢复后自动解除。
- 幂等：同一 `client_order_id` 重复 `submit` 返回**原订单**，不重复建单/成交。

同 tick 优先级（Rev 1.1 §5.2，固定实现）：
```
市价（提交后首 tick 立即撮合，独立于 S1–S5）
S1 平仓止损 / 追踪止损     （close + stop/trailing）
S2 平仓限价                       （close + limit）
S3 维持率重算（< loss_cut_ratio → 取消剩余新规挂单，跳过 S4/S5）
S4 新規止损                     （open + stop）
S5 新規限价                       （open + limit）
同一阶段 order_id 升序（服务端 ID 单调，创建早者先）
```

成交价规则（§4.1/§4.2）：
- 买用 **ask**、卖用 **bid**；mid 只用于估值，绝不成交
- 限价不劣化：买 `ask ≤ limit` 才成交且成交价 = ask；卖对称；跳空取更优价
- 止损转市价 + **逆向滑点**；跳空写 `gap_pips`；限价**不加滑点**
- 价格一律先按 tick 对齐（滑点向不利方向取整）

已知 P0 边界（与规格差异，交付时须上报 Lead）：
1. 市价单在 `submit` 后由**首个 `process_tick`** 撮合（等价于命令阶段同步成交）；
2. `trailing_stop` 仅按“用户给定 trigger 的止损”处理（P1 才做锚点/步进），
   配置 `orders.allow_trailing` 默认 false；
3. IFD/IFO 不实现（`allow_ifd` 默认 false）；`created` 态仅出现在恢复数据中，
   恢复时按 `pending` 参与撮合；
4. 挂单保证金取置不实现（每 tick 用实时保证金校验 + 单笔 15% 上限兜底）；
5. TIF：仅处理显式 `expires_at`（GTD）；`DAY` 日切由 scheduler/market_clock 负责；
6. OCO 原子性：两条腿由两次 `submit` 建立，第二条校验冲突时**两条都置 rejected**
   （`OCO_CONFLICT`），避免半组生效；单腿 OCO 允许存在。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Callable, Mapping, Sequence

import accounting
import risk
from fxconv import RATE_UNAVAILABLE, FxRates
from models import (
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    ORDER_TRAILING,
    POS_CLOSE,
    POS_OPEN,
    SIDE_BUY,
    STATUS_CANCELLED,
    STATUS_CREATED,
    STATUS_EXPIRED,
    STATUS_FILLED,
    STATUS_PENDING,
    STATUS_REJECTED,
    AccountState,
    Fill,
    FillResult,
    Order,
    OrderRequest,
    PairSpec,
    Position,
    QuoteSnapshot,
)

# 默认滑点（pips，fixed 模式；规格 §4.4 / margin-swap §5.1）。
# 测试与集成可注入自定义 slippage_fn（签名 (order_type, spec) -> Decimal）。
DEFAULT_SLIPPAGE_PIPS: dict[str, Decimal] = {
    ORDER_MARKET: Decimal("0.2"),       # 市价（含平仓市价）
    ORDER_STOP: Decimal("0.5"),         # 止损触发后市价
    ORDER_TRAILING: Decimal("0.5"),
    ORDER_LIMIT: Decimal("0"),          # 限价不加滑点
    "loss_cut": Decimal("1.5"),         # 系统强平（引擎不执行，仅 memo）
}
UNITS_STEP = 1_000
MAX_QUOTE_AGE_FILL_SECONDS_DEFAULT = 5
PRICE_DEVIATION_LIMIT_DEFAULT = Decimal("0.20")  # 限价/触发价距 mid 超过 20% 拒单


def default_slippage_pips(order_type: str, spec: PairSpec) -> Decimal:
    """默认固定滑点（pips）：市价 0.2 / 止损 0.5 / 限价 0。"""
    return DEFAULT_SLIPPAGE_PIPS.get(order_type, Decimal("0"))


def _dec(value: Decimal | int | float | str) -> Decimal:
    """Decimal/int/float/str → Decimal（float 走 str）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("INVALID_DECIMAL: bool is not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError(f"INVALID_DECIMAL: unsupported type {type(value)!r}")


def _align_unfavorable(price: Decimal, tick: Decimal, side: str) -> Decimal:
    """按 tick 对齐价格，且**向不利方向**取整（买向上、卖向下）。"""
    rounding = ROUND_CEILING if side == SIDE_BUY else ROUND_FLOOR
    return price.quantize(tick, rounding=rounding)


def _align_nearest(price: Decimal, tick: Decimal) -> Decimal:
    """按 tick 四舍五入对齐（限价成交价用；行情层应已对齐，此处兜底）。"""
    return price.quantize(tick, rounding=ROUND_HALF_UP)


def _quote_age_seconds(quote: QuoteSnapshot, now: datetime) -> float:
    """报价年龄（秒）：now − recv_ts；异常/缺失返回 0。"""
    if quote is None or quote.recv_ts is None or now is None:
        return 0.0
    try:
        return (now - quote.recv_ts).total_seconds()
    except TypeError:  # naive/aware 混用 → 视为新鲜（由行情层保证 tz）
        return 0.0


class _SimState:
    """本 tick 的模拟账务状态（仅供 S3 维持率重算，不回写调用方的 account/positions）。"""

    def __init__(self, account: AccountState, positions: Sequence[Position]) -> None:
        self.balance: int = int(account.balance)
        self.positions: list[Position] = [replace(p) for p in positions]

    def find(self, position_id: str) -> Position | None:
        for pos in self.positions:
            if pos.position_id == position_id:
                return pos
        return None

    def apply_fill(self, fill: Fill, spec: PairSpec, leverage: int, rates: FxRates | None) -> None:
        """应用一笔成交到模拟状态：改余额、建/减持仓。"""
        self.balance += int(fill.realized_pnl) - int(fill.fee)
        if fill.position_type == POS_OPEN:
            self.positions.append(
                Position(
                    position_id=fill.position_id or f"POS-{fill.order_id}",
                    pair=fill.pair,
                    side=fill.side,
                    units=fill.units,
                    entry_price=fill.price,
                    opened_at=fill.ts,
                    margin=accounting.required_margin(spec, fill.units, fill.price, rates, leverage),
                )
            )
            return
        pos = self.find(fill.position_id or "")
        if pos is None:
            return
        pos.units -= fill.units
        if pos.units <= 0:
            self.positions.remove(pos)


class OrderEngine:
    """P0 订单引擎（内存态）。

    参数：
    - `pairs`: `{pair_code: PairSpec}`；必填（A 的 `pairs.PAIRS` 或测试构造）
    - `cfg`: `config.MaiFxConfig` 结构对象（鸭子类型；不 import 宿主 SDK）
    - `slippage_fn`（可选）: `(order_type: str, spec: PairSpec) -> Decimal`，返回 pips；
      默认 `default_slippage_pips`。集成时可注入 A 的 `execution_cost.slippage_pips` 适配器。
    - `usable_fn`（可选）: `(spec, quote, now, cfg) -> (ok: bool, reason: str)`；引擎自带的
      `_classify` 已覆盖缺失/过期/倒挂/点差超限，保留扩展口给 A 的 `execution_cost.quote_usable`。

    引擎不持久化；`restore()` 由 scheduler 启动时用 `ledger.load_orders_pending()` 灌入。
    """

    def __init__(
        self,
        pairs: Mapping[str, PairSpec],
        cfg,
        *,
        slippage_fn: Callable[[str, PairSpec], Decimal] | None = None,
        usable_fn: Callable[[PairSpec, QuoteSnapshot | None, datetime, object], tuple[bool, str]] | None = None,
    ) -> None:
        if not pairs:
            raise ValueError("INVALID_PAIRS: pairs must not be empty")
        self.pairs: dict[str, PairSpec] = dict(pairs)
        self.cfg = cfg
        self._slippage_fn = slippage_fn or default_slippage_pips
        self._usable_fn = usable_fn

        self._orders: dict[str, Order] = {}
        self._pending: dict[str, Order] = {}
        self._client_ids: dict[str, str] = {}
        self._close_target: dict[str, str] = {}      # order_id -> position_id
        self._oco: dict[str, list[str]] = {}         # oco_group_id -> order_ids
        self._last_quote_ts: dict[str, datetime] = {}
        self._last_quote_seq: dict[str, int] = {}
        self._order_seq: int = 1
        self._tick_seq: int = 0

        # 审计事件（append-only）：(order_id, event_type, detail)，供 scheduler 写 order_events
        self.events: list[tuple[str, str, str]] = []
        # 本 tick S3 判定结果（scheduler 据此决定是否执行一次性全部平仓）
        self.last_loss_cut_triggered: bool = False

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------
    @property
    def pending(self) -> list[Order]:
        """当前有效挂单（`created/pending` 且未终态），按 order_id 升序。"""
        return [self._pending[k] for k in sorted(self._pending)]

    def all_orders(self) -> list[Order]:
        """引擎见过的全部订单（含终态），按 order_id 升序；scheduler 落库用。"""
        return [self._orders[k] for k in sorted(self._orders)]

    def get_order(self, order_id: str) -> Order | None:
        """按 order_id 取订单；未知返回 None。"""
        return self._orders.get(order_id)

    def drain_events(self) -> list[tuple[str, str, str]]:
        """取走并清空审计事件缓冲（scheduler 每 tick 落库后调用）。"""
        events, self.events = self.events, []
        return events

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------
    def submit(
        self,
        req: OrderRequest,
        quotes: Mapping[str, QuoteSnapshot],
        now: datetime,
        account: AccountState,
        positions: Sequence[Position],
        rates: FxRates | None,
    ) -> Order:
        """提交订单（含校验/拒单原因码）。

        - 幂等：`client_order_id` 命中 → 返回原订单（不重复校验/建单）
        - 校验失败 → 返回 `Order(status=rejected, reject_reason=...)`，不抛异常
        - 通过 → 返回 `Order(status=pending)`，由后续 `process_tick` 撮合
          （市价单在首个 tick 立即成交）
        """
        positions = list(positions or [])
        quotes = dict(quotes or {})

        # 1) 幂等命中
        client_id = req.client_order_id or ""
        if client_id and client_id in self._client_ids:
            existing = self._orders.get(self._client_ids[client_id])
            if existing is not None:
                self._event(existing.order_id, "request_ignored", "DUPLICATE_CLIENT_ORDER_ID")
                return existing

        # 2) 建单占位（拒单也保留审计行）
        order = Order(
            order_id=self._alloc_order_id(),
            request=req,
            status=STATUS_CREATED,
            created_at=now,
            updated_at=now,
        )
        self._orders[order.order_id] = order

        reason = self._validate_create(order, quotes, now, account, positions, rates)
        if not reason and req.oco_group_id:
            reason = self._validate_oco(order, positions)
        if reason:
            order.status = STATUS_REJECTED
            order.reject_reason = reason
            order.updated_at = now
            self._event(order.order_id, "rejected", reason)
            return order

        # 3) 生效
        order.status = STATUS_PENDING
        order.updated_at = now
        self._pending[order.order_id] = order
        if client_id:
            self._client_ids[client_id] = order.order_id
        if req.oco_group_id:
            self._oco.setdefault(req.oco_group_id, []).append(order.order_id)
        self._event(order.order_id, "pending", "")
        return order

    # ------------------------------------------------------------------
    # 撤单 / 改单 / 恢复
    # ------------------------------------------------------------------
    def cancel(self, order_id: str, now: datetime) -> tuple[bool, str]:
        """撤单：成功返回 `(True, "")`；否则 `(False, 原因码)`。

        - 未知订单 → `ORDER_NOT_FOUND`
        - 已成交 → `ALREADY_FILLED`（幂等，不报错）
        - 已取消 → `ALREADY_CANCELLED`
        - 已过期/拒单 → `ALREADY_TERMINAL`
        """
        order = self._orders.get(order_id)
        if order is None:
            return False, "ORDER_NOT_FOUND"
        if order.status == STATUS_FILLED:
            return False, "ALREADY_FILLED"
        if order.status == STATUS_CANCELLED:
            return False, "ALREADY_CANCELLED"
        if order.status in (STATUS_EXPIRED, STATUS_REJECTED):
            return False, "ALREADY_TERMINAL"
        self._close_order(order, "USER_CANCELLED", now)
        return True, ""

    def modify(
        self,
        order_id: str,
        *,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        expires_at: datetime | None = None,
        now: datetime,
    ) -> tuple[bool, str]:
        """改单（仅 pending；改价/改有效期，version+1）：返回 `(ok, reason)`。"""
        order = self._orders.get(order_id)
        if order is None:
            return False, "ORDER_NOT_FOUND"
        if order.status != STATUS_PENDING:
            return False, "NOT_MODIFIABLE"
        req = order.request
        spec = self.pairs.get(req.pair)
        if spec is None:
            return False, "UNKNOWN_PAIR"
        if limit_price is not None:
            if req.order_type != ORDER_LIMIT:
                return False, "IMMUTABLE_FIELD"
            if not self._price_aligned(limit_price, spec):
                return False, "INVALID_PRICE_TICK"
            if _dec(limit_price) <= 0:
                return False, "INVALID_PRICE"
            req.limit_price = _dec(limit_price)
        if trigger_price is not None:
            if req.order_type not in (ORDER_STOP, ORDER_TRAILING):
                return False, "IMMUTABLE_FIELD"
            if not self._price_aligned(trigger_price, spec):
                return False, "INVALID_PRICE_TICK"
            if _dec(trigger_price) <= 0:
                return False, "INVALID_PRICE"
            req.trigger_price = _dec(trigger_price)
        if expires_at is not None:
            req.expires_at = expires_at
        order.version += 1
        order.updated_at = now
        self._event(order_id, "modified", f"version={order.version}")
        return True, ""

    def restore(self, orders: Sequence[Order]) -> None:
        """重启恢复：把 `ledger.load_orders_pending()` 的结果灌回内存。

        - 终态订单只入 `_orders`（供查询/幂等），不参与撮合
        - `created` 视为 `pending`（P0 无 IFD 子单；恢复时统一参与撮合）
        - 重建 `client_order_id` 幂等映射、OCO 分组、平仓取置（按 `position_id`）
        """
        for order in orders or []:
            self._orders[order.order_id] = order
            self._bump_order_seq(order.order_id)
            if order.request.client_order_id:
                self._client_ids[order.request.client_order_id] = order.order_id
            if order.status not in (STATUS_CREATED, STATUS_PENDING):
                continue
            order.status = STATUS_PENDING
            self._pending[order.order_id] = order
            req = order.request
            if req.oco_group_id:
                self._oco.setdefault(req.oco_group_id, []).append(order.order_id)
            if req.position_type == POS_CLOSE and req.position_id:
                self._close_target[order.order_id] = req.position_id
            self._event(order.order_id, "pending", "RESTORED")

    # ------------------------------------------------------------------
    # 撮合主流程
    # ------------------------------------------------------------------
    def process_tick(
        self,
        quotes: Mapping[str, QuoteSnapshot],
        now: datetime,
        account: AccountState,
        positions: Sequence[Position],
        rates: FxRates | None,
    ) -> FillResult:
        """处理一个行情 tick（只读 account/positions，返回 FillResult）。

        S1 平仓止损/追踪止损 → S2 平仓限价 → S3 维持率重算（<50% 取消新规挂单、
        跳过 S4/S5，`last_loss_cut_triggered=True`）→ S4 新规止损 → S5 新规限价；
        市价单在 S1 之前撮合。同一阶段按 `order_id` 升序，完全确定性。
        """
        quotes = dict(quotes or {})
        positions = list(positions or [])
        result = FillResult()
        self._tick_seq += 1
        self.last_loss_cut_triggered = False

        # 0) 到期（仅显式 expires_at；DAY 日切由 scheduler 负责）
        self._expire_due(now)

        # 1) 行情质量（每 pair 每 tick 只算一次；含乱序/重复快照守卫）
        pairs_in_play: set[str] = {o.request.pair for o in self._pending.values()}
        pairs_in_play.update(p.pair for p in positions)
        quality: dict[str, dict] = {}
        for pair in pairs_in_play:
            quality[pair] = self._classify(pair, quotes.get(pair), now)

        # 2) 本 tick 模拟状态（仅用于 S3 维持率重算）
        sim = _SimState(account, positions)
        leverage = int(self.cfg.account.leverage)

        # 3) 市价（提交后首 tick 立即撮合）
        for order in self._candidates((ORDER_MARKET,), None):
            self._process_order(order, quotes, quality, now, account, positions, rates, sim, result, leverage)

        # 4) S1 平仓止损 / 追踪止损
        for order in self._candidates((ORDER_STOP, ORDER_TRAILING), POS_CLOSE):
            self._process_order(order, quotes, quality, now, account, positions, rates, sim, result, leverage)

        # 5) S2 平仓限价
        for order in self._candidates((ORDER_LIMIT,), POS_CLOSE):
            self._process_order(order, quotes, quality, now, account, positions, rates, sim, result, leverage)

        # 6) S3 维持率重算（S1/S2 成交后；仍 < loss_cut_ratio 则取消剩余新规挂单）
        if sim.positions and self._loss_cut_breached(sim, quotes, rates):
            self.last_loss_cut_triggered = True
            self._cancel_new_orders("RISK_CANCELLED", now)
            return result

        # 7) S4 新規止损
        for order in self._candidates((ORDER_STOP, ORDER_TRAILING), POS_OPEN):
            self._process_order(order, quotes, quality, now, account, positions, rates, sim, result, leverage)

        # 8) S5 新規限价
        for order in self._candidates((ORDER_LIMIT,), POS_OPEN):
            self._process_order(order, quotes, quality, now, account, positions, rates, sim, result, leverage)
        return result

    # ------------------------------------------------------------------
    # 内部：校验
    # ------------------------------------------------------------------
    def _validate_create(
        self,
        order: Order,
        quotes: Mapping[str, QuoteSnapshot],
        now: datetime,
        account: AccountState,
        positions: Sequence[Position],
        rates: FxRates | None,
    ) -> str:
        """建单校验：返回原因码（"" 表示通过）。顺序固定，便于断言。"""
        req = order.request
        spec = self.pairs.get(req.pair)
        if spec is None:
            return "UNKNOWN_PAIR"
        if req.side not in (SIDE_BUY, "sell"):
            return "INVALID_SIDE"
        if req.position_type not in (POS_OPEN, POS_CLOSE):
            return "INVALID_POSITION_TYPE"
        if req.order_type not in (ORDER_MARKET, ORDER_LIMIT, ORDER_STOP, ORDER_TRAILING):
            return "INVALID_ORDER_TYPE"
        cfg_orders = self.cfg.orders
        if req.order_type == ORDER_LIMIT and not bool(cfg_orders.allow_limit):
            return "ORDER_TYPE_DISABLED"
        if req.order_type == ORDER_STOP and not bool(cfg_orders.allow_stop):
            return "ORDER_TYPE_DISABLED"
        if req.order_type == ORDER_TRAILING and not bool(getattr(cfg_orders, "allow_trailing", False)):
            return "ORDER_TYPE_DISABLED"

        # 数量：1,000 整数倍、≥ min_units、≤ 单品种上限
        units = req.units
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            return "INVALID_UNITS"
        if units % UNITS_STEP != 0:
            return "INVALID_UNITS"
        min_units = int(getattr(self.cfg.account, "min_units", UNITS_STEP))
        if units < min_units:
            return "INVALID_UNITS"
        if spec.max_units and units > int(spec.max_units):
            return "INVALID_UNITS"

        # 价格：限价/止损必填且 tick 对齐
        if req.order_type == ORDER_LIMIT:
            if req.limit_price is None:
                return "INVALID_LIMIT_PRICE"
            if not self._price_aligned(req.limit_price, spec) or _dec(req.limit_price) <= 0:
                return "INVALID_PRICE_TICK"
        if req.order_type in (ORDER_STOP, ORDER_TRAILING):
            if req.trigger_price is None:
                return "INVALID_TRIGGER_PRICE"
            if not self._price_aligned(req.trigger_price, spec) or _dec(req.trigger_price) <= 0:
                return "INVALID_PRICE_TICK"

        # 行情：存在 + 新鲜（新单口径：market.stale_quote_seconds）
        quote = quotes.get(req.pair)
        if quote is None or quote.bid is None or quote.ask is None:
            return "QUOTE_MISSING"
        if quote.bid <= 0 or quote.ask <= 0:
            return "LIQUIDITY_ANOMALY"
        if quote.ask < quote.bid:
            return "LIQUIDITY_ANOMALY"
        stale_order_seconds = int(getattr(self.cfg.market, "stale_quote_seconds", 60))
        if _quote_age_seconds(quote, now) > stale_order_seconds:
            return "QUOTE_STALE"
        # 汇率锚：账户币种是元，缺 USDCNY/USDJPY 时一律拒绝开仓（不得用假汇率）
        if rates is None:
            return RATE_UNAVAILABLE

        # 价格偏离保护（防手滑/错误单位）：限价/止损距 mid 超过 20%
        mid = quote.mid if quote.mid is not None else (quote.bid + quote.ask) / Decimal(2)
        if mid > 0:
            for candidate in (req.limit_price, req.trigger_price):
                if candidate is None:
                    continue
                deviation = abs(_dec(candidate) - mid) / mid
                if deviation > PRICE_DEVIATION_LIMIT_DEFAULT:
                    return "PRICE_OUT_OF_RANGE"

        # 新规：点差（市价/止损拒绝；限价接受）+ 综合风控
        if req.position_type == POS_OPEN:
            spread = quote.spread_pips(spec)
            if req.order_type in (ORDER_MARKET, ORDER_STOP, ORDER_TRAILING):
                if spread is not None and spread > spec.max_spread_pips:
                    return "SPREAD_TOO_WIDE"
            if len(self._pending) >= int(cfg_orders.max_pending):
                return "MAX_OPEN_ORDERS"
            equity = self._safe_equity(account, positions, quotes, rates)
            ok, reason = risk.check_open(
                req,
                spec,
                account,
                positions,
                quotes,
                rates,
                self.cfg,
                now,
                risk.daily_loss_hit(account, equity, self.cfg),
                risk.drawdown_hit(account, equity, self.cfg),
                pairs=self.pairs,
            )
            if not ok:
                return reason
            return ""

        # 平仓：解析目标持仓并冻结 position_id（FIFO 或显式）
        target, reason = self._resolve_close_target(req, positions)
        if target is None:
            return reason
        self._close_target[order.order_id] = target.position_id
        return ""

    def _validate_oco(self, order: Order, positions: Sequence[Position]) -> str:
        """OCO 组一致性：同货币对/同方向/同 position_type/同 units/同目标持仓，最多 2 腿。

        冲突时把已有的腿一并置 rejected（`OCO_CONFLICT`），避免半组生效。
        """
        group = order.request.oco_group_id or ""
        legs = [self._pending[oid] for oid in self._oco.get(group, []) if oid in self._pending]
        conflict = len(legs) >= 2
        new_target = self._close_target.get(order.order_id)
        for leg in legs:
            lreq = leg.request
            if (
                lreq.pair != order.request.pair
                or lreq.side != order.request.side
                or lreq.position_type != order.request.position_type
                or lreq.units != order.request.units
            ):
                conflict = True
            if order.request.position_type == POS_CLOSE and self._close_target.get(leg.order_id) != new_target:
                conflict = True
        if not conflict:
            return ""
        order.status = STATUS_REJECTED
        order.reject_reason = "OCO_CONFLICT"
        self._close_target.pop(order.order_id, None)
        self._event(order.order_id, "rejected", "OCO_CONFLICT")
        for leg in legs:
            leg.status = STATUS_REJECTED
            leg.reject_reason = "OCO_CONFLICT"
            self._pending.pop(leg.order_id, None)
            self._release_reservation(leg)
            self._event(leg.order_id, "rejected", "OCO_CONFLICT")
        return "OCO_CONFLICT"

    def _resolve_close_target(self, req: OrderRequest, positions: Sequence[Position]) -> tuple[Position | None, str]:
        """解析平仓目标持仓：显式 position_id 优先，否则同货币对反向 FIFO；校验可平数量（含取置）。

        取置口径：同货币对同方向的其它 pending 平仓单会占用可平 units；但**同一 OCO 组的
        两条腿互斥**（一条成交另一条取消），因此同组其它腿不重复占用。
        """
        target_side = SIDE_BUY if req.side != SIDE_BUY else "sell"
        candidates = [p for p in positions if p.pair == req.pair and p.side == target_side]
        if req.position_id:
            candidates = [p for p in candidates if p.position_id == req.position_id]
            if not candidates:
                return None, "POSITION_NOT_FOUND"
        if not candidates:
            return None, "POSITION_NOT_FOUND"
        candidates.sort(key=lambda p: (p.opened_at or datetime.min, p.position_id))
        for pos in candidates:
            available = int(pos.units) - self._reserved_units_for(pos.position_id, req)
            if int(req.units) <= available:
                return pos, ""
        return None, "CLOSE_UNITS_EXCEED"

    def _reserved_units_for(self, position_id: str, req: OrderRequest) -> int:
        """目标持仓上已被其它 pending 平仓单占用的 units（同 OCO 组互斥腿不占用）。"""
        reserved = 0
        oco_group = req.oco_group_id or ""
        for oid, pid in self._close_target.items():
            if pid != position_id:
                continue
            order = self._orders.get(oid)
            if order is None or order.status != STATUS_PENDING:
                continue
            if oco_group and order.request.oco_group_id == oco_group:
                continue  # 同 OCO 组：互斥，不重复占用
            reserved += int(order.request.units)
        return reserved

    @staticmethod
    def _price_aligned(price: Decimal | None, spec: PairSpec) -> bool:
        """价格是否按 tick 对齐（`price % tick == 0`）。"""
        if price is None:
            return False
        try:
            value = _dec(price)
            return value % spec.tick == 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 内部：单订单撮合
    # ------------------------------------------------------------------
    def _process_order(
        self,
        order: Order,
        quotes: Mapping[str, QuoteSnapshot],
        quality: Mapping[str, dict],
        now: datetime,
        account: AccountState,
        positions: Sequence[Position],
        rates: FxRates | None,
        sim: _SimState,
        result: FillResult,
        leverage: int,
    ) -> None:
        """评估并撮合一条挂单（可能成交/挂起/取消/拒单）。"""
        if order.status != STATUS_PENDING:
            return
        req = order.request
        spec = self.pairs.get(req.pair)
        if spec is None:
            self._reject_order(order, "UNKNOWN_PAIR", now, result)
            return
        quote = quotes.get(req.pair)
        info = quality.get(req.pair) or self._classify(req.pair, quote, now)
        if info.get("duplicate"):
            return  # 乱序/重复快照：丢弃本 tick
        if quote is None or quote.bid is None or quote.ask is None:
            if req.order_type == ORDER_MARKET:
                self._reject_order(order, info.get("reason") or "QUOTE_MISSING", now, result)
            else:
                self._suspend_order(order, info.get("reason") or "QUOTE_MISSING", result)
            return

        # ---- 行情可用性 / 点差（§4.5 行为矩阵） ----
        reason = str(info.get("reason") or "")
        wide = bool(info.get("wide"))
        if self._usable_fn is not None:
            ok, custom_reason = self._usable_fn(spec, quote, now, self.cfg)
            if not ok and custom_reason:
                reason = custom_reason
        if req.order_type == ORDER_MARKET:
            if reason:
                self._reject_order(order, reason, now, result)
                return
            if wide and req.position_type == POS_OPEN:
                self._reject_order(order, "SPREAD_TOO_WIDE", now, result)
                return
        elif req.position_type == POS_CLOSE:
            # 风险单（止损/止盈/平仓）点差宽也允许成交，但行情不可用则挂起
            if reason:
                self._suspend_order(order, reason, result)
                return
        else:  # open 的限价/止损
            if reason:
                self._suspend_order(order, reason, result)
                return
            if wide and req.order_type in (ORDER_STOP, ORDER_TRAILING):
                self._suspend_order(order, "SPREAD_TOO_WIDE", result)
                return

        # 恢复：挂起原因消失后解除（事件只写一次）
        if order.suspend_reason:
            order.suspend_reason = ""
            self._event(order.order_id, "resumed", "")

        # ---- 触发判定 ----
        buy = req.side == SIDE_BUY
        if buy:
            exec_base = _dec(quote.ask)
            judge_price = exec_base
        else:
            exec_base = _dec(quote.bid)
            judge_price = exec_base
        if req.order_type == ORDER_LIMIT:
            limit = _dec(req.limit_price)  # type: ignore[arg-type]
            triggered = judge_price <= limit if buy else judge_price >= limit
            if not triggered:
                return
        elif req.order_type in (ORDER_STOP, ORDER_TRAILING):
            trigger = _dec(req.trigger_price)  # type: ignore[arg-type]
            triggered = judge_price >= trigger if buy else judge_price <= trigger
            if not triggered:
                return
        else:  # market
            triggered = True

        # ---- 平仓目标（可能被同 tick 先前成交消耗） ----
        target_pos: Position | None = None
        if req.position_type == POS_CLOSE:
            pid = self._close_target.get(order.order_id)
            if pid is None:
                target, reason2 = self._resolve_close_target(req, positions)
                if target is None:
                    reason2 = reason2 if reason2 in ("POSITION_NOT_FOUND", "CLOSE_UNITS_EXCEED") else "POSITION_NOT_FOUND"
                    self._cancel_order(order, reason2, now, result)
                    return
                pid = target.position_id
                self._close_target[order.order_id] = pid
            target_pos = sim.find(pid) or next((p for p in positions if p.position_id == pid), None)
            if target_pos is None:
                self._cancel_order(order, "POSITION_NOT_FOUND", now, result)
                return
            # 本 tick 已被其他成交消耗的部分也要扣除
            already_closed = int(target_pos.units)
            if int(req.units) > already_closed:
                self._cancel_order(order, "CLOSE_UNITS_EXCEED", now, result)
                return

        # ---- 成交价 ----
        gap_pips = Decimal("0")
        slippage_pips = Decimal("0")
        if req.order_type == ORDER_LIMIT:
            fill_price = _align_nearest(exec_base, spec.tick)  # 限价不加滑点；不劣于限价由触发条件保证
        else:
            slippage_pips = _dec(self._slippage_fn(req.order_type, spec))
            if slippage_pips < 0:
                slippage_pips = Decimal("0")  # 只允许逆向滑点
            if req.order_type == ORDER_MARKET:
                max_slip = _dec(getattr(self.cfg.spread, "market_max_slip_pips", 3.0))
                if slippage_pips > max_slip:
                    self._reject_order(order, "SLIPPAGE_TOO_LARGE", now, result)
                    return
            slip_price = slippage_pips * spec.pip
            raw_price = exec_base + slip_price if buy else exec_base - slip_price
            fill_price = _align_unfavorable(raw_price, spec.tick, req.side)
            if req.order_type in (ORDER_STOP, ORDER_TRAILING):
                trigger = _dec(req.trigger_price)  # type: ignore[arg-type]
                raw_gap = (exec_base - trigger) / spec.pip if buy else (trigger - exec_base) / spec.pip
                gap_pips = raw_gap.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if raw_gap > 0 else Decimal("0.00")

        # ---- 元損益（平仓单）----
        realized_pnl = 0
        position_id = f"POS-{order.order_id}"
        if req.position_type == POS_CLOSE and target_pos is not None:
            position_id = target_pos.position_id
            # 平仓损益按**本单平掉的 units** 计算（部分平仓不是部分成交）
            closed_view = replace(target_pos, units=req.units)
            realized_pnl = accounting.close_pnl(closed_view, fill_price, spec, rates)
        fee = accounting.commission(req.units, getattr(self.cfg.account, "commission_per_10k", 0.0))

        fill = Fill(
            order_id=order.order_id,
            pair=req.pair,
            side=req.side,
            position_type=req.position_type,
            units=req.units,
            price=fill_price,
            slippage_pips=slippage_pips,
            fee=fee,
            realized_pnl=realized_pnl,
            position_id=position_id,
            ts=now,
            decision_id=req.decision_id,
        )

        # ---- 落状态：triggered → filled（同 tick 原子完成） ----
        order.status = STATUS_FILLED
        order.filled_price = fill_price
        order.filled_units = req.units
        order.slippage_pips = slippage_pips
        order.gap_pips = gap_pips
        order.trade_id = order.order_id
        order.updated_at = now
        order.version += 1
        self._pending.pop(order.order_id, None)
        self._release_reservation(order)
        self._event(order.order_id, "triggered", f"tick={self._tick_seq}")
        self._event(order.order_id, "filled", f"price={fill_price}")
        result.fills.append(fill)

        # 模拟账务（仅 S3 用）+ OCO 互斥
        sim.apply_fill(fill, spec, leverage, rates)
        self._cancel_oco_siblings(order, now)

    def _cancel_oco_siblings(self, filled_order: Order, now: datetime) -> None:
        """OCO：一条成交 → 同组其余腿 `cancelled(OCO_CANCELLED)`。"""
        group = filled_order.request.oco_group_id
        if not group:
            return
        for oid in list(self._oco.get(group, [])):
            if oid == filled_order.order_id:
                continue
            sibling = self._pending.get(oid)
            if sibling is None:
                continue
            self._cancel_order(sibling, "OCO_CANCELLED", now, result=None)

    # ------------------------------------------------------------------
    # 内部：状态迁移工具
    # ------------------------------------------------------------------
    def _suspend_order(self, order: Order, reason: str, result: FillResult) -> None:
        """挂起订单（粘性，只在首次/原因变化时写事件与返回值）。"""
        reason = reason or "QUOTE_STALE"
        if order.suspend_reason == reason:
            return
        order.suspend_reason = reason
        self._event(order.order_id, "suspended", reason)
        result.suspended.append((order.order_id, reason))

    def _reject_order(self, order: Order, reason: str, now: datetime, result: FillResult | None, ) -> None:
        """把挂单置 rejected（不影响 account/positions）。"""
        order.status = STATUS_REJECTED
        order.reject_reason = reason
        order.updated_at = now
        self._pending.pop(order.order_id, None)
        self._release_reservation(order)
        self._event(order.order_id, "rejected", reason)
        if result is not None:
            result.rejected.append((order.order_id, reason))

    def _cancel_order(self, order: Order, reason: str, now: datetime, result: FillResult | None) -> None:
        """把挂单置 cancelled（用户/OCO/风控/目标失效）。"""
        order.status = STATUS_CANCELLED
        order.reject_reason = ""
        order.suspend_reason = ""
        order.updated_at = now
        self._pending.pop(order.order_id, None)
        self._release_reservation(order)
        self._event(order.order_id, "cancelled", reason)
        if result is not None:
            result.rejected.append((order.order_id, reason))

    def _close_order(self, order: Order, reason: str, now: datetime) -> None:
        """cancel() 用的内部终止（cancelled）。"""
        self._cancel_order(order, reason, now, None)

    def _release_reservation(self, order: Order) -> None:
        """释放平仓单对目标持仓的取置（取置由 pending 平仓单实时扫描得出，这里只需摘除目标）。"""
        self._close_target.pop(order.order_id, None)

    def _expire_due(self, now: datetime) -> None:
        """到期处理：仅处理显式 `expires_at`（GTD 语义）。"""
        for order in list(self._pending.values()):
            expires_at = order.request.expires_at
            if expires_at is None:
                continue
            try:
                if now < expires_at:
                    continue
            except TypeError:
                continue
            order.status = STATUS_EXPIRED
            order.updated_at = now
            self._pending.pop(order.order_id, None)
            self._release_reservation(order)
            self._event(order.order_id, "expired", "GTD_EXPIRED")

    def _cancel_new_orders(self, reason: str, now: datetime) -> None:
        """S3：取消本 tick 剩余的全部新规挂单（市价已撮合，不影响）。"""
        for order in list(self._pending.values()):
            if order.request.position_type == POS_OPEN:
                self._cancel_order(order, reason, now, None)

    def _loss_cut_breached(
        self, sim: _SimState, quotes: Mapping[str, QuoteSnapshot], rates: FxRates | None
    ) -> bool:
        """重算模拟账户净值/所需保证金，返回是否跌破 loss_cut_ratio（整数严格比较）。

        缺汇率时无法估值 → 返回 ``False``（不误判强平；调度层会拒绝新开仓并挂起估值）。
        """
        if rates is None:
            return False
        pairs_view = dict(self.pairs)
        account_view = SimpleNamespace(balance=sim.balance)
        margin_total = accounting.required_margin_total(
            sim.positions, quotes, pairs_view, rates, int(self.cfg.account.leverage)
        )
        if margin_total <= 0:
            return False
        equity = sim.balance
        for pos in sim.positions:
            spec = pairs_view.get(pos.pair)
            if spec is None:
                continue
            equity += accounting.unrealized_pnl(pos, quotes.get(pos.pair), spec, rates)
        return accounting.loss_cut_triggered(equity, margin_total, self.cfg.account.loss_cut_ratio)

    # ------------------------------------------------------------------
    # 内部：行情分类 / 工具
    # ------------------------------------------------------------------
    def _classify(self, pair: str, quote: QuoteSnapshot | None, now: datetime) -> dict:
        """行情质量分类：`{reason, wide, duplicate}`。

        - `duplicate=True`：乱序/重复快照（quote_ts 不递增）→ 本 tick 该货币对完全不处理
        - `reason`："" / QUOTE_MISSING / LIQUIDITY_ANOMALY / QUOTE_STALE
        - `wide=True`：点差超过品种上限（平仓单仍允许成交，新规止损挂起）
        """
        spec = self.pairs.get(pair)
        info = {"reason": "", "wide": False, "duplicate": False}
        if quote is None or quote.bid is None or quote.ask is None:
            info["reason"] = "QUOTE_MISSING"
            return info
        quote_seq = int(getattr(quote, "seq", 0) or 0)
        last_seq = self._last_quote_seq.get(pair)
        quote_ts = quote.quote_ts
        last_ts = self._last_quote_ts.get(pair)
        # 去重规则：仅当"时间戳不变或倒退"且"seq 未严格前进"时判为重复。
        # - 真实轮询：seq 单调递增，同一秒内时间戳不变的新快照仍应处理；
        # - 外部注入/测试：seq 可能恒为 0 或相同，此时退回按行情时间戳去重。
        if quote_ts is not None and last_ts is not None and quote_ts <= last_ts:
            if quote_seq <= 0 or (last_seq is not None and quote_seq <= last_seq):
                info["duplicate"] = True
                return info
        if quote_seq > 0:
            self._last_quote_seq[pair] = max(quote_seq, last_seq or 0)
        if quote_ts is not None:
            self._last_quote_ts[pair] = quote_ts if last_ts is None else max(quote_ts, last_ts)
        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            info["reason"] = "LIQUIDITY_ANOMALY"
            return info
        stale_seconds = int(getattr(self.cfg.orders, "quote_stale_fill_seconds", MAX_QUOTE_AGE_FILL_SECONDS_DEFAULT))
        if _quote_age_seconds(quote, now) > stale_seconds:
            info["reason"] = "QUOTE_STALE"
            return info
        if spec is not None:
            spread = quote.spread_pips(spec)
            if spread is not None and spread > spec.max_spread_pips:
                info["wide"] = True
        return info

    def _candidates(self, order_types: tuple[str, ...], position_type: str | None) -> list[Order]:
        """候选列表快照：pending + 指定 order_type/position_type，按 order_id 升序。"""
        out = [
            o
            for o in self._pending.values()
            if o.request.order_type in order_types
            and (position_type is None or o.request.position_type == position_type)
        ]
        out.sort(key=lambda o: o.order_id)
        return out

    def _safe_equity(
        self,
        account: AccountState,
        positions: Sequence[Position],
        quotes: Mapping[str, QuoteSnapshot],
        rates: FxRates | None,
    ) -> int:
        """估值失败时退化为 balance，避免风控因数据问题误判/崩溃。"""
        try:
            return accounting.equity(account, positions, quotes, self.pairs, rates)
        except (KeyError, ValueError):
            return int(account.balance)

    def _alloc_order_id(self) -> str:
        """生成单调订单 ID：`ORD-000001`（同阶段升序 = 创建顺序）。"""
        while True:
            order_id = f"ORD-{self._order_seq:06d}"
            self._order_seq += 1
            if order_id not in self._orders and order_id not in self._pending:
                return order_id

    def _bump_order_seq(self, order_id: str) -> None:
        """恢复时推进序列号，避免新单与历史 ID 冲突。"""
        if not order_id.startswith("ORD-"):
            return
        try:
            seq = int(order_id[4:])
        except ValueError:
            return
        if seq >= self._order_seq:
            self._order_seq = seq + 1

    def _event(self, order_id: str, event_type: str, detail: str = "") -> None:
        """写一条内存审计事件（scheduler 用 drain_events 落 order_events）。"""
        self.events.append((order_id, event_type, detail))
