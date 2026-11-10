"""人民币（虚拟元）换算：USDCNY / USDJPY 两条真实行情锚 → 账户币种金额。

口径（v0.3.0）
--------------
- 账户币种 = 虚拟人民币（元，CNY）；行情仍是真实世界行情。
- ``cny_per_usd = USDCNY 中间价``（新浪 ``fx_susdcny``，在岸人民币）。
- ``jpy_per_cny = USDJPY 中间价 / USDCNY 中间价``（1 元 = 多少日元）。
- JPY 报价货币对（USDJPY/EURJPY/GBPJPY/AUDJPY/TRYJPY/MXNJPY/ZARJPY）：
  ``元 = 日元金额 / jpy_per_cny``；
- USD 报价货币对（EURUSD）：``元 = 美元金额 × cny_per_usd``。
- 缺任一汇率时**不得用假汇率**：需要换算的函数抛 :class:`RateUnavailable`
  （原因码 ``RATE_UNAVAILABLE``），由调用方拒绝新开仓 / 挂起估值。

本模块纯函数、无 I/O；只依赖 ``models.PairSpec`` 与 ``models.QuoteSnapshot`` 的字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from models import PairSpec, QuoteSnapshot

#: 换算锚的原因码（订单被拒绝 / 日志里展示）。
RATE_UNAVAILABLE = "RATE_UNAVAILABLE"

#: 人民币换算锚：新浪在岸 USDCNY（不是可交易品种）。
USDCNY_CODE = "USDCNY"
USDCNY_SINA_CODE = "fx_susdcny"


class RateUnavailable(ValueError):
    """缺少 USDCNY / USDJPY 汇率，无法换算为人民币（消息带 ``RATE_UNAVAILABLE``）。"""


def _dec(value: Any) -> Decimal:
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


def quote_mid(quote: QuoteSnapshot | None) -> Decimal | None:
    """取报价中间价：优先 ``mid``，其次 ``(bid+ask)/2``；缺失/非正返回 ``None``。"""

    if quote is None:
        return None
    mid = getattr(quote, "mid", None)
    if mid is not None and mid > 0:
        return _dec(mid)
    bid = getattr(quote, "bid", None)
    ask = getattr(quote, "ask", None)
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (_dec(bid) + _dec(ask)) / Decimal(2)
    return None


@dataclass(frozen=True)
class FxRates:
    """两条换算锚（都必须是正数）。"""

    usdjpy_mid: Decimal
    usdcny_mid: Decimal

    def __post_init__(self) -> None:
        usdjpy = _dec(self.usdjpy_mid)
        usdcny = _dec(self.usdcny_mid)
        if usdjpy <= 0:
            raise ValueError("INVALID_USDJPY_MID: usdjpy_mid must be > 0")
        if usdcny <= 0:
            raise ValueError("INVALID_USDCNY_MID: usdcny_mid must be > 0")
        object.__setattr__(self, "usdjpy_mid", usdjpy)
        object.__setattr__(self, "usdcny_mid", usdcny)

    # ---------- 基础汇率 ----------

    @property
    def cny_per_usd(self) -> Decimal:
        """1 美元 = 多少元。"""

        return self.usdcny_mid

    @property
    def jpy_per_cny(self) -> Decimal:
        """1 元 = 多少日元（= USDJPY / USDCNY）。"""

        return self.usdjpy_mid / self.usdcny_mid

    def jpy_to_cny(self, amount: Decimal | int | float | str) -> Decimal:
        """日元金额 → 元（未取整）。"""

        return _dec(amount) / self.jpy_per_cny

    def usd_to_cny(self, amount: Decimal | int | float | str) -> Decimal:
        """美元金额 → 元（未取整）。"""

        return _dec(amount) * self.cny_per_usd

    # ---------- 按品种 ----------

    def account_rate(self, spec: PairSpec, price: Decimal | int | float | str) -> Decimal:
        """1 单位基础货币值多少元（价格 → 账户币种汇率）。

        - JPY 报价：``price / jpy_per_cny``
        - USD 报价：``price × cny_per_usd``
        """

        price_d = _dec(price)
        if price_d <= 0:
            raise ValueError("INVALID_PRICE: price must be > 0")
        quote_currency = str(getattr(spec, "quote_currency", "") or "").upper()
        if quote_currency == "JPY":
            return price_d / self.jpy_per_cny
        if quote_currency == "USD":
            return price_d * self.cny_per_usd
        raise ValueError(f"UNSUPPORTED_QUOTE_CURRENCY: {quote_currency!r}")

    def pip_value(self, spec: PairSpec, units: int = 1) -> Decimal:
        """1 pip 的价值（元）＝ pip × 1 单位基础货币的元值 × units。"""

        pip = _dec(getattr(spec, "pip", 0))
        if pip <= 0:
            raise ValueError("INVALID_PIP: spec.pip must be > 0")
        return pip * self.account_rate(spec, Decimal(1)) * Decimal(int(units))


def rates_from_quotes(quotes: Mapping[str, QuoteSnapshot] | None) -> FxRates | None:
    """从行情字典里取 ``USDJPY`` / ``USDCNY`` 中间价；任一缺失返回 ``None``。"""

    if not quotes:
        return None
    usdjpy = quote_mid(quotes.get("USDJPY"))
    usdcny = quote_mid(quotes.get(USDCNY_CODE))
    if usdjpy is None or usdcny is None or usdjpy <= 0 or usdcny <= 0:
        return None
    return FxRates(usdjpy_mid=usdjpy, usdcny_mid=usdcny)


def require_rates(rates: FxRates | None) -> FxRates:
    """校验汇率可用；缺失抛 :class:`RateUnavailable`。"""

    if rates is None:
        raise RateUnavailable(
            f"{RATE_UNAVAILABLE}：缺少 USDCNY 或 USDJPY 汇率，无法把金额换算成元"
        )
    return rates
