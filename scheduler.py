"""交易编排器：行情轮询 → 订单撮合 → 账本落库 → 日切隔夜利息 → 强平 → LLM 决策。

设计要点（Rev 1.2，账户币种=虚拟人民币/元）：
- 每 tick：拉行情（含 USDCNY 换算锚）→ 刷新财经日历（auto/static）→ 日切 → 订单引擎按
  S1/S2 平仓 → S3 重算维持率（仍<50% 才一次性全部平仓）→ S4/S5 新规；
- 金额一律按 `USDJPY`/`USDCNY` 换算成元；缺汇率时拒绝开仓、挂起估值并播报一次
  （原因码 `RATE_UNAVAILABLE`），绝不用假汇率；
- 日切（纽约 17:00）：先补隔夜利息入账 → 快照 → 日切计数 → 再检查强平；
- LLM 决策只在决策窗口内、且带漂移校验；失败 hold，不伪造交易；
- 决策上下文只包含「宿主人设 + 群聊参考材料 + 财经日历」三类软信息，
  全部由插件注入、失败静默降级；风险偏好/交易风格不写死，由麦麦本人的人设 + LLM 判断；
- 宿主人设是**一段字符串**：插件 on_load 时读一次，这里只缓存并原样传给决策；
- 通知（events）由插件层 drain 后发送，编排器本身不直接碰 ctx.send。
"""

from __future__ import annotations

import asyncio
import copy
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from . import (
    accounting,
    calendar_source,
    decision,
    econ_calendar as econ_calendar_mod,
    fxconv,
    influence as influence_mod,
    market_clock,
    pairs as pairs_mod,
    report,
    risk,
)
from .execution_cost import slippage_pips
from .fxconv import FxRates
from .ledger import DuplicateTradeError
from .models import (
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    POS_CLOSE,
    POS_OPEN,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_FILLED,
    STATUS_REJECTED,
    AccountState,
    DecisionIntent,
    Fill,
    Order,
    OrderRequest,
    Position,
    QuoteSnapshot,
)
from .orders import STAGE_OPEN, STAGE_RISK, OrderEngine

CST = ZoneInfo("Asia/Shanghai")
MAX_ROLLOVER_CATCHUP_DAYS = 10


