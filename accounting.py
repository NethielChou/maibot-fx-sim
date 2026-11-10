"""账户核算纯函数（账户币种=虚拟人民币/元）：持仓額 / 所需保证金 / 估值損益 / 净值 / 维持率 / 隔夜利息 / 已实现損益。

本模块**不触达任何 I/O**（不读写 SQLite、不拉行情、不 import 宿主 SDK），
只接受 `models.py` 的纯数据对象、`fxconv.FxRates` 与 `pairs` 映射，便于离线单测与确定性重放。

换算口径（v0.3.0，账户币种改为虚拟元）
--------------------------------------
- ``cny_per_usd = USDCNY 中间价``
- ``jpy_per_cny = USDJPY 中间价 / USDCNY 中间价``（1 元 = 多少日元）
- JPY 报价货币对：``元 = 日元金额 / jpy_per_cny``
- USD 报价货币对（EURUSD）：``元 = 美元金额 × cny_per_usd``
- 缺 USDCNY/USDJPY 时抛 :class:`fxconv.RateUnavailable`（原因码 ``RATE_UNAVAILABLE``），
  **不得用假汇率**；调用方据此拒绝新开仓 / 挂起估值。

关键公式
--------
- 持仓額（元） = 数量 × 1 单位基础货币的元值（**中间价**）
- 所需保证金（元） = ceil(持仓額 / 杠杆)   ← 元未満**向上取整**（保守）
- 双向持仓（同货币对多空并存） = ceil(max(Σ多持仓額, Σ空持仓額) / 杠杆)，跨货币对相加
- 浮动盈亏（元）: 多单用 **bid**、空单用 **ask** 评估，再换算成元
- 账户净值（元） = balance + Σ估值損益（**逐持仓先四舍五入到 1 元** 再汇总）
- 维持率（%） = 账户净值 / 所需保证金合計 × 100（无持仓返回 None，分母 0 不判定）
- 强制平仓判定 = `equity × 100 < margin_total × ratio`（**整数交叉乘法**，恰好 50% 不触发）
- 隔夜利息（元） = round_half_up(隔夜利息单価(日元/1万/日) × units/10,000 × days ÷ jpy_per_cny)
- 已实现損益（元） = dir × (平仓价 − 持仓价) × units 换算成元

取整口径：一切金额最终落 **int 元**，Decimal 运算后 `ROUND_HALF_UP`（所需保证金用
`ROUND_CEILING`）。行情或汇率缺失时，`unrealized_pnl` 返回 0 / 估值函数按「0 浮动盈亏 /
持仓原价」退化处理，仅为启动恢复期展示服务；**撮合前必须由调用方保证报价与汇率可用**
（见 `orders.py` / `scheduler.py`）。
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

try:  # 插件包内加载（宿主以包形式导入 plugin.py 时）
    from .fxconv import (  # type: ignore[import-not-found]
        FxRates,
        RateUnavailable,
        require_rates,
    )
    from .models import (  # type: ignore[import-not-found]
        SIDE_BUY,
        AccountState,
        PairSpec,
        Position,
        QuoteSnapshot,
    )
except ImportError:  # 项目根直接加载（单测 / 脚本 / python -m unittest）
    from fxconv import (  # type: ignore[no-redef]
        FxRates,
        RateUnavailable,
        require_rates,
    )
    from models import (  # type: ignore[no-redef]
        SIDE_BUY,
        AccountState,
        PairSpec,
        Position,
        QuoteSnapshot,
    )

__all__ = [
    "RateUnavailable",
    "ceil_money",
    "close_pnl",
    "commission",
    "equity",
    "loss_cut_triggered",
    "maintenance_ratio",
    "notional",
    "required_margin",
    "required_margin_total",
    "round_money",
    "swap_amount",
    "to_account_rate",
    "unrealized_pnl",
]

# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _dec(value: Decimal | int | float | str, code: str = "INVALID_DECIMAL") -> Decimal:
    """把 Decimal/int/float/str 统一转换成 Decimal；float 走 str 避免二进制误差。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool 是 int 子类，显式拒绝
        raise ValueError(f"{code}: bool is not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError(f"{code}: unsupported type {type(value)!r}")


def round_money(value: Decimal) -> int:
    """元金额 → 整数元（ROUND_HALF_UP，四舍五入）。"""
    return int(_dec(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ceil_money(value: Decimal) -> int:
    """元金额 → 整数元（ROUND_CEILING，元以下向上取整；所需保证金/手续费口径）。"""
    return int(_dec(value).to_integral_value(rounding=ROUND_CEILING))


def to_account_rate(
    spec: PairSpec,
    price: Decimal,
    rates: FxRates | None,
) -> Decimal:
    """1 单位基础货币的元值（账户币种汇率）：委托 :meth:`fxconv.FxRates.account_rate`。

    - JPY 报价货币对：``price / jpy_per_cny``
    - USD 报价货币对（EURUSD）：``price × cny_per_usd``
    - 缺汇率 → :class:`RateUnavailable`
    """
    price_d = _dec(price, "INVALID_PRICE")
    if price_d <= 0:
        raise ValueError("INVALID_PRICE: price must be > 0")
    return require_rates(rates).account_rate(spec, price_d)


def _validate_units(units: int) -> int:
    """校验并返回正整数手数；非法抛 `ValueError("INVALID_UNITS")`。"""
    if isinstance(units, bool) or not isinstance(units, int):
        raise ValueError("INVALID_UNITS: units must be int")
    if units <= 0:
        raise ValueError("INVALID_UNITS: units must be > 0")
    return units


def _validate_leverage(leverage: int) -> Decimal:
    """校验杠杆；非法抛 `ValueError("INVALID_LEVERAGE")`。"""
    if isinstance(leverage, bool) or not isinstance(leverage, int):
        raise ValueError("INVALID_LEVERAGE: leverage must be int")
    if leverage <= 0:
        raise ValueError("INVALID_LEVERAGE: leverage must be > 0")
    return Decimal(leverage)


def _quote_mid(quote: QuoteSnapshot | None, spec: PairSpec) -> Decimal | None:
    """取报价仲值：优先 mid 字段，其次 (bid+ask)/2；缺失返回 None。"""
    if quote is None:
        return None
    if quote.mid is not None and quote.mid > 0:
        return quote.mid
    if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
        return (quote.bid + quote.ask) / Decimal(2)
    return None


def _fallback_rate(
    spec: PairSpec,
    positions: Sequence[Position],
    rates: FxRates | None,
) -> Decimal | None:
    """无行情时的退化元汇率：按持仓开仓价加权平均（仅供启动恢复展示）。"""
    if not positions or rates is None:
        return None
    total_units = sum(p.units for p in positions)
    if total_units <= 0:
        return None
    weighted = sum((_dec(p.entry_price) * p.units for p in positions), Decimal(0))
    avg_price = weighted / Decimal(total_units)
    return to_account_rate(spec, avg_price, rates)


# ---------------------------------------------------------------------------
# 持仓額 / 所需保证金
# ---------------------------------------------------------------------------


def notional(spec: PairSpec, units: int, price: Decimal, rates: FxRates | None) -> Decimal:
    """持仓額（元，未取整） = 数量 × 1 单位基础货币的元值（中间价）。

    保留小数（判定/显示口径分别处理），**不做四舍五入**。
    """
    units = _validate_units(units)
    return Decimal(units) * to_account_rate(spec, price, rates)


def required_margin(
    spec: PairSpec,
    units: int,
    price: Decimal,
    rates: FxRates | None,
    leverage: int,
) -> int:
    """所需保证金（整数元） = ceil(持仓額 / 杠杆)。

    元以下向上取整（保守）。
    """
    lev = _validate_leverage(leverage)
    value = notional(spec, units, price, rates)
    return ceil_money(value / lev)


def required_margin_total(
    positions: Sequence[Position],
    quotes: Mapping[str, QuoteSnapshot],
    pairs: Mapping[str, PairSpec],
    rates: FxRates | None,
    leverage: int,
) -> int:
    """所需保证金合計（整数元）。

    口径：
    - 同一货币对：`ceil(max(Σ多持仓額, Σ空持仓額) / 杠杆)`（双向持仓取大边）
    - 不同货币对：各自计算后**相加**（不取大边）
    - 持仓額按**当前中间价**重算（无行情时退化为开仓价，仅恢复期展示用）
    """
    lev = _validate_leverage(leverage)
    if not positions:
        return 0
    require_rates(rates)

    by_pair: dict[str, list[Position]] = {}
    for pos in positions:
        by_pair.setdefault(pos.pair, []).append(pos)

    total = Decimal(0)
    for pair_code, group in by_pair.items():
        spec = pairs.get(pair_code)
        if spec is None:
            raise ValueError(f"UNKNOWN_PAIR: {pair_code}")
        quote = quotes.get(pair_code)
        mid = _quote_mid(quote, spec)
        if mid is not None:
            rate = to_account_rate(spec, mid, rates)
        else:
            rate = _fallback_rate(spec, group, rates)
            if rate is None:
                continue  # 无任何可用价格（空组不可能），安全跳过
        long_units = sum(p.units for p in group if p.side == SIDE_BUY)
        short_units = sum(p.units for p in group if p.side != SIDE_BUY)
        long_notional = Decimal(long_units) * rate
        short_notional = Decimal(short_units) * rate
        side_max = long_notional if long_notional >= short_notional else short_notional
        total += side_max
    return ceil_money(total / lev)


# ---------------------------------------------------------------------------
# 估值損益 / 账户净值 / 维持率 / 强制平仓
# ---------------------------------------------------------------------------


def _pnl(
    spec: PairSpec,
    side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    units: int,
    rates: FxRates | None,
) -> int:
    """通用元損益：dir × (exit − entry) × units 换算成元（四舍五入到 1 元）。"""
    units = _validate_units(units)
    entry = _dec(entry_price, "INVALID_PRICE")
    exit_d = _dec(exit_price, "INVALID_PRICE")
    rate_set = require_rates(rates)
    direction = Decimal(1) if side == SIDE_BUY else Decimal(-1)
    quote_currency = str(getattr(spec, "quote_currency", "") or "").upper()
    if quote_currency == "JPY":
        raw = direction * (exit_d - entry) * Decimal(units) / rate_set.jpy_per_cny
    elif quote_currency == "USD":
        raw = direction * (exit_d - entry) * Decimal(units) * rate_set.cny_per_usd
    else:
        raise ValueError(f"UNSUPPORTED_QUOTE_CURRENCY: {quote_currency!r}")
    return round_money(raw)


def unrealized_pnl(
    pos: Position,
    quote: QuoteSnapshot,
    spec: PairSpec,
    rates: FxRates | None,
) -> int:
    """浮动盈亏（整数元）：多单用 **bid**、空单用 **ask** 评估。

    - 报价缺失/该侧价格缺失 → 返回 0（调用方应在撮合前判定行情可用；此处仅为展示兜底）
    - 缺汇率 → 抛 :class:`RateUnavailable`（不允许用假汇率）
    """
    if quote is None:
        return 0
    price = quote.bid if pos.side == SIDE_BUY else quote.ask
    if price is None or price <= 0:
        return 0
    return _pnl(spec, pos.side, pos.entry_price, price, pos.units, rates)


def equity(
    account: AccountState,
    positions: Sequence[Position],
    quotes: Mapping[str, QuoteSnapshot],
    pairs: Mapping[str, PairSpec],
    rates: FxRates | None,
) -> int:
    """账户净值（整数元） = balance + Σ浮动盈亏（逐持仓先取整到 1 元再汇总）。"""
    total = int(account.balance)
    for pos in positions:
        spec = pairs.get(pos.pair)
        if spec is None:
            raise ValueError(f"UNKNOWN_PAIR: {pos.pair}")
        total += unrealized_pnl(pos, quotes.get(pos.pair), spec, rates)
    return total


def maintenance_ratio(equity: int, margin_total: int) -> Decimal | None:
    """保证金维持率（%，显示 2 位小数）；无持仓（分母 0）返回 None。

    **判定不要用本函数**：强制平仓判定请用 `loss_cut_triggered()` 的整数交叉乘法。
    """
    if margin_total is None or margin_total <= 0:
        return None
    ratio = (Decimal(int(equity)) * Decimal(100)) / Decimal(int(margin_total))
    return ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def loss_cut_triggered(equity: int, margin_total: int, ratio: Decimal | float | str = Decimal("50")) -> bool:
    """强制平仓判定（整数严格比较）：`equity × 100 < margin_total × ratio`。

    - 恰好等于 ratio（如恰好 50.00%）→ **不触发**（严格小于）
    - 无持仓（margin_total ≤ 0）→ 不判定
    - 用整数交叉乘法避免浮点误差；`ratio` 支持 Decimal/float/str
    """
    margin = int(margin_total)
    if margin <= 0:
        return False
    ratio_d = _dec(ratio, "INVALID_RATIO")
    if ratio_d <= 0:
        raise ValueError("INVALID_RATIO: ratio must be > 0")
    return Decimal(int(equity)) * Decimal(100) < Decimal(margin) * ratio_d


# ---------------------------------------------------------------------------
# 隔夜利息 / 已实现損益 / 手续费
# ---------------------------------------------------------------------------


def swap_amount(pos: Position, spec: PairSpec, days: int, rates: FxRates | None) -> int:
    """隔夜利息付与額（整数元） = round_half_up(单価(日元/1万/日) × units/10,000 × days ÷ jpy_per_cny)。

    - 单价：多单 `swap_long_jpy`、空单 `swap_short_jpy`（EURUSD 也已在 PairSpec 存为
      日元口径的近似值：long −101 / short +55），再按 ``jpy_per_cny`` 换算成元。
    - 缺汇率 → :class:`RateUnavailable`（由调度层跳过本次计息并播报）。
    """
    units = _validate_units(pos.units)
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError("INVALID_DAYS: days must be int")
    if days < 0:
        raise ValueError("INVALID_DAYS: days must be >= 0")
    rate_set = require_rates(rates)
    unit = spec.swap_long_jpy if pos.side == SIDE_BUY else spec.swap_short_jpy
    raw_yen = _dec(unit, "INVALID_SWAP") * Decimal(units) / Decimal(10_000) * Decimal(days)
    return round_money(raw_yen / rate_set.jpy_per_cny)


def close_pnl(
    pos: Position,
    exit_price: Decimal,
    spec: PairSpec,
    rates: FxRates | None,
) -> int:
    """已实现損益（整数元） = dir × (平仓价 − 持仓价) × units 换算成元。"""
    return _pnl(spec, pos.side, pos.entry_price, exit_price, pos.units, rates)


def commission(units: int, commission_per_10k: Decimal | float | str) -> int:
    """单边手续费（整数元） = ceil(commission_per_10k × units / 10,000)。

    默认 `commission_per_10k = 0` → 0（点差已内含成本，禁止双重计费）。
    """
    units = _validate_units(units)
    rate = _dec(commission_per_10k, "INVALID_COMMISSION")
    if rate <= 0:
        return 0
    return ceil_money(rate * Decimal(units) / Decimal(10_000))
