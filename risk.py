"""开仓风控：止损 2% 定量、保证金约束、日亏/回撤/亏损加仓限制。

本模块是**纯函数集合**，不读写 DB、不拉行情、不 import 宿主 SDK；配置对象按
`config.py` 的 `MaiFxConfig` 结构鸭子类型访问（便于单测注入轻量 stub）。

关键口径（账户币种=虚拟人民币/元）：

- 单笔风险定量：`risk = 账户净值 × risk_per_trade_ratio`（P0 以 balance 作为账户净值近似，
  引擎在 `submit` 校验阶段使用；有持仓时偏差由保证金/维持率检查兜底）
  `units = floor(risk / (stop_pips × 1 货币每 pip 元值))`，再向下取 1,000 整数倍
  - JPY 货币对：1 pip 元值 = `pip / jpy_per_cny`
  - USD 货币对（EURUSD）：1 pip 元值 = `pip × cny_per_usd`
- 单笔保证金上限：`所需保证金 ≤ 账户净值 × margin_per_order_ratio`（默认 15%）
- 开仓后维持率 ≥ margin_call_ratio（默认 100%）：`账户净值 ≥ 所需保证金合計 + 本单所需保证金`
- 日亏：`(day_start_equity − equity) > day_start_equity × daily_loss_limit_ratio` → 停手
- 回撤：`(peak_equity − equity) > peak_equity × max_drawdown_ratio` → 停手
- 亏损禁加仓（逆势加仓禁止）：同货币对同方向、且该持仓浮动盈亏 < 0 → 拒单 `NANPIN_FORBIDDEN`
- **缺 USDCNY/USDJPY 汇率** → 直接拒单 `RATE_UNAVAILABLE`（不得用假汇率开仓）

拒单原因码（与订单引擎统一）：`ACCOUNT_HALTED` / `LOSS_CUT_PENDING` /
`LOSS_CUT_COOLDOWN` / `DAILY_LOSS_LIMIT` / `MAX_DRAWDOWN` / `DAILY_TRADE_LIMIT` /
`HEDGE_NOT_ALLOWED` / `NANPIN_FORBIDDEN` / `MAX_POSITIONS` / `MAX_UNITS_EXCEEDED` /
`MARGIN_PER_ORDER_EXCEEDED` / `INSUFFICIENT_MARGIN` / `RATE_UNAVAILABLE` / `QUOTE_MISSING` /
`INVALID_UNITS` / `INVALID_PRICE` / `INVALID_STOP` / `INVALID_POSITION_TYPE` / `RISK_SIZING_ZERO`。

`check_open` 的执行顺序固定（先廉价参数校验 → 账户状态 → 数量/仓位上限 → 对冲/加仓 →
保证金），便于单测断言与用户提示稳定。**点差/行情可用性由调用方（订单引擎）先判**，
本模块只要求能取到报价仲值与汇率来估算保证金。
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Mapping, Sequence

try:  # 插件包内加载
    from . import accounting  # type: ignore[import-not-found]
    from .fxconv import (  # type: ignore[import-not-found]
        RATE_UNAVAILABLE,
        FxRates,
        RateUnavailable,
        require_rates,
    )
    from .models import (  # type: ignore[import-not-found]
        ORDER_LIMIT,
        ORDER_MARKET,
        ORDER_STOP,
        ORDER_TRAILING,
        POS_OPEN,
        SIDE_BUY,
        AccountState,
        OrderRequest,
        PairSpec,
        Position,
        QuoteSnapshot,
    )
except ImportError:  # 项目根直接加载（单测 / 脚本）
    import accounting  # type: ignore[no-redef]
    from fxconv import (  # type: ignore[no-redef]
        RATE_UNAVAILABLE,
        FxRates,
        RateUnavailable,
        require_rates,
    )
    from models import (  # type: ignore[no-redef]
        ORDER_LIMIT,
        ORDER_MARKET,
        ORDER_STOP,
        ORDER_TRAILING,
        POS_OPEN,
        SIDE_BUY,
        AccountState,
        OrderRequest,
        PairSpec,
        Position,
        QuoteSnapshot,
    )

_UNITS_STEP = 1_000  # 最小下单单位 / 数量步长（Rev 1.1：1,000 货币）


def _dec(value: Decimal | int | float | str) -> Decimal:
    """Decimal/int/float/str → Decimal（float 走 str，避免二进制误差）。"""
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


# ---------------------------------------------------------------------------
# 止损定量
# ---------------------------------------------------------------------------


def size_by_risk(
    account: AccountState,
    spec: PairSpec,
    entry_price: Decimal | None,
    stop_price: Decimal | None,
    rates: FxRates | None,
    cfg,
) -> int:
    """按「单笔止损风险 = 账户净值 × risk_per_trade_ratio」计算下单量。

    返回 **1,000 货币整数倍**；无法定量（无止损价/止损距离为 0/结果小于最小单位）返回 0，
    表示该单应改用别的数量或拒单（`RISK_SIZING_ZERO`）。

    公式：
    ```
    risk       = balance × risk_per_trade_ratio        # P0 的账户净值近似
    pip_value  = 1 货币 1 pip 的元値
               = pip / jpy_per_cny                  （JPY 货币对）
               = pip × cny_per_usd                  （EURUSD）
    stop_pips  = |entry − stop| / pip
    units      = floor(risk / (stop_pips × pip_value) / 1,000) × 1,000
    units      = min(units, spec.max_units, cfg.risk.max_units_per_pair)
    ```
    """
    if entry_price is None or stop_price is None:
        return 0
    entry = _dec(entry_price)
    stop = _dec(stop_price)
    if entry <= 0 or stop <= 0:
        raise ValueError("INVALID_PRICE: entry/stop must be > 0")
    pip = _dec(spec.pip)
    if pip <= 0:
        raise ValueError("INVALID_PIP: spec.pip must be > 0")
    stop_pips = abs(entry - stop) / pip
    if stop_pips <= 0:
        return 0
    pip_value = require_rates(rates).pip_value(spec, 1)
    ratio = _dec(cfg.risk.risk_per_trade_ratio)
    if ratio <= 0:
        return 0
    risk = _dec(account.balance) * ratio
    raw_units = (risk / (stop_pips * pip_value)).to_integral_value(rounding=ROUND_FLOOR)
    if raw_units < _UNITS_STEP:
        return 0
    units = int(raw_units) // _UNITS_STEP * _UNITS_STEP
    cap = min(int(spec.max_units), int(cfg.risk.max_units_per_pair))
    if cap > 0:
        units = min(units, cap // _UNITS_STEP * _UNITS_STEP)
    return max(units, 0)


# ---------------------------------------------------------------------------
# 日亏 / 回撤
# ---------------------------------------------------------------------------


def daily_loss_hit(account: AccountState, equity: int, cfg) -> bool:
    """当日亏损是否超过上限：`loss > day_start_equity × daily_loss_limit_ratio`。

    - 严格大于（恰好等于上限**不**触发）
    - day_start_equity ≤ 0（异常账本）时不判定
    - 盈利（equity > day_start）不触发
    """
    start = int(account.day_start_equity)
    if start <= 0:
        return False
    limit = _dec(start) * _dec(cfg.risk.daily_loss_limit_ratio)
    loss = Decimal(start) - Decimal(int(equity))
    if loss <= 0:
        return False
    return loss > limit


def drawdown_hit(account: AccountState, equity: int, cfg) -> bool:
    """历史最高账户净值回撤是否超过上限：`(peak − equity) > peak × max_drawdown_ratio`。"""
    peak = int(account.peak_equity)
    if peak <= 0:
        return False
    limit = _dec(peak) * _dec(cfg.risk.max_drawdown_ratio)
    dd = Decimal(peak) - Decimal(int(equity))
    if dd <= 0:
        return False
    return dd > limit


# ---------------------------------------------------------------------------
# 亏损加仓限制
# ---------------------------------------------------------------------------


def allow_add_to_loser(
    position: Position,
    req: OrderRequest,
    spec: PairSpec,
    quote: QuoteSnapshot | None,
    rates: FxRates | None,
    cfg,
) -> tuple[bool, str]:
    """是否允许对「浮亏持仓」同方向加仓（逆势加仓）。

    返回 `(ok, reason)`；仅当配置 `forbid_add_to_loser=true`、且该持仓与本次开仓
    **同货币对同方向**、且该持仓**当前浮动盈亏 < 0** 时拒绝，原因码 `NANPIN_FORBIDDEN`。
    不同方向（双向持仓）/不同货币对/浮盈加仓 → 允许。
    """
    if not bool(cfg.risk.forbid_add_to_loser):
        return True, ""
    if req.position_type != POS_OPEN:
        return True, ""
    if position.pair != req.pair or position.side != req.side:
        return True, ""
    pnl = accounting.unrealized_pnl(position, quote, spec, rates)
    if pnl < 0:
        return False, "NANPIN_FORBIDDEN"
    return True, ""


# ---------------------------------------------------------------------------
# 开仓综合校验
# ---------------------------------------------------------------------------


def _estimate_price(req: OrderRequest, quote: QuoteSnapshot | None) -> Decimal | None:
    """估算本单持仓价（用于保证金）：限价用 limit_price、止损用 trigger_price，市价用仲值。"""
    if req.order_type == ORDER_LIMIT and req.limit_price is not None:
        return _dec(req.limit_price)
    if req.order_type in (ORDER_STOP, ORDER_TRAILING) and req.trigger_price is not None:
        return _dec(req.trigger_price)
    if quote is None:
        return None
    if quote.mid is not None:
        return _dec(quote.mid)
    if quote.bid is not None and quote.ask is not None:
        return (_dec(quote.bid) + _dec(quote.ask)) / Decimal(2)
    return None


def check_open(
    req: OrderRequest,
    spec: PairSpec,
    account: AccountState,
    positions: Sequence[Position],
    quotes: Mapping[str, QuoteSnapshot],
    rates: FxRates | None,
    cfg,
    now,
    daily_loss_hit: bool,
    drawdown_hit: bool,
    pairs: Mapping[str, PairSpec] | None = None,
) -> tuple[bool, str]:
    """新規开仓综合校验（纯函数）：返回 `(ok, reason_code)`。

    检查顺序（固定）：
    1. 订单基本参数（position_type/units/价格）
    2. 账户状态：halted → LOSS_CUT_PENDING → 冷却中
    3. 当日开仓笔数上限
    4. 日亏/回撤停手（由调用方预计算传入）
    5. 单货币对持仓上限 / 同时持仓数上限
    6. 双向持仓禁止（反向开仓）
    7. 亏损同向加仓禁止（逆势加仓）
    8. 保证金：单笔 ≤ 账户净值 × margin_per_order_ratio，且开仓后维持率 ≥ margin_call_ratio
    """
    # 1. 基本参数 ---------------------------------------------------------
    if req.position_type != POS_OPEN:
        return False, "INVALID_POSITION_TYPE"
    units = req.units
    if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
        return False, "INVALID_UNITS"
    if units % _UNITS_STEP != 0:
        return False, "INVALID_UNITS"
    if spec.max_units and units > int(spec.max_units):
        return False, "MAX_UNITS_EXCEEDED"
    lev = int(cfg.account.leverage)

    # 1.5 汇率：账户币种是元，缺 USDCNY/USDJPY 一律拒绝开仓（不得用假汇率）
    if rates is None:
        return False, RATE_UNAVAILABLE

    # 2. 账户状态 ---------------------------------------------------------
    if bool(account.halted):
        return False, "ACCOUNT_HALTED"
    if bool(account.loss_cut_pending):
        return False, "LOSS_CUT_PENDING"
    cooldown_until = account.loss_cut_cooldown_until
    if cooldown_until is not None and now is not None and now < cooldown_until:
        return False, "LOSS_CUT_COOLDOWN"

    # 3. 当日开仓笔数 -----------------------------------------------------
    daily_limit = int(cfg.risk.daily_trade_limit)
    if daily_limit > 0 and int(account.trades_today) >= daily_limit:
        return False, "DAILY_TRADE_LIMIT"

    # 4. 日亏 / 回撤 -------------------------------------------------------
    if daily_loss_hit:
        return False, "DAILY_LOSS_LIMIT"
    if drawdown_hit:
        return False, "MAX_DRAWDOWN"

    # 5. 数量上限 / 持仓数 -------------------------------------------------
    pair_units = sum(p.units for p in positions if p.pair == req.pair)
    pair_cap = int(spec.max_units) if spec.max_units else 0
    greedy_cap = int(cfg.risk.max_units_per_pair)
    if greedy_cap > 0:
        pair_cap = min(pair_cap, greedy_cap) if pair_cap > 0 else greedy_cap
    if pair_cap > 0 and pair_units + units > pair_cap:
        return False, "MAX_UNITS_EXCEEDED"
    max_positions = int(cfg.risk.max_positions)
    if max_positions > 0 and len(positions) >= max_positions:
        return False, "MAX_POSITIONS"

    # 6. 双向持仓禁止（P0 默认 allow_hedging=false） -------------------------
    if not bool(cfg.account.allow_hedging):
        opposite = SIDE_BUY if req.side != SIDE_BUY else "sell"
        for pos in positions:
            if pos.pair == req.pair and pos.side == opposite:
                return False, "HEDGE_NOT_ALLOWED"

    # 7. 亏损加仓（逆势加仓） ----------------------------------------------
    quote = quotes.get(req.pair) if quotes is not None else None
    for pos in positions:
        if pos.pair == req.pair and pos.side == req.side:
            ok, reason = allow_add_to_loser(pos, req, spec, quote, rates, cfg)
            if not ok:
                return False, reason

    # 8. 保证金 -----------------------------------------------------------
    est_price = _estimate_price(req, quote)
    if est_price is None or est_price <= 0:
        return False, "QUOTE_MISSING"
    # `pairs` 为可选扩展参数：引擎会传入完整映射；独立调用时用本单 spec 兜底
    # （兜底仅影响跨货币对持仓的换算精度，P0 默认单货币对为主，可接受）。
    pairs_view: dict[str, PairSpec] = dict(pairs) if pairs else {}
    pairs_view.setdefault(spec.code, spec)
    for pos in positions:
        pairs_view.setdefault(pos.pair, spec)
    try:
        equity = accounting.equity(account, positions, quotes or {}, pairs_view, rates)
        margin_total = accounting.required_margin_total(
            positions, quotes or {}, pairs_view, rates, lev
        )
    except (KeyError, ValueError):
        # 估值异常时退化为 balance 口径（保守：不因估值问题放行保证金检查）
        equity = int(account.balance)
        margin_total = 0
    new_margin = accounting.required_margin(spec, units, est_price, rates, lev)

    per_order_ratio = _dec(cfg.risk.margin_per_order_ratio)
    if per_order_ratio > 0 and equity > 0:
        if Decimal(new_margin) > Decimal(equity) * per_order_ratio:
            return False, "MARGIN_PER_ORDER_EXCEEDED"
    target_ratio = _dec(getattr(cfg.account, "margin_call_ratio", 100.0))
    required_equity = Decimal(margin_total + new_margin) * target_ratio / Decimal(100)
    if Decimal(equity) < required_equity:
        return False, "INSUFFICIENT_MARGIN"
    return True, ""
