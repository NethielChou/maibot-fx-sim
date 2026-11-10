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
)
from .execution_cost import slippage_pips
from .fxconv import FxRates
from .models import (
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    POS_CLOSE,
    POS_OPEN,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_CANCELLED,
    STATUS_REJECTED,
    AccountState,
    DecisionIntent,
    Fill,
    Order,
    OrderRequest,
    Position,
    QuoteSnapshot,
)
from .orders import OrderEngine

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

        self.engine = OrderEngine(self.pairs, config)
        self.engine.restore(ledger.load_orders_pending())

        # 群聊上下文：默认由插件注入读取器（走宿主 message.get_recent）
        self.chat_context_fetcher: Callable[[datetime], Any] | None = chat_context_fetcher
        self.chat_context_chat_ids: list[str] = []
        self.last_chat_context: Any = influence_mod.empty_context()
        self._announced_events: set[str] = set()

        self.quotes: dict[str, QuoteSnapshot] = {}
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
        self._budget: decision.CallBudget | None = None
        self._budget_day: str = ""
        self.last_intent: DecisionIntent | None = None
        self.last_error: str = ""

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="mai-fx-engine")

    async def stop(self) -> None:
        self._stop.set()
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
        """**只用本 tick 的** USDJPY + USDCNY；缺失返回 None（不用上一次的旧汇率）。

        旧汇率在行情源断线时会掩盖问题（估值/保证金都会失真），因此缺汇率一律
        挂起估值、拒绝开仓（原因码 ``RATE_UNAVAILABLE``），等两条锚点都回来再继续。
        """

        return fxconv.rates_from_quotes(self.quotes)

    def equity_cny(self) -> int | None:
        rates = self._rates()
        if rates is None:
            return None
        return accounting.equity(self.account, self.positions, self.quotes, self.pairs, rates)

    def margin_total_cny(self) -> int | None:
        rates = self._rates()
        if rates is None:
            return None
        return accounting.required_margin_total(
            self.positions, self.quotes, self.pairs, rates, int(self.config.account.leverage)
        )

    def maintenance_ratio(self) -> Decimal | None:
        equity = self.equity_cny()
        margin = self.margin_total_cny()
        if equity is None or margin is None:
            return None
        return accounting.maintenance_ratio(equity, margin)

    def _handle_rate_unavailable(self) -> None:
        """汇率缺失：本轮不估值、不撮合、不开仓；同一段缺失只播报并记录一次日志。"""

        self.last_error = (
            f"{fxconv.RATE_UNAVAILABLE}：缺少 USDCNY/USDJPY 汇率，"
            "账户估值与开仓已挂起（不会用假汇率）"
        )
        if not self._rate_warned:
            self._rate_warned = True
            self._log(self.last_error)
            self.emit(report.rate_unavailable_text())

    # ---------- 主 tick ----------

    async def tick_once(self) -> None:
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
            self.quotes = quotes
            self._last_quotes_at = now
            await self.refresh_calendar(now)
            self._rollover_if_due(now)
            if market_clock.market_status(now) != "open":
                return
            rates = self._rates()
            if rates is None:
                self._handle_rate_unavailable()
                return
            self._rate_warned = False
            if not self.paused:
                self._process_orders(now, rates)
                self._check_loss_cut(now, rates)
                self._announce_upcoming(now)
                await self._maybe_decide(now, rates)
            self._persist_orders()

    # ---------- 订单处理 ----------

    def _process_orders(self, now: datetime, rates: FxRates) -> None:
        result = self.engine.process_tick(self.quotes, now, self.account, self.positions, rates)
        self._apply_fills(result.fills, now)
        for order_id, reason in result.rejected:
            self.emit(report.order_rejected(order_id, reason))
        for order_id, reason in result.suspended:
            self.emit(report.order_suspended(order_id, reason))

    def _apply_fills(self, fills: Iterable[Fill], now: datetime) -> None:
        for fill in fills:
            spec = self.pairs.get(fill.pair)
            self.account.fees_paid += int(fill.fee)
            self.account.balance -= int(fill.fee)
            if fill.position_type == POS_OPEN and fill.units > 0:
                position = Position(
                    position_id=fill.position_id or f"POS-{fill.order_id}",
                    pair=fill.pair,
                    side=fill.side,
                    units=int(fill.units),
                    entry_price=fill.price,
                    opened_at=now,
                )
                if spec is not None:
                    position.margin = accounting.required_margin(
                        spec,
                        position.units,
                        fill.price,
                        self._rates(),
                        int(self.config.account.leverage),
                    )
                self.positions.append(position)
                self.account.trades_today += 1
                self.ledger.upsert_position(position)
                self.emit(report.fill_report(fill, spec))
            elif fill.position_type == POS_CLOSE:
                pnl = int(fill.realized_pnl)
                self.account.balance += pnl
                self.account.realized_pnl += pnl
                self._reduce_position(fill, now)
                self.emit(report.fill_report(fill, spec))
            self.ledger.insert_trade(fill)
            self.ledger.save_equity(now, self.equity_cny() or self.account.balance)

    def _reduce_position(self, fill: Fill, now: datetime) -> None:
        remaining = int(fill.units)
        for position in list(self.positions):
            if remaining <= 0:
                break
            if fill.position_id and position.position_id != fill.position_id:
                continue
            if position.pair != fill.pair or position.side == fill.side:
                continue
            if not fill.position_id and position.opened_at > now:
                continue
            used = min(remaining, position.units)
            remaining -= used
            position.units -= used
            if position.units <= 0:
                self.positions.remove(position)
                self.ledger.delete_position(position.position_id)
            else:
                self.ledger.upsert_position(position)

    def _persist_orders(self) -> None:
        for order in getattr(self.engine, "pending", []):
            try:
                self.ledger.save_order(order)
            except Exception:  # noqa: BLE001
                continue

    # ---------- 强平 ----------

    def _check_loss_cut(self, now: datetime, rates: FxRates) -> None:
        equity = self.equity_cny()
        margin = self.margin_total_cny()
        if equity is None or margin is None or margin <= 0:
            return
        ratio = Decimal(str(self.config.account.loss_cut_ratio))
        if not accounting.loss_cut_triggered(equity, margin, ratio):
            return
        self.account.loss_cut_pending = True
        mode = str(getattr(self.config.account, "liquidation_mode", "all") or "all")
        candidates = list(self.positions)
        if mode == "sequential":
            candidates.sort(
                key=lambda p: -accounting.unrealized_pnl(p, self.quotes.get(p.pair), self.pairs[p.pair], rates)
            )
        closed: list[Fill] = []
        for position in candidates:
            if position not in self.positions:
                continue
            if mode == "sequential":
                cur_equity = self.equity_cny() or 0
                cur_margin = self.margin_total_cny() or 0
                if cur_margin and not accounting.loss_cut_triggered(cur_equity, cur_margin, ratio):
                    break
            fill = self._close_position_at_market(position, now, rates, tag="损失扩大")
            if fill is not None:
                closed.append(fill)
        self.account.loss_cut_pending = False
        if closed:
            self.emit(report.loss_cut_report(closed, self.account, self.equity_cny() or 0, self.positions))
            self._persist_orders()

    def _close_position_at_market(
        self, position: Position, now: datetime, rates: FxRates, *, tag: str = ""
    ) -> Fill | None:
        spec = self.pairs.get(position.pair)
        quote = self.quotes.get(position.pair)
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
        self.account.fees_paid += fee
        self.account.balance += pnl - fee
        self.account.realized_pnl += pnl
        self.positions.remove(position)
        self.ledger.delete_position(position.position_id)
        self.ledger.insert_trade(fill)
        return fill

    # ---------- 日切隔夜利息 ----------

    def _rollover_if_due(self, now: datetime) -> None:
        if not bool(getattr(self.config.account, "swap_enabled", True)):
            return
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
        if day_count > MAX_ROLLOVER_CATCHUP_DAYS:
            day_count = MAX_ROLLOVER_CATCHUP_DAYS
        rates = self._rates()
        if rates is None:
            # 汇率缺失：本次日切暂不计息（不改 last_rollover），等汇率恢复后补算
            self._handle_rate_unavailable()
            return
        total_swap = 0
        specs = self.pairs
        triple_weekday = int(getattr(self.config.swap, "triple_weekday", 2))
        triple_enabled = bool(getattr(self.config.swap, "triple_enabled", True))
        for offset in range(1, day_count + 1):
            day = last + timedelta(days=offset)
            days = 3 if (triple_enabled and day.weekday() == triple_weekday) else 1
            date_text = day.strftime("%Y-%m-%d")
            for position in list(self.positions):
                spec = specs.get(position.pair)
                if spec is None:
                    continue
                amount = accounting.swap_amount(position, spec, days, rates)
                if amount == 0:
                    continue
                total_swap += amount
                position.swap_accrued += amount
                self.ledger.upsert_position(position)
                self.ledger.insert_swap(date_text, position.pair, position.side, position.units, amount, days)
                if bool(getattr(self.config.account, "swap_enabled", True)):
                    self.account.balance += amount
                    self.account.swap_paid += amount
        self._last_rollover_date = cur_date
        self.ledger.save_meta("last_rollover", cur_date)
        self.account.day = cur_date
        self.account.trades_today = 0
        equity = self.equity_cny()
        if equity is not None:
            self.account.day_start_equity = equity
            self.account.peak_equity = max(self.account.peak_equity, equity)
            self.ledger.save_equity(now, equity)
        self.ledger.save_account(self.account)
        if total_swap:
            self.emit(report.swap_report(total_swap, day_count))
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
        host_chat_lines, calendar_lines = await self.collect_context(now)
        try:
            intent = await decision.decide(
                self.ctx,
                self.account,
                self.positions,
                self.quotes,
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
        self._last_decision_at = now
        if intent.action in ("open_long", "open_short"):
            block = self._open_block_reason(now)
            if block:
                self.last_intent = intent
                self.emit(report.decision_report(intent))
                self.emit(report.order_rejected("", block))
                return
        self.last_intent = intent
        self.emit(report.decision_report(intent))
        request = self._intent_to_request(intent, now)
        if request is None:
            return
        order = self.engine.submit(request, self.quotes, now, self.account, self.positions, rates)
        self.emit(report.order_submitted(order))
        self._persist_orders()

    def _intent_to_request(self, intent: DecisionIntent, now: datetime) -> OrderRequest | None:
        if intent.action in ("hold", "", None):
            return None
        spec = self.pairs.get(intent.pair)
        quote = self.quotes.get(intent.pair)
        if spec is None or quote is None:
            return None
        mid = quote.mid or quote.bid or quote.ask
        if mid is None:
            return None
        max_drift = Decimal(str(getattr(self.config.decision, "max_drift_pips", 3)))
        if intent.price is not None and abs(intent.price - mid) / spec.pip > max_drift:
            self.emit(report.order_rejected("", "DRIFT_TOO_LARGE"))
            return None
        if intent.action in ("close", "close_all"):
            return OrderRequest(
                pair=intent.pair,
                side=SIDE_SELL,
                position_type=POS_CLOSE,
                order_type=ORDER_MARKET,
                units=intent.units,
                client_order_id=f"llm-{intent.pair}-{int(now.timestamp())}",
                decision_id=None,
            )
        side = SIDE_BUY if intent.action == "open_long" else SIDE_SELL
        order_type = intent.order_type if intent.order_type in (ORDER_MARKET, ORDER_LIMIT, ORDER_STOP) else ORDER_MARKET
        return OrderRequest(
            pair=intent.pair,
            side=side,
            position_type=POS_OPEN,
            order_type=order_type,
            units=int(intent.units),
            client_order_id=f"llm-{intent.pair}-{int(now.timestamp())}",
            limit_price=intent.price if order_type == ORDER_LIMIT else None,
            trigger_price=intent.price if order_type == ORDER_STOP else None,
        )

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
    ) -> tuple[bool, str]:
        spec = pairs_mod.resolve_pair(pair_text)
        if spec is None:
            return False, f"未知品种：{pair_text}"
        quote = self.quotes.get(spec.code)
        if quote is None:
            return False, "行情尚未就绪"
        rates = self._rates()
        if rates is None:
            return False, f"{fxconv.RATE_UNAVAILABLE}：汇率未就绪（缺 USDCNY 或 USDJPY）"
        request = OrderRequest(
            pair=spec.code,
            side=side,
            position_type=position_type,
            order_type=order_type,
            units=int(units),
            client_order_id=f"manual-{spec.code}-{int(market_clock.now_utc().timestamp())}",
            limit_price=price if order_type == ORDER_LIMIT else None,
            trigger_price=price if order_type == ORDER_STOP else None,
            position_id=position_id,
            created_by=user,
        )
        order = self.engine.submit(request, self.quotes, market_clock.now_utc(), self.account, self.positions, rates)
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
        quote = self.quotes.get(spec.code)
        if quote is None:
            return False, "行情尚未就绪"
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
            self.engine.submit(request, self.quotes, now, self.account, self.positions, rates)
        self._persist_orders()
        return True, "平仓委托已提交"

    def cancel_order(self, order_id: str) -> tuple[bool, str]:
        ok, reason = self.engine.cancel(order_id, market_clock.now_utc())
        self._persist_orders()
        return ok, reason

    def reset(self) -> None:
        initial = int(self.config.account.initial_balance)
        self.ledger.reset(initial)
        self.account = self.ledger.load_account()
        self.positions = []
        self.engine = OrderEngine(self.pairs, self.config)
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
        self.events.clear()
