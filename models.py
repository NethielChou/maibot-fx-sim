"""共享数据模型：麦麦炒外汇 P0。

所有金额/价格一律 Decimal；账本金额以「整数元（int，虚拟人民币）」存储与比较。
行情仍是真实世界行情（含 USDJPY/USDCNY 换算锚），账户币种为虚拟元。
本文件由 Lead 维护，各模块只读引用，不修改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

# ---------- 订单/成交状态常量 ----------
STATUS_CREATED = "created"
STATUS_PENDING = "pending"
STATUS_TRIGGERED = "triggered"
STATUS_FILLED = "filled"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_REJECTED = "rejected"

SIDE_BUY = "buy"
SIDE_SELL = "sell"
POS_OPEN = "open"
POS_CLOSE = "close"
ORDER_MARKET = "market"
ORDER_LIMIT = "limit"
ORDER_STOP = "stop"
ORDER_TRAILING = "trailing_stop"


@dataclass(frozen=True)
class PairSpec:
    """品种规格（日式零售 FX）。价格精度用 digits/tick；pip 仅用于显示与点差单位。"""

    code: str                 # USDJPY
    name_cn: str              # 美元/日元
    digits: int               # 3 / 4 / 5
    tick: Decimal             # 0.001 / 0.0001 / 0.00001
    pip: Decimal              # JPY 货币对 0.01；EURUSD 0.0001
    quote_currency: str       # JPY / USD
    sina_code: str            # fx_susdjpy
    source_kind: str          # "raw"（有真实买卖价） / "computed"（计算品种，用合成点差）
    base_spread_pips: Decimal # 基准点差（pips）
    max_spread_pips: Decimal  # 点差上限（pips）
    swap_long_jpy: Decimal    # 日元/1万单位/日，正=收取（市场数据，日元口径）
    swap_short_jpy: Decimal
    max_units: int            # 单品种持仓上限（货币）

    def pip_value_jpy(self, units: int, usdjpy_mid: Decimal) -> Decimal:
        """1 pip 价值（日元）：JPY 货币对 = pip × units；EURUSD = pip × units × USDJPY。"""
        if self.quote_currency == "JPY":
            return self.pip * Decimal(units)
        return self.pip * Decimal(units) * usdjpy_mid


@dataclass
class QuoteSnapshot:
    pair: str
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    quote_ts: datetime | None          # 源行情时间（含时区）
    recv_ts: datetime                  # 插件收到时间（UTC）
    source: str = "sina"
    seq: int = 0

    def spread_pips(self, spec: PairSpec) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / spec.pip


@dataclass
class OrderRequest:
    pair: str
    side: str                          # buy / sell
    position_type: str                 # open / close
    order_type: str                    # market / limit / stop
    units: int                         # 货币，1,000 整数倍
    client_order_id: str = ""
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    time_in_force: str = "GTC"         # GTC / DAY / GTD
    expires_at: datetime | None = None
    position_id: str | None = None     # close 目标持仓（可空 = FIFO）
    oco_group_id: str | None = None
    decision_id: str | None = None
    created_by: str = ""               # qq:xxx
    reason: str = ""


@dataclass
class Order:
    order_id: str
    request: OrderRequest
    status: str = STATUS_CREATED
    filled_price: Decimal | None = None
    filled_units: int = 0
    slippage_pips: Decimal = Decimal("0")
    gap_pips: Decimal = Decimal("0")
    reject_reason: str = ""
    suspend_reason: str = ""
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    trade_id: str | None = None


@dataclass
class Fill:
    order_id: str
    pair: str
    side: str
    position_type: str
    units: int
    price: Decimal
    slippage_pips: Decimal
    fee: int
    realized_pnl: int
    position_id: str | None
    ts: datetime
    decision_id: str | None = None


@dataclass
class Position:
    position_id: str
    pair: str
    side: str                          # buy=多头 / sell=空头
    units: int
    entry_price: Decimal
    opened_at: datetime
    margin: int = 0                    # 最近一次计算值（元；显示用；判定用实时重算）
    swap_accrued: int = 0              # 累计隔夜利息（元）
    stop_price: Decimal | None = None
    take_price: Decimal | None = None

    @property
    def direction(self) -> int:
        return 1 if self.side == SIDE_BUY else -1


@dataclass
class AccountState:
    balance: int = 100_000            # 现金余额（元）
    initial_balance: int = 100_000    # 累计本金（元）
    realized_pnl: int = 0             # 已实现盈亏（元）
    fees_paid: int = 0                # 累计手续费（元）
    swap_paid: int = 0                # 累计隔夜利息（元）
    day: str = ""                       # 日切日期（NY 当地 YYYY-MM-DD）
    trades_today: int = 0
    day_start_equity: int = 100_000
    peak_equity: int = 100_000
    halted: bool = False
    loss_cut_pending: bool = False
    loss_cut_cooldown_until: datetime | None = None
    schema_version: int = 2


@dataclass
class FillResult:
    """一次订单撮合的结果（订单引擎返回给调度层）。"""

    fills: list[Fill] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (order_id, reason)
    suspended: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DecisionIntent:
    pair: str
    action: str                        # open_long / open_short / close / close_all / hold
    order_type: str = ORDER_MARKET     # market / limit / stop
    units: int = 10_000
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    reason: str = ""
    speech: str = ""


def money_to_int(value: Decimal) -> int:
    """元金额 → 整数元（ROUND_HALF_UP）。"""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