class TradingEngine:
    """单账户交易编排器（P0：单实例、单 asyncio 循环）。"""

    def __init__(
        self,
        *,
        ctx: Any,
        config: Any,
        quote_source: Any,
        ledger: Any,
        log: Callable[[str], None] | None = None,
        persona: str = "",
        chat_context_fetcher: Callable[[datetime], Any] | None = None,
        calendar_fetcher: Callable[[int], Any] | None = None,
    ) -> None:
        self.ctx = ctx
        self.config = config
        self.source = quote_source
        self.ledger = ledger
        self._log = log or (lambda msg: None)

        self.pairs: dict[str, Any] = pairs_mod.pairs_from_config(getattr(config.market, "pairs", []) or [])
        self.account: AccountState = ledger.load_account()
        self.positions: list[Position] = ledger.load_positions()

        # 宿主人设：插件 on_load 时读取一次后注入（这里只缓存；不定义、不派生任何性格/风控参数）
        self.persona: str = str(persona or "").strip()
        # 财经日历：静态事件表 + 真实数据源（[calendar] source=auto 时联网，失败回退静态）
        self.calendar: econ_calendar_mod.EconomicCalendar = econ_calendar_mod.EconomicCalendar.from_config(
            config
        )
        for error in self.calendar.errors:
            self._log(f"财经日历：{error}")
        self._static_events: list[econ_calendar_mod.CalendarEvent] = list(self.calendar.events)
        self.calendar_feed: calendar_source.CalendarFeed = calendar_source.CalendarFeed(
            fetcher=calendar_fetcher,
            refresh_minutes=int(getattr(getattr(config, "calendar", None), "refresh_minutes", 30) or 30),
        )
        self._calendar_warned: bool = False
        self.calendar_status: str = ""

        self.engine = OrderEngine(self.pairs, config, open_block_fn=self._open_block_reason)
        self.engine.restore(ledger.load_orders_pending())
        self._persisted_orders: dict[str, tuple[int, str]] = {
            order.order_id: (int(order.version), str(order.status))
            for order in self.engine.all_orders()
        }
        #: 订单事件/账户落盘去抖
        self._account_dirty: bool = True
        #: 人工重置/账户重建的版本号（旧决策执行前必须比对，防止把重置前的意图打进来）
        self._ops_version: int = 0

        # 群聊上下文：默认由插件注入读取器（走宿主 message.get_recent）
        self.chat_context_fetcher: Callable[[datetime], Any] | None = chat_context_fetcher
        self.chat_context_chat_ids: list[str] = []
        self.last_chat_context: Any = influence_mod.empty_context()
        self._announced_events: set[str] = set()

        self.quotes: dict[str, QuoteSnapshot] = {}
        #: 本 tick 通过「源时间/接收时间/价格/顺序」校验的快照（估值/强平/换算只用它）
        self.valid_quotes: dict[str, QuoteSnapshot] = {}
        self.quote_issues: dict[str, str] = {}
        self._last_source_ts: dict[str, datetime] = {}
        self._quote_guard_key: tuple[tuple[str, str], ...] = ()
        self._quote_guard_warned: bool = False
        self.seq: int = 0
        self.paused: bool = bool(ledger.load_meta("paused") == "1")
        self.events: list[str] = []

        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._last_quotes_at: datetime | None = None
        self._rate_warned: bool = False
        self._last_rollover_date: str = ledger.load_meta("last_rollover") or ""
        self._last_decision_at: datetime | None = None
        self._decision_task: asyncio.Task[None] | None = None
        self._budget: decision.CallBudget | None = None
        self._budget_day: str = ""
        self.last_intent: DecisionIntent | None = None
        self.last_dropped_decision: str = ""
        self.last_error: str = ""

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="mai-fx-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._decision_task is not None:
            self._decision_task.cancel()
            self._decision_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self._log(f"交易循环异常：{exc}")
            base = max(3, int(getattr(self.config.market, "quote_poll_seconds", 15)))
            jitter = random.uniform(
                -float(getattr(self.config.market, "quote_jitter_ratio", 0.15)),
                float(getattr(self.config.market, "quote_jitter_ratio", 0.15)),
            )
            try:
                await asyncio.sleep(max(1.0, base * (1 + jitter)))
            except asyncio.CancelledError:
                raise

    # ---------- 事件 ----------

    def emit(self, text: str) -> None:
        if text:
            self.events.append(text)

    def drain_events(self) -> list[str]:
        events = self.events
        self.events = []
        return events

    # ---------- 估值 ----------

    def _rates(self) -> FxRates | None:
        """**只用本 tick 通过校验的** USDJPY + USDCNY；缺失返回 None（不用旧汇率）。

        旧汇率在行情源断线/源时间倒退时会掩盖问题（估值/保证金都会失真），因此缺锚一律
        挂起估值、拒绝开仓（原因码 ``RATE_UNAVAILABLE``），等两条锚点都回来再继续。
        """

        return fxconv.rates_from_quotes(self.valid_quotes)

    def equity_cny(self) -> int | None:
        rates = self._rates()
        if rates is None:
            return None
        for position in self.positions:
            if position.pair not in self.valid_quotes:
                return None  # 部分持仓缺新鲜报价：挂起估值，不用旧价冒充
        return accounting.equity(self.account, self.positions, self.valid_quotes, self.pairs, rates)

    def margin_total_cny(self) -> int | None:
        rates = self._rates()
        if rates is None:
            return None
        for position in self.positions:
            if position.pair not in self.valid_quotes:
                return None
        return accounting.required_margin_total(
            self.positions, self.valid_quotes, self.pairs, rates, int(self.config.account.leverage)
        )

    def maintenance_ratio(self) -> Decimal | None:
        equity = self.equity_cny()
        margin = self.margin_total_cny()
        if equity is None or margin is None:
            return None
        return accounting.maintenance_ratio(equity, margin)

    def _handle_rate_unavailable(self) -> None:
        """汇率缺失：本轮不估值、不撮合、不开仓；同一段缺失只播报并记录一次日志。"""

        missing = [
            code
            for code in ("USDJPY", fxconv.USDCNY_CODE)
            if code not in self.valid_quotes
        ]
        detail = "、".join(missing) if missing else "USDJPY/USDCNY"
        self.last_error = (
            f"{fxconv.RATE_UNAVAILABLE}：缺少 {detail} 汇率，"
            "账户估值与开仓已挂起（不会用假汇率）"
        )
        if not self._rate_warned:
            self._rate_warned = True
            self._log(self.last_error)
            self.emit(report.rate_unavailable_text(missing))

    # ---------- 行情校验（源时间 / 接收时间 / 价格 / 顺序） ----------

    def _validate_quotes(self, now: datetime, quotes: dict[str, QuoteSnapshot]) -> dict[str, QuoteSnapshot]:
        """逐条校验本 tick 行情：**旧数据绝不参与成交/强平/估值**。

        校验项：价格存在且为正、ask ≥ bid、源时间 ``quote_ts`` 存在且不倒退、源时间在
        ``market.stale_quote_seconds`` 内、接收时间 ``recv_ts`` 在
        ``orders.quote_stale_fill_seconds`` 内。通过者进入 ``self.valid_quotes``；
        不通过者记录原因并在 `self.quote_issues` 里播报缺哪个品种/锚。
        """

        issues: dict[str, str] = {}
        valid: dict[str, QuoteSnapshot] = {}
        # 只校验「可交易品种 + 换算锚」：人民币参考对只展示，缺了不挂起交易。
        wanted: list[str] = list(self.pairs)
        if fxconv.USDCNY_CODE not in wanted:
            wanted.append(fxconv.USDCNY_CODE)
        for code in wanted:
            quote = (quotes or {}).get(code)
            reason = self._quote_issue(str(code), quote, now)
            if reason:
                issues[str(code)] = reason
            else:
                valid[str(code)] = quote  # type: ignore[assignment]
        self.quote_issues = issues
        self.valid_quotes = valid
        self._warn_quote_issues()
        return valid

    def _quote_issue(self, code: str, quote: QuoteSnapshot | None, now: datetime) -> str:
        """单条行情校验；返回原因码（``""`` = 可用）。"""

        if quote is None or quote.bid is None or quote.ask is None:
            return "QUOTE_MISSING"
        if quote.bid <= 0 or quote.ask <= 0:
            return "QUOTE_MISSING"
        if quote.ask < quote.bid:
            return "LIQUIDITY_ANOMALY"
        quote_ts = quote.quote_ts
        if quote_ts is None or quote_ts.tzinfo is None:
            return "QUOTE_STALE"
        last_ts = self._last_source_ts.get(code)
        if last_ts is not None and quote_ts < last_ts:
            return "OUT_OF_ORDER"
        try:
            source_age = (now - quote_ts).total_seconds()
        except TypeError:
            return "QUOTE_STALE"
        if source_age > int(getattr(self.config.market, "stale_quote_seconds", 60)):
            return "QUOTE_STALE"
        recv = quote.recv_ts
        if recv is None or recv.tzinfo is None:
            return "STALE_QUOTE_FILL"
        try:
            recv_age = (now - recv).total_seconds()
        except TypeError:
            return "STALE_QUOTE_FILL"
        if recv_age > int(getattr(self.config.orders, "quote_stale_fill_seconds", 5)):
            return "STALE_QUOTE_FILL"
        self._last_source_ts[code] = quote_ts
        return ""

    def _warn_quote_issues(self) -> None:
        """行情不完整/过期时播报一次（同一组问题只播一次，恢复后重置）并挂起依赖项。"""

        key = tuple(sorted(self.quote_issues.items()))
        if not key:
            self._quote_guard_key = ()
            self._quote_guard_warned = False
            return
        if key == self._quote_guard_key:
            return
        self._quote_guard_key = key
        self._quote_guard_warned = True
        self.last_error = "QUOTE_GUARD：" + "、".join(f"{code}（{reason}）" for code, reason in key)
        self._log(self.last_error)
        self.emit(report.quote_guard_text(dict(self.quote_issues)))

    # ---------- 主 tick ----------

    async def tick_once(self) -> None:
        decision_ctx: tuple[datetime, FxRates] | None = None
        async with self._lock:
            now = market_clock.now_utc()
            codes = list(self.pairs)
            try:
                quotes = await self.source.fetch(codes, now, self.seq + 1)
            except Exception as exc:  # noqa: BLE001 - 行情失败：本轮不交易
                self.last_error = f"行情获取失败：{exc}"
                self._log(self.last_error)
                return
            self.seq += 1
            self.quotes = dict(quotes)
            self._last_quotes_at = now
            self._validate_quotes(now, self.quotes)
            await self.refresh_calendar(now)
            self._rollover_if_due(now)
            if market_clock.market_status(now) == "open":
                rates = self._rates()
                if rates is None:
                    self._handle_rate_unavailable()
                else:
                    self._rate_warned = False
                # 风控/保护单/强平始终运行（暂停只停「新开仓」与自动决策）
                self._process_orders(now, rates, stage=STAGE_RISK)
                if rates is not None:
                    self._check_loss_cut(now, rates)
                if not self.paused:
                    self._process_orders(now, rates, stage=STAGE_OPEN)
                    self._announce_upcoming(now)
                self._flush_order_events()
                self._persist_orders()
                if self._account_dirty:
                    self.ledger.save_account(self.account)
                    self._account_dirty = False
                if not self.paused and rates is not None:
                    decision_ctx = (now, rates)
            else:
                self._flush_order_events()
                self._persist_orders()
        if decision_ctx is not None:
            self._decision_cycle(*decision_ctx)

    # ---------- 订单处理 ----------

    def _engine_quotes(self) -> dict[str, QuoteSnapshot]:
        """交给订单引擎的行情：只含本 tick 通过校验的可交易品种（防止旧价成交）。"""

        return {code: self.valid_quotes[code] for code in self.pairs if code in self.valid_quotes}

    def _process_orders(self, now: datetime, rates: FxRates | None, *, stage: str) -> None:
        result = self.engine.process_tick(
            self._engine_quotes(), now, self.account, self.positions, rates, stage=stage
        )
        self._apply_fills(result.fills, now)
        for order_id, reason in result.rejected:
            self.emit(report.order_rejected(order_id, reason))
        for order_id, reason in result.suspended:
            self.emit(report.order_suspended(order_id, reason))

    def _apply_fills(self, fills: Iterable[Fill], now: datetime) -> None:
        """把本阶段成交**逐笔**走账本复合事务；事务成功后才更新内存状态。"""

        for fill in fills:
            order = self.engine.get_order(fill.order_id)
            try:
                self._commit_fill(fill, order, now)
            except DuplicateTradeError:
                # 相同成交重复抵达：恢复到同一状态（幂等，不再加仓/加钱）
                if order is not None and order.status == STATUS_FILLED:
                    self._persisted_orders[order.order_id] = (int(order.version), str(order.status))
                continue
            spec = self.pairs.get(fill.pair)
            self.emit(report.fill_report(fill, spec))
            if fill.position_type == POS_OPEN:
                self._create_protection_orders(fill, order, now)
        if fills:
            equity = self.equity_cny()
            self.ledger.save_equity(now, equity if equity is not None else int(self.account.balance))
            self._account_dirty = False

    def _commit_fill(self, fill: Fill, order: Order | None, now: datetime) -> None:
        """成交/持仓/订单终态/事件/费用/账户计数走同一 SQLite 事务，再换内存。"""

        spec = self.pairs.get(fill.pair)
        rates = self._rates()
        account = copy.copy(self.account)
        pnl = int(fill.realized_pnl)
        fee = int(fill.fee)
        account.balance += pnl - fee
        account.realized_pnl += pnl
        account.fees_paid += fee
        position_arg: Position | None = None
        delete_position_id: str | None = None
        if fill.position_type == POS_OPEN:
            position = Position(
                position_id=fill.position_id or f"POS-{fill.order_id}",
                pair=fill.pair,
                side=fill.side,
                units=int(fill.units),
                entry_price=fill.price,
                opened_at=now,
                stop_price=order.request.stop_loss if order is not None else None,
                take_price=order.request.take_profit if order is not None else None,
            )
            if spec is not None and rates is not None:
                position.margin = accounting.required_margin(
                    spec, position.units, fill.price, rates, int(self.config.account.leverage)
                )
            account.trades_today = int(account.trades_today) + 1
            position_arg = position
        else:
            target = self._position_by_id(fill.position_id or "")
            if target is not None:
                remaining = int(target.units) - int(fill.units)
                if remaining <= 0:
                    delete_position_id = target.position_id
                else:
                    position_arg = copy.copy(target)
                    position_arg.units = remaining
        self.ledger.commit_fill(
            fill,
            position=position_arg,
            delete_position_id=delete_position_id,
            order=order,
            event="filled",
            event_detail=f"price={fill.price}",
            account=account,
        )
        # ---- 事务成功后才更新内存 ----
        self.account = account
        if position_arg is not None:
            if delete_position_id is None and fill.position_type == POS_OPEN:
                self.positions.append(position_arg)
            else:
                self._replace_position(position_arg)
        if delete_position_id is not None:
            self._remove_position(delete_position_id)
        if order is not None:
            self._persisted_orders[order.order_id] = (int(order.version), str(order.status))
        if fill.position_type == POS_CLOSE:
            closed_id = delete_position_id or (fill.position_id or "")
            if closed_id and self._position_by_id(closed_id) is None:
                self.engine.cancel_orders_for_position(closed_id, now, reason="POSITION_CLOSED")

    def _position_by_id(self, position_id: str) -> Position | None:
        if not position_id:
            return None
        for position in self.positions:
            if position.position_id == str(position_id):
                return position
        return None

    def _replace_position(self, position: Position) -> None:
        for index, current in enumerate(self.positions):
            if current.position_id == position.position_id:
                self.positions[index] = position
                return
        self.positions.append(position)

    def _remove_position(self, position_id: str) -> None:
        self.positions = [pos for pos in self.positions if pos.position_id != str(position_id)]

    def _create_protection_orders(self, fill: Fill, parent_order: Order | None, now: datetime) -> None:
        """父单成交后按**真实持仓 ID** 建保护单（独立稳定 client ID + 同一 OCO 组）。

        - 止损 → 反向 stop 平仓单；止盈 → 反向 limit 平仓单；
        - 两腿 ``oco_group_id = OCO-<position_id>``，任一腿成交后引擎自动取消另一腿；
        - 同一持仓的 client_order_id 稳定（``prot-sl/tp-<position_id>``），重启不重复建。
        """

        if parent_order is None:
            return
        request = parent_order.request
        if request.stop_loss is None and request.take_profit is None:
            return
        position_id = fill.position_id or f"POS-{fill.order_id}"
        position = self._position_by_id(position_id)
        if position is None:
            return
        close_side = SIDE_SELL if position.side == SIDE_BUY else SIDE_BUY
        group_id = f"OCO-{position_id}"
        legs: list[tuple[str, str, Decimal]] = []
        if request.stop_loss is not None:
            legs.append(("sl", ORDER_STOP, request.stop_loss))
        if request.take_profit is not None:
            legs.append(("tp", ORDER_LIMIT, request.take_profit))
        rates = self._rates()
        quotes = self._engine_quotes()
        for label, order_type, price in legs:
            leg_request = OrderRequest(
                pair=position.pair,
                side=close_side,
                position_type=POS_CLOSE,
                order_type=order_type,
                units=int(position.units),
                client_order_id=f"prot-{label}-{position_id}",
                limit_price=price if order_type == ORDER_LIMIT else None,
                trigger_price=price if order_type == ORDER_STOP else None,
                position_id=position_id,
                oco_group_id=group_id,
                created_by="protection",
            )
            order = self.engine.submit(leg_request, quotes, now, self.account, self.positions, rates)
            if order.status == STATUS_REJECTED:
                self.emit(report.order_rejected(order.order_id, order.reject_reason or "保护单被拒绝"))
            else:
                self.emit(report.order_submitted(order))
        self._persist_orders()

    def _persist_orders(self) -> None:
        """把内存订单（含终态）按 (version, status) 去重落库；不再漏掉已成交/已撤销订单。"""

        for order in self.engine.all_orders():
            key = (int(order.version), str(order.status))
            if self._persisted_orders.get(order.order_id) == key:
                continue
            try:
                self.ledger.save_order(order)
            except Exception as exc:  # noqa: BLE001 - 单条落库失败不拖垮整轮
                self._log(f"订单落库失败 {order.order_id}：{exc}")
                continue
            self._persisted_orders[order.order_id] = key

    def _flush_order_events(self) -> None:
        """把引擎内存审计事件（append-only）写入 order_events；失败只记日志。"""

        for order_id, event, detail in self.engine.drain_events():
            try:
                self.ledger.append_order_event(order_id, event, detail)
            except Exception as exc:  # noqa: BLE001
                self._log(f"订单事件落库失败 {order_id}：{exc}")

    # ---------- 强平 ----------

    def _check_loss_cut(self, now: datetime, rates: FxRates) -> None:
        equity = self.equity_cny()
        margin = self.margin_total_cny()
        if equity is None or margin is None or margin <= 0:
            return
        ratio = Decimal(str(self.config.account.loss_cut_ratio))
        if not accounting.loss_cut_triggered(equity, margin, ratio):
            return
        # 正常强平入口：记录历史最高净值与强平冷却截止时间
        self.account.loss_cut_pending = True
        self.account.peak_equity = max(int(self.account.peak_equity), int(equity))
        cooldown_minutes = int(getattr(self.config.risk, "loss_cut_cooldown_minutes", 0) or 0)
        if cooldown_minutes > 0:
            self.account.loss_cut_cooldown_until = now + timedelta(minutes=cooldown_minutes)
        self._account_dirty = True
        mode = str(getattr(self.config.account, "liquidation_mode", "all") or "all")
        candidates = list(self.positions)
        if mode == "sequential":
            candidates.sort(
                key=lambda p: -accounting.unrealized_pnl(
                    p, self.valid_quotes.get(p.pair), self.pairs[p.pair], rates
                )
            )
        closed: list[Fill] = []
        for position in candidates:
            if self._position_by_id(position.position_id) is None:
                continue
            if mode == "sequential":
                cur_equity = self.equity_cny()
                cur_margin = self.margin_total_cny()
                if (
                    cur_equity is not None
                    and cur_margin
                    and not accounting.loss_cut_triggered(cur_equity, cur_margin, ratio)
                ):
                    break
            fill = self._close_position_at_market(position, now, rates, tag="损失扩大")
            if fill is not None:
                closed.append(fill)
        self.account.loss_cut_pending = False
        self._account_dirty = True
        self.ledger.save_account(self.account)
        if closed:
            self.emit(report.loss_cut_report(closed, self.account, self.equity_cny() or 0, self.positions))
            self._persist_orders()
            self._flush_order_events()

    def _close_position_at_market(
        self, position: Position, now: datetime, rates: FxRates, *, tag: str = ""
    ) -> Fill | None:
        spec = self.pairs.get(position.pair)
        quote = self.valid_quotes.get(position.pair)
        if spec is None or quote is None or quote.bid is None or quote.ask is None:
            return None
        slip = slippage_pips("rocut", spec, self.config)
        slip_price = slip * spec.pip
        exit_price = (quote.bid - slip_price) if position.side == SIDE_BUY else (quote.ask + slip_price)
        exit_price = exit_price.quantize(spec.tick)
        pnl = accounting.close_pnl(position, exit_price, spec, rates)
        fee = accounting.commission(
            position.units, getattr(self.config.account, "commission_per_10k", 0.0)
        )
        fill = Fill(
            order_id=f"LIQ-{position.position_id}",
            pair=position.pair,
            side=SIDE_SELL if position.side == SIDE_BUY else SIDE_BUY,
            position_type=POS_CLOSE,
            units=position.units,
            price=exit_price,
            slippage_pips=slip,
            fee=fee,
            realized_pnl=pnl,
            position_id=position.position_id,
            ts=now,
        )
        # 强平走与正常成交相同的账本入口：先取消该持仓的关联保护单
        self.engine.cancel_orders_for_position(position.position_id, now, reason="LIQUIDATED")
        try:
            self._commit_fill(fill, None, now)
        except DuplicateTradeError:
            return None
        return fill

    # ---------- 日切隔夜利息 ----------

    def _rollover_if_due(self, now: datetime) -> None:
        """日切：隔夜利息与**日风控重置**分开处理，全部走同一事务。

        - 幂等键 = (交易日, 稳定持仓 ID, 天数)，同一天重复 tick 不重复计息；
        - 只按插件结算日历计息（周三 ×3、周六/周日不单独计息）；
        - 关闭 ``account.swap_enabled`` 时仍重置 ``day`` / ``trades_today`` / 日初净值；
        - 停机补算最多 ``MAX_ROLLOVER_CATCHUP_DAYS`` 天，水位只推进到**已完成日期**，
          剩余天数下一 tick 继续，不会把未处理天数直接标成已完成；
        - 汇率缺失时本次日切暂不计息（水位不动），等锚恢复后补算。
        """

        cur_date = market_clock.current_rollover_date(now)
        if cur_date == self._last_rollover_date:
            return
        if not self._last_rollover_date:
            self._last_rollover_date = cur_date
            self.ledger.save_meta("last_rollover", cur_date)
            return
        try:
            last = datetime.strptime(self._last_rollover_date, "%Y-%m-%d").date()
            cur = datetime.strptime(cur_date, "%Y-%m-%d").date()
        except ValueError:
            self._last_rollover_date = cur_date
            self.ledger.save_meta("last_rollover", cur_date)
            return
        day_count = (cur - last).days
        if day_count <= 0:
            return
        swap_enabled = bool(getattr(self.config.account, "swap_enabled", True))
        rates = self._rates() if swap_enabled else None
        if swap_enabled and rates is None:
            self._handle_rate_unavailable()
            return
        if swap_enabled:
            process_date = last + timedelta(days=min(day_count, MAX_ROLLOVER_CATCHUP_DAYS))
        else:
            process_date = cur

        entries: list[tuple[str, str, str, str, int, int, int]] = []
        total_swap = 0
        if swap_enabled and self.positions:
            triple_weekday = int(getattr(self.config.swap, "triple_weekday", 2))
            triple_enabled = bool(getattr(self.config.swap, "triple_enabled", True))
            assert rates is not None
            for offset in range(1, (process_date - last).days + 1):
                day = last + timedelta(days=offset)
                if day.weekday() >= 5:
                    continue  # 周末不单独计息：周三三倍已覆盖
                days = 3 if (triple_enabled and day.weekday() == triple_weekday) else 1
                date_text = day.strftime("%Y-%m-%d")
                for position in self.positions:
                    spec = self.pairs.get(position.pair)
                    if spec is None:
                        continue
                    amount = accounting.swap_amount(position, spec, days, rates)
                    if amount == 0:
                        continue
                    entries.append(
                        (date_text, position.position_id, position.pair, position.side,
                         int(position.units), int(amount), int(days))
                    )
                    total_swap += int(amount)

        account = copy.copy(self.account)
        account.day = process_date.strftime("%Y-%m-%d")
        account.trades_today = 0
        positions = [copy.copy(position) for position in self.positions]
        equity_now = self.equity_cny()
        day_start = (
            int(equity_now) + total_swap if equity_now is not None else int(account.balance) + total_swap
        )
        account.day_start_equity = day_start
        account.peak_equity = max(int(account.peak_equity), day_start)
        applied = self.ledger.commit_rollover(
            entries=entries,
            positions=positions,
            account=account,
            rollover_date=process_date.strftime("%Y-%m-%d"),
            equity_ts=now if equity_now is not None else None,
            equity=day_start if equity_now is not None else None,
        )
        # ---- 事务成功后更新内存 ----
        self.account = account
        self.positions = positions
        self._last_rollover_date = process_date.strftime("%Y-%m-%d")
        self._account_dirty = False
        if applied:
            self.emit(report.swap_report(total_swap, max(1, (process_date - last).days)))
        if swap_enabled and rates is not None:
            self._check_loss_cut(now, rates)

    # ---------- LLM 决策 ----------

    def _in_decision_window(self, now: datetime) -> bool:
        windows = list(getattr(self.config.decision, "decision_windows", []) or [])
        if not windows:
            return True
        local = now.astimezone(CST)
        current = local.hour * 60 + local.minute
        for window in windows:
            try:
                start_text, end_text = str(window).split("-", 1)
                start_h, start_m = [int(x) for x in start_text.split(":")]
                end_h, end_m = [int(x) for x in end_text.split(":")]
            except (ValueError, TypeError):
                continue
            start = start_h * 60 + start_m
            end = end_h * 60 + end_m if not (end_h == 24 and end_m == 0) else 24 * 60
            if end <= start:
                if current >= start or current < end:
                    return True
            elif start <= current < end:
                return True
        return False

    def _ensure_budget(self, now: datetime) -> decision.CallBudget:
        day = market_clock.ny_date(now)
        if self._budget is None or self._budget_day != day:
            self._budget_day = day
            self._budget = decision.CallBudget(day, int(self.config.decision.max_llm_calls_per_day))
        return self._budget

    # ---------- 宿主人设 / 群聊上下文 / 财经日历 ----------

    async def refresh_calendar(self, now: datetime, *, force: bool = False) -> None:
        """刷新财经日历：``source=auto`` 联网取真实事件并合并静态表，失败回退静态表。

        - ``enabled=false`` → 不触网、不改表；
        - ``source=static`` → **不触网**，只用配置里的静态事件表；
        - ``source=auto`` 失败 → 回退静态表，并只记一次原因码（warning 级，由插件层落日志）；
        - 成功 → 静态事件 + 真实事件按 (时间, 货币, 名称) 去重合并。
        """

        section = getattr(self.config, "calendar", None)
        if not bool(getattr(section, "enabled", False)):
            return
        source = str(getattr(section, "source", "auto") or "auto").strip().lower()
        if source != "auto":
            self.calendar.replace_events(self._static_events)
            self.calendar_status = "static"
            self._calendar_warned = False
            return
        try:
            events, reason = await self.calendar_feed.get_events(force=force)
        except Exception as exc:  # noqa: BLE001 - 日历失败绝不影响交易
            events, reason = [], f"{calendar_source.REASON_ALL_SOURCES_FAILED}: {type(exc).__name__}"
        if not events:
            self.calendar.replace_events(self._static_events)
            self.calendar_status = f"{reason or calendar_source.REASON_EMPTY}（已回退静态事件表）"
            if not self._calendar_warned:
                self._calendar_warned = True
                self._log(f"财经日历：{self.calendar_status}")
            return
        self.calendar.replace_events(calendar_source.merge_events(self._static_events, events))
        self.calendar_status = f"auto（{len(self.calendar.events)} 条，含静态表）"
        self._calendar_warned = False

    async def collect_context(self, now: datetime) -> tuple[list[str], list[str]]:
        """收集本轮的（群聊参考材料 / 财经日历）提示片段；失败一律降级为空。

        宿主人设不走这里：``self.persona`` 是一段字符串，由决策层直接作为开场使用。
        """

        chat_context = await self.gather_chat_context(now)
        chat_context_lines = chat_context.prompt_lines()
        calendar_lines = self.calendar.prompt_lines(now) if self.calendar.enabled else []
        return chat_context_lines, calendar_lines

    async def gather_chat_context(self, now: datetime) -> Any:
        """读取群聊上下文；未开启或任何失败都退化为空上下文（绝不抛给调用方）。"""

        del now  # 宿主能力按「当前时间 − hours」自行取数
        inf_cfg = getattr(self.config, "influence", None)
        if not bool(getattr(inf_cfg, "enabled", False)):
            self.last_chat_context = influence_mod.empty_context(note=influence_mod.NOTE_DISABLED)
            return self.last_chat_context
        try:
            if callable(self.chat_context_fetcher):
                context = await influence_mod.maybe_await(self.chat_context_fetcher(None))
            else:
                context = await influence_mod.collect(
                    self.ctx,
                    self.config,
                    self.chat_context_chat_ids,
                    log=self._log,
                )
        except Exception as exc:  # noqa: BLE001 - 群聊读取失败不影响交易
            self._log(f"群聊上下文读取失败（{type(exc).__name__}），本轮按没有群聊处理")
            context = influence_mod.empty_context(note=influence_mod.NOTE_FAILED)
        if not isinstance(context, influence_mod.ChatContext):
            context = influence_mod.empty_context(note=influence_mod.NOTE_FAILED)
        self.last_chat_context = context
        return context

    def _announce_upcoming(self, now: datetime) -> None:
        """事件临近时播报一次「即将公布」（同一事件只播一次）。"""

        if not self.calendar.enabled:
            return
        event = self.calendar.event_soon(now)
        if event is None:
            return
        key = event.raw or f"{event.at.isoformat()}|{event.name}"
        if key in self._announced_events:
            return
        self._announced_events.add(key)
        self.emit(report.event_soon_text(event))

    def _open_block_reason(self, now: datetime) -> str:
        """开仓前的硬性拦截（只限事件窗）；返回原因码（``""`` = 放行）。

        保证金 / 强平 / 持仓上限 / 逆势加仓等账户风控由 ``risk`` 与 ``OrderEngine``
        在提交订单时统一判定，这里不再重复、也不做人设派生。
        """

        blocked, event = self.calendar.blocks_new_position(now)
        if blocked and event is not None:
            return f"{econ_calendar_mod.EVENT_WINDOW}：{event.currency} {event.name}".strip()
        return ""

    async def _maybe_decide(self, now: datetime, rates: FxRates) -> None:
        """执行一次 LLM 决策（**在锁外**）：调用前取快照，返回后严格复核再执行。

        旧决策在以下任一情况下丢弃：暂停、账户被重置（``_ops_version`` 变化）、市场休市、
        行情太久没更新。慢模型只占用自己的 ``asyncio.Task``，不会阻塞行情/保护单/强平。
        """

        cfg = self.config.decision
        if not bool(getattr(cfg, "auto_trade", False)) or self.paused:
            return
        if not bool(getattr(cfg, "llm_enabled", True)):
            return
        if not self._in_decision_window(now):
            return
        interval = timedelta(minutes=max(5, int(getattr(cfg, "interval_minutes", 30))))
        if self._last_decision_at is not None and now - self._last_decision_at < interval:
            return
        budget = self._ensure_budget(now)
        if not budget.can_call():
            return
        snapshot = self._decision_snapshot(now)
        host_chat_lines, calendar_lines = await self.collect_context(now)
        decision_id = f"DEC-{int(now.timestamp())}-{self._ops_version}"
        try:
            intent = await decision.decide(
                self.ctx,
                snapshot["account"],
                snapshot["positions"],
                snapshot["quotes"],
                self.pairs,
                [],
                self.config,
                budget,
                persona=self.persona,
                chat_context_lines=host_chat_lines,
                calendar_lines=calendar_lines,
            )
        except Exception as exc:  # noqa: BLE001 - LLM 失败 hold
            self.last_error = f"LLM 决策失败：{exc}"
            self._log(self.last_error)
            return
        self._last_decision_at = market_clock.now_utc()
        self.last_intent = intent
        outcome = self._execute_intent(intent, snapshot, decision_id)
        self._save_decision_record(decision_id, intent, outcome)

    def _save_decision_record(self, decision_id: str, intent: DecisionIntent, outcome: str) -> None:
        """把结构化决策号/动作/理由落账本 decisions（审计用，失败不影响交易）。"""

        try:
            self.ledger.save_decision(
                decision_id,
                "llm",
                (
                    f"action={intent.action};pair={intent.pair};units={intent.units};"
                    f"price={intent.price};stop_loss={intent.stop_loss};"
                    f"take_profit={intent.take_profit};reason={intent.reason}"
                ),
                outcome,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"决策记录落库失败 {decision_id}：{exc}")

    def _decision_snapshot(self, now: datetime) -> dict[str, Any]:
        """决策开始时的快照（账户/持仓/行情/重置版本），供返回后复核。"""

        return {
            "seq": self.seq,
            "now": now,
            "ops_version": self._ops_version,
            "quotes": dict(self.quotes),
            "mids": {code: fxconv.quote_mid(quote) for code, quote in self.quotes.items()},
            "account": copy.copy(self.account),
            "positions": [copy.copy(position) for position in self.positions],
        }

    def _execute_intent(
        self,
        intent: DecisionIntent,
        snapshot: dict[str, Any],
        decision_id: str = "",
    ) -> str:
        """执行前重查暂停/账户版本/市场状态/新行情；旧决策直接丢弃并播报。"""

        del decision_id
        now = market_clock.now_utc()
        drop_reason = ""
        if self.paused:
            drop_reason = "PAUSED"
        elif int(snapshot.get("ops_version", 0)) != int(self._ops_version):
            drop_reason = "ACCOUNT_RESET"
        elif market_clock.market_status(now) != "open":
            drop_reason = "MARKET_CLOSED"
        else:
            last_quotes = self._last_quotes_at
            if last_quotes is None:
                drop_reason = "QUOTE_STALE"
            else:
                max_age = max(90, 2 * int(getattr(self.config.market, "stale_quote_seconds", 60)))
                if (now - last_quotes).total_seconds() > max_age:
                    drop_reason = "QUOTE_STALE"
        if drop_reason:
            self.last_dropped_decision = drop_reason
            self._log(f"丢弃过期决策（{drop_reason}）")
            self.emit(report.decision_dropped_text(drop_reason))
            return f"dropped:{drop_reason}"
        if intent.action in ("hold", "", None):
            self.emit(report.decision_report(intent))
            return "hold"
        rates = self._rates()
        requests, reasons = self._intent_to_requests(intent, snapshot, now, rates)
        self.emit(report.decision_report(intent))
        for reason in reasons:
            self.emit(report.order_rejected("", reason))
        if not requests and not reasons:
            return "rejected"
        for request in requests:
            available = self._engine_quotes()
            order = self.engine.submit(request, available, now, self.account, self.positions, rates)
            if order.status == STATUS_REJECTED:
                self.emit(report.order_rejected(order.order_id, order.reject_reason or ""))
            else:
                self.emit(report.order_submitted(order))
        self._persist_orders()
        return "submitted" if requests else "rejected"

    def _intent_to_requests(
        self,
        intent: DecisionIntent,
        snapshot: dict[str, Any],
        now: datetime,
        rates: FxRates | None,
    ) -> tuple[list[OrderRequest], list[str]]:
        """把决策意图转成**合法、正向、逐持仓**的订单请求列表。

        - ``close`` / ``close_all``：按目标持仓逐笔拆单，平空仓用**买入**方向与正数数量；
        - 开仓：止损/止盈进入请求；按可成交价 + 合理止损 + 实际汇率重算 2% 止损预算，
          超预算则缩量（``RISK_REDUCED`` 播报）或拒绝（``RISK_SIZING_ZERO``）；
        - 漂移口径：市价单比较**决策时 mid** 与**执行时 mid**（独立时间戳/目标价）；
          限价/止损单校验委托目标价本身（tick 对齐 + 距当前 mid ≤ 20%），不因"远端合法限价"拒单。
        """

        if rates is None:
            return [], [fxconv.RATE_UNAVAILABLE]
        if intent.action in ("close", "close_all"):
            return self._close_requests(intent, now)
        spec = self.pairs.get(intent.pair)
        if spec is None:
            return [], ["INVALID_PAIR"]
        quote = self.valid_quotes.get(intent.pair)
        if quote is None:
            return [], [self.quote_issues.get(intent.pair) or "QUOTE_STALE"]
        mid = fxconv.quote_mid(quote)
        if mid is None:
            return [], ["QUOTE_MISSING"]
        order_type = (
            intent.order_type
            if intent.order_type in (ORDER_MARKET, ORDER_LIMIT, ORDER_STOP)
            else ORDER_MARKET
        )
        max_drift = Decimal(str(getattr(self.config.decision, "max_drift_pips", 3)))
        if order_type == ORDER_MARKET:
            snapshot_mid = snapshot.get("mids", {}).get(intent.pair)
            if snapshot_mid is not None and abs(mid - snapshot_mid) / spec.pip > max_drift:
                return [], ["PRICE_MOVED"]
        else:
            if intent.price is None:
                return [], [
                    "INVALID_PRICE：limit/stop 单必须给 price"
                ]
            if intent.price % spec.tick != 0:
                return [], ["INVALID_PRICE_TICK"]
            if abs(intent.price - mid) / mid > Decimal("0.20"):
                return [], ["PRICE_OUT_OF_RANGE"]
        side = SIDE_BUY if intent.action == "open_long" else SIDE_SELL
        stop_loss = intent.stop_loss
        take_profit = intent.take_profit
        if stop_loss is None:
            return [], ["MISSING_STOP_LOSS"]
        units = int(intent.units)
        entry = quote.ask if side == SIDE_BUY else quote.bid
        if entry is None or entry <= 0:
            return [], ["QUOTE_MISSING"]
        reason = risk.validate_protection_plan(spec, side, entry, stop_loss, take_profit)
        if reason:
            return [], [reason]
        sized = risk.size_by_risk(self.account, spec, entry, stop_loss, rates, self.config)
        if sized <= 0:
            return [], ["RISK_SIZING_ZERO"]
        if units > sized:
            units = int(sized)
            ratio = Decimal(str(getattr(self.config.risk, "risk_per_trade_ratio", 0.02)))
            self.emit(
                report.card_notice(
                    "⚠️ 下单数量已按止损预算调整",
                    [
                        f"计划风险超过账户 {ratio:.0%} 的单笔上限",
                        f"已缩量到 {units:,} 货币（止损 {intent.stop_loss}）",
                    ],
                    footer="📝 止损越远，可开数量越小（纯模拟盘）",
                )
            )
        request = OrderRequest(
            pair=spec.code,
            side=side,
            position_type=POS_OPEN,
            order_type=order_type,
            units=units,
            client_order_id=f"llm-{spec.code}-{int(now.timestamp())}-{units}",
            limit_price=intent.price if order_type == ORDER_LIMIT else None,
            trigger_price=intent.price if order_type == ORDER_STOP else None,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return [request], []

    def _close_requests(self, intent: DecisionIntent, now: datetime) -> tuple[list[OrderRequest], list[str]]:
        """按目标持仓逐笔拆反向、正数平仓单（平空仓用买入方向 + 正数数量）。"""

        targets = [position for position in self.positions if position.pair == intent.pair]
        if not targets:
            return [], ["POSITION_NOT_FOUND"]
        targets.sort(key=lambda p: (p.opened_at or datetime.min, p.position_id))
        selected: list[tuple[Position, int]] = []
        remaining = int(intent.units or 0)
        for position in targets:
            if remaining > 0:
                units = min(remaining, int(position.units))
                remaining -= units
            else:
                units = int(position.units)
            if units <= 0:
                continue
            selected.append((position, units))
            if intent.action not in ("close_all",) and intent.units and remaining <= 0:
                break
        if intent.units and remaining > 0:
            return [], ["CLOSE_UNITS_EXCEED"]
        requests: list[OrderRequest] = []
        for position, units in selected:
            side = SIDE_SELL if position.side == SIDE_BUY else SIDE_BUY
            requests.append(
                OrderRequest(
                    pair=position.pair,
                    side=side,
                    position_type=POS_CLOSE,
                    order_type=ORDER_MARKET,
                    units=int(units),
                    client_order_id=f"llm-close-{position.position_id}-{int(now.timestamp())}",
                    position_id=position.position_id,
                )
            )
        return requests, []

    def _decision_cycle(self, now: datetime, rates: FxRates) -> None:
        """主 tick 调度决策：已有决策在跑就不并发；否则条件满足才起独立任务（不阻塞 tick）。"""

        if self._decision_task is not None and not self._decision_task.done():
            return
        if self._decision_task is not None and self._decision_task.done():
            try:
                self._decision_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"LLM 决策任务异常：{exc}"
                self._log(self.last_error)
            self._decision_task = None
        cfg = self.config.decision
        if not bool(getattr(cfg, "auto_trade", False)) or self.paused:
            return
        if not bool(getattr(cfg, "llm_enabled", True)):
            return
        if not self._in_decision_window(now):
            return
        interval = timedelta(minutes=max(5, int(getattr(cfg, "interval_minutes", 30))))
        if self._last_decision_at is not None and now - self._last_decision_at < interval:
            return
        if not self._ensure_budget(now).can_call():
            return
        self._decision_task = asyncio.create_task(self._maybe_decide(now, rates), name="mai-fx-decision")

    # ---------- 管理操作 ----------

    def pause(self) -> None:
        self.paused = True
        self.ledger.save_meta("paused", "1")

    def resume(self) -> None:
        self.paused = False
        self.ledger.save_meta("paused", "0")

    def manual_order(
        self,
        *,
        pair_text: str,
        side: str,
        units: int,
        order_type: str = ORDER_MARKET,
        price: Decimal | None = None,
        user: str = "",
        position_type: str = POS_OPEN,
        position_id: str | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> tuple[bool, str]:
        spec = pairs_mod.resolve_pair(pair_text)
        if spec is None:
            return False, f"未知品种：{pair_text}"
        quote = self.valid_quotes.get(spec.code)
        if quote is None:
            reason = self.quote_issues.get(spec.code) or "QUOTE_STALE"
            return False, f"行情不可用（{reason}），已拒绝下单"
        rates = self._rates()
        if rates is None:
            return False, f"{fxconv.RATE_UNAVAILABLE}：汇率未就绪（缺 USDCNY 或 USDJPY）"
        now = market_clock.now_utc()
        request = OrderRequest(
            pair=spec.code,
            side=side,
            position_type=position_type,
            order_type=order_type,
            units=int(units),
            client_order_id=f"manual-{spec.code}-{int(now.timestamp())}",
            limit_price=price if order_type == ORDER_LIMIT else None,
            trigger_price=price if order_type == ORDER_STOP else None,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_id=position_id,
            created_by=user,
        )
        order = self.engine.submit(
            request, self._engine_quotes(), now, self.account, self.positions, rates
        )
        self._persist_orders()
        if order.status == STATUS_REJECTED:
            return False, order.reject_reason or "订单被拒绝"
        return True, order.order_id

    def close_position(self, pair_text: str, user: str = "") -> tuple[bool, str]:
        spec = pairs_mod.resolve_pair(pair_text)
        if spec is None:
            return False, f"未知品种：{pair_text}"
        targets = [p for p in self.positions if p.pair == spec.code]
        if not targets:
            return False, "没有该品种持仓"
        quote = self.valid_quotes.get(spec.code)
        if quote is None:
            reason = self.quote_issues.get(spec.code) or "QUOTE_STALE"
            return False, f"行情不可用（{reason}），已拒绝平仓"
        now = market_clock.now_utc()
        rates = self._rates()
        if rates is None:
            return False, f"{fxconv.RATE_UNAVAILABLE}：汇率未就绪（缺 USDCNY 或 USDJPY）"
        for position in targets:
            side = SIDE_SELL if position.side == SIDE_BUY else SIDE_BUY
            request = OrderRequest(
                pair=spec.code,
                side=side,
                position_type=POS_CLOSE,
                order_type=ORDER_MARKET,
                units=position.units,
                client_order_id=f"close-{position.position_id}-{int(now.timestamp())}",
                position_id=position.position_id,
                created_by=user,
            )
            self.engine.submit(
                request, self._engine_quotes(), now, self.account, self.positions, rates
            )
        self._persist_orders()
        return True, "平仓委托已提交"

    def cancel_order(self, order_id: str) -> tuple[bool, str]:
        ok, reason = self.engine.cancel(order_id, market_clock.now_utc())
        self._persist_orders()
        return ok, reason

    def reset(self) -> None:
        if self._decision_task is not None and not self._decision_task.done():
            self._decision_task.cancel()
        self._decision_task = None
        initial = int(self.config.account.initial_balance)
        self.ledger.reset(initial)
        self.account = self.ledger.load_account()
        self.positions = []
        self.valid_quotes = {}
        self.quote_issues = {}
        self._last_source_ts.clear()
        self._quote_guard_key = ()
        self._quote_guard_warned = False
        self.engine = OrderEngine(self.pairs, self.config, open_block_fn=self._open_block_reason)
        self._persisted_orders = {}
        self._ops_version += 1
        self._account_dirty = True
        self._last_rollover_date = market_clock.current_rollover_date(market_clock.now_utc())
        self.ledger.save_meta("last_rollover", self._last_rollover_date)
        self._last_decision_at = None
        self._announced_events.clear()
        self.last_chat_context = influence_mod.empty_context()
        self.calendar.replace_events(self._static_events)
        self.calendar_feed.clear()
        self.calendar_status = ""
        self._calendar_warned = False
        self._rate_warned = False
        self.last_intent = None
        self.last_dropped_decision = ""
        self.events.clear()
