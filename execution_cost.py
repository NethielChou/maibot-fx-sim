"""执行成本：报价可用性 / 有效点差合成 / 滑点（P0 行情层收口）。

本模块把「源快照」变成「可成交的 bid/ask + 滑点」，公式口径来自
``tmp/forex-design/margin-swap-spec.md`` Rev 1.1 §4/§5、
``order-engine-spec.md`` §4.3/§4.4，以及 ``docs/INTERFACES.md``。

核心公式
--------
点差（Rev 1.1 §4.1）::

    mid = (源 bid + 源 ask) / 2                     # 一律用源中间价
    基准点差(pips) = 直源: max(源点差, base_spread_pips)   # 不允许比源更窄
                     计算: base_spread_pips                 # 源点差是幻影，不可信
    有效点差(pips) = 基准点差 × max(时间窗系数) × 波动系数(1+P0 未实现)
    时间系数 = max(命中的 rollover / 周初 / 指标窗)，总倍率 ≤ cfg.spread.max_multiplier
    有效 bid/ask = mid ∓ 有效点差 × pip / 2，向外对齐到 tick

滑点（§5.1）::

    滑点(pips) = base[order_type] + jitter(0 ~ jitter_max)，方向由订单引擎按买卖取反
    market 0.2 / stop 0.5 / rocut 1.5 / limit 0

报价可用性优先级（order-engine-spec §4.3，**第一个命中即停**）
--------------------------------------------------------------
``QUOTE_MISSING`` → ``MARKET_CLOSED`` → ``BAD_QUOTE`` → ``STALE_QUOTE`` →
``STALE_QUOTE_FILL`` → ``SPREAD_TOO_WIDE`` → OK

- **CLOSED 优先于 STALE**：周末僵尸时间戳会同时满足 STALE，但对外必须报休市。
- EURUSD 是**两条腿**的品种：必须同时检查 ``USDJPY`` 可用性（日元换算 + 基础货币
  流动性），否则行情/估值会错位。
- ``stale_quote_seconds`` 检查**行情数据时间**（``quote_ts``，源时间戳）；
  ``stale_fill_seconds`` 检查**快照接收时间**（``recv_ts``，本插件抓取时刻）。
  两者任一超限都返回不可用，但原因码不同：前者 ``STALE_QUOTE``（数据过期），
  后者 ``STALE_QUOTE_FILL``（快照不新，禁止撮合，等待下一 tick）。调用方若只想
  用一档阈值，可把两个参数传同一个值。

约定：金额/价格一律 ``Decimal``；``cfg`` 传入任意具备 ``config.MaiFxConfig``
字段结构的对象（本模块不 import ``config``，因此不依赖 ``maibot_sdk``）。
"""

from __future__ import annotations

import random
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from models import PairSpec, QuoteSnapshot
import market_clock as market_clock_module

#: 原因码（与 order-engine-spec §5.3 枚举名对齐，第二个腿附带 ``:USDJPY`` 后缀）。
REASON_QUOTE_MISSING = "QUOTE_MISSING"
REASON_MARKET_CLOSED = "MARKET_CLOSED"
REASON_BAD_QUOTE = "BAD_QUOTE"
REASON_STALE_QUOTE = "STALE_QUOTE"
REASON_STALE_QUOTE_FILL = "STALE_QUOTE_FILL"
REASON_SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
REASON_OK = ""

#: 滑点基准（pip）：见 Rev 1.1 §5.1 / order-engine-spec §4.4。
SLIPPAGE_BASE_PIPS: dict[str, Decimal] = {
    "market": Decimal("0.2"),
    "stop": Decimal("0.5"),
    "trailing_stop": Decimal("0.5"),
    "rocut": Decimal("1.5"),  # 强制平仓：1.0 × 压力系数 1.5
    "loss_cut": Decimal("1.5"),  # 同上，别名
    "limit": Decimal("0"),  # 限价不劣化，绝不滑价成交
}

#: 滑点 jitter 上界（pip）：order-engine-spec §4.4 默认表。
SLIPPAGE_JITTER_MAX_PIPS: dict[str, Decimal] = {
    "market": Decimal("0.3"),
    "stop": Decimal("0.5"),
    "trailing_stop": Decimal("0.5"),
    "rocut": Decimal("0.5"),
    "loss_cut": Decimal("0.5"),
    "limit": Decimal("0"),
}

#: 需要第二腿 USDJPY 行情的品种（EURUSD 的日元换算 + 美元流动性依赖）。
_LEG_PAIRS: dict[str, tuple[str, ...]] = {"EURUSD": ("USDJPY",)}

_ZERO = Decimal("0")
_TWO = Decimal("2")


# ---------------------------------------------------------------------------
# Decimal 小工具
# ---------------------------------------------------------------------------
def _dec(value: Any) -> Decimal:
    """把配置里的 float/int/str 统一转 Decimal（float 走 str 再解析，避免二进制误差）。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _age_seconds(ts: Any, now: Any) -> Decimal | None:
    """``ts`` 相对 ``now`` 的年龄（秒，非负；未来时间按 0 处理）；无效返回 ``None``。"""
    from datetime import datetime, timezone

    if not isinstance(ts, datetime) or not isinstance(now, datetime):
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None or ts.utcoffset() is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (now - ts).total_seconds()
    if age < 0:
        return _ZERO
    return Decimal(str(age))


# ---------------------------------------------------------------------------
# tick 对齐
# ---------------------------------------------------------------------------
def round_up_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """向"更不利的一侧"（价格更高）对齐到 tick。"""
    return value.quantize(tick, rounding=ROUND_CEILING)


def round_down_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """向"更不利的一侧"（价格更低）对齐到 tick。"""
    return value.quantize(tick, rounding=ROUND_FLOOR)


def is_tick_aligned(value: Decimal, spec: PairSpec) -> bool:
    """价格是否对齐到品种最小变动单位（``tick``）。"""
    return value.quantize(spec.tick) == value


# ---------------------------------------------------------------------------
# 报价可用性
# ---------------------------------------------------------------------------
def _basic_quote_ok(quote: QuoteSnapshot | None) -> bool:
    """快照存在且 bid/ask 均为正值。"""
    if quote is None:
        return False
    if quote.bid is None or quote.ask is None:
        return False
    return quote.bid > _ZERO and quote.ask > _ZERO


def _abnormal_reason(quote: QuoteSnapshot, spec: PairSpec) -> str:
    """脏数据判定（返回原因码后缀，空串=正常）。"""
    assert quote.bid is not None and quote.ask is not None
    if quote.ask < quote.bid:
        return "ASK_LT_BID"
    spread = quote.ask - quote.bid
    if spec.source_kind == "raw" and spread <= _ZERO:
        # 直源必须有真实点差；计算品种允许 bid==ask（点差由本模块合成）
        return "ZERO_SPREAD"
    if not is_tick_aligned(quote.bid, spec) or not is_tick_aligned(quote.ask, spec):
        return "TICK_MISALIGNED"
    return ""


def quote_usable(
    spec: PairSpec,
    quotes: dict[str, QuoteSnapshot],
    now: Any,
    stale_quote_seconds: int,
    stale_fill_seconds: int,
) -> tuple[bool, str]:
    """判定某品种当前是否可用于估值/下单/撮合，返回 ``(ok, reason)``。

    判定顺序（第一个命中即停）：

    1. ``QUOTE_MISSING``：无快照 / bid/ask 缺失 / 非正（EURUSD 还会检查 USDJPY 腿）。
    2. ``MARKET_CLOSED``：休市（**优先于 STALE**，周末僵尸时间戳报休市不报过期）。
    3. ``BAD_QUOTE``：``ask < bid``、直源 0 点差、价格未对齐 tick；
       第二腿同样判定，原因码带后缀（如 ``BAD_QUOTE:USDJPY``）。
    4. ``STALE_QUOTE``：源行情时间 ``quote_ts`` 缺失或超过 ``stale_quote_seconds``。
    5. ``STALE_QUOTE_FILL``：快照接收时间 ``recv_ts`` 超过 ``stale_fill_seconds``
       （数据没过期但快照太旧，禁止本 tick 撮合，应由调度层等待下一次抓取）。
    6. ``SPREAD_TOO_WIDE``：直源品种源点差已超过 ``spec.max_spread_pips``
       （配置化的"有效点差上限"由调用方用 :func:`effective_spread_pips` 判定）。
    7. ``(True, "")``：可用。

    函数不修改任何入参；同一 tick 内可对每个品种各调一次。
    """
    # --- 1) 存在性 ---------------------------------------------------------
    quote = quotes.get(spec.code)
    if not _basic_quote_ok(quote):
        return False, REASON_QUOTE_MISSING
    assert quote is not None

    leg_quotes: list[tuple[str, QuoteSnapshot]] = []
    for leg_code in _LEG_PAIRS.get(spec.code, ()):
        leg = quotes.get(leg_code)
        if not _basic_quote_ok(leg):
            return False, f"{REASON_QUOTE_MISSING}:{leg_code}"
        assert leg is not None
        leg_quotes.append((leg_code, leg))

    # --- 2) 休市（必须先于 STALE） -----------------------------------------
    try:
        market_open = market_clock_module.is_market_open(now)
    except (TypeError, ValueError):
        # now 不合法（naive）→ 保守视为休市，拒绝成交
        return False, REASON_MARKET_CLOSED
    if not market_open:
        return False, REASON_MARKET_CLOSED

    # --- 3) 脏数据 ---------------------------------------------------------
    abnormal = _abnormal_reason(quote, spec)
    if abnormal:
        return False, f"{REASON_BAD_QUOTE}:{abnormal}"
    for leg_code, leg in leg_quotes:
        try:
            leg_spec = _leg_spec(leg_code)
        except KeyError:
            return False, f"{REASON_QUOTE_MISSING}:{leg_code}"
        leg_abnormal = _abnormal_reason(leg, leg_spec)
        if leg_abnormal:
            return False, f"{REASON_BAD_QUOTE}:{leg_code}"

    # --- 4) 数据时间过期 ---------------------------------------------------
    quote_age = _age_seconds(quote.quote_ts, now)
    if quote_age is None or quote_age > _dec(stale_quote_seconds):
        return False, REASON_STALE_QUOTE
    for leg_code, leg in leg_quotes:
        leg_age = _age_seconds(leg.quote_ts, now)
        if leg_age is None or leg_age > _dec(stale_quote_seconds):
            return False, f"{REASON_STALE_QUOTE}:{leg_code}"

    # --- 5) 快照接收时间过旧（禁止撮合） -----------------------------------
    recv_age = _age_seconds(quote.recv_ts, now)
    if recv_age is None or recv_age > _dec(stale_fill_seconds):
        return False, REASON_STALE_QUOTE_FILL
    for leg_code, leg in leg_quotes:
        leg_recv_age = _age_seconds(leg.recv_ts, now)
        if leg_recv_age is None or leg_recv_age > _dec(stale_fill_seconds):
            return False, f"{REASON_STALE_QUOTE_FILL}:{leg_code}"

    # --- 6) 源点差绝对上限 -------------------------------------------------
    if spec.source_kind == "raw":
        spread_pips = (quote.ask - quote.bid) / spec.pip
        if spread_pips > spec.max_spread_pips:
            return False, REASON_SPREAD_TOO_WIDE

    return True, REASON_OK


def _leg_spec(code: str) -> PairSpec:
    """取第二腿品种规格（延迟 import 避免循环依赖）。"""
    from pairs import get_pair

    return get_pair(code)


# ---------------------------------------------------------------------------
# 有效点差
# ---------------------------------------------------------------------------
def base_spread_pips(spec: PairSpec, quote: QuoteSnapshot) -> Decimal:
    """基准点差（pips，未乘时间窗）：直源 ``max(源点差, 配置基准)``；计算品种用配置基准。

    直源源点差缺失/非正（脏数据，调用方应先过 :func:`quote_usable`）时退回配置基准。
    """
    if spec.source_kind == "raw":
        bid, ask = quote.bid, quote.ask
        if bid is not None and ask is not None and ask > bid > _ZERO:
            source_spread = (ask - bid) / spec.pip
            return max(source_spread, spec.base_spread_pips)
        return spec.base_spread_pips
    return spec.base_spread_pips


def _news_multiplier(now: Any, cfg: Any, market_clock: Any) -> Decimal | None:
    """指标窗倍率（P0 默认关闭：``cfg.calendar.enabled=False``）。

    约定：若传入的 ``market_clock`` 对象暴露 ``news_multiplier(ts)`` 回调，且
    ``cfg.calendar.enabled`` 为真，则调用它；返回值可以是：

    - ``"core"`` / ``"edge"`` → 分别取 ``cfg.spread.news_core_multiplier`` /
      ``cfg.spread.news_edge_multiplier``；
    - ``None`` → 不在指标窗；
    - 数值 → 直接作为倍率。

    P1 接入经济日历后由调度层注入该回调即可，本模块无需改签名。
    """
    calendar = getattr(cfg, "calendar", None)
    if calendar is None or not getattr(calendar, "enabled", False):
        return None
    callback = getattr(market_clock, "news_multiplier", None)
    if not callable(callback):
        return None
    tier = callback(now)
    if tier is None:
        return None
    if isinstance(tier, str):
        key = tier.strip().lower()
        if key == "core":
            return _dec(cfg.spread.news_core_multiplier)
        if key == "edge":
            return _dec(cfg.spread.news_edge_multiplier)
        raise ValueError(f"UNKNOWN_NEWS_TIER: {tier!r}")
    return _dec(tier)


def time_multiplier(now: Any, cfg: Any, market_clock: Any) -> Decimal:
    """时间窗放大系数：``max(命中的窗系数)``，再按 ``cfg.spread.max_multiplier`` 封顶。

    - 换日（NY 16:45–17:15）→ ``rollover_multiplier``（默认 3.0）
    - 周初（周日 17:00–18:00 NY）→ ``monday_open_multiplier``（默认 2.5）
    - 指标核心 / 指标前後 → ``news_core_multiplier`` / ``news_edge_multiplier``（需注入回调）

    系数**只取最大的一项**（不连乘时间窗）；波动系数 P0 未实现（按 1.0）。
    """
    multipliers = [Decimal(1)]

    is_rollover = getattr(market_clock, "is_rollover_window", None)
    if callable(is_rollover) and is_rollover(now):
        multipliers.append(_dec(cfg.spread.rollover_multiplier))

    is_monday = getattr(market_clock, "is_monday_open_window", None)
    if callable(is_monday) and is_monday(now):
        multipliers.append(_dec(cfg.spread.monday_open_multiplier))

    news = _news_multiplier(now, cfg, market_clock)
    if news is not None:
        multipliers.append(news)

    result = max(multipliers)
    cap = _dec(cfg.spread.max_multiplier)
    if result > cap:
        result = cap  # 总倍率上限（Rev 1.1 §4.4）
    return result


def effective_spread_pips(
    spec: PairSpec,
    quote: QuoteSnapshot,
    now: Any,
    cfg: Any,
    market_clock: Any = market_clock_module,
) -> Decimal:
    """有效点差（pips）= 基准点差 × 时间窗系数（总倍率 ≤ ``cfg.spread.max_multiplier``）。

    - 需要快照 bid/ask 有效（缺失抛 ``ValueError("QUOTE_MISSING")``）；
      调用前应先用 :func:`quote_usable` 过滤脏数据。
    - ``market_clock`` 参数可注入（默认本模块 :mod:`market_clock`），便于单测固定时间窗；
      P0 的指标窗默认关闭（``cfg.calendar.enabled=False``）。
    - 本函数**不**按 ``spec.max_spread_pips`` 截断：超上限由调用方拒单
      （``SPREAD_TOO_WIDE``），这是规格要求的"允许平仓、拒绝新开"语义。
    """
    if quote is None or quote.bid is None or quote.ask is None:
        raise ValueError("QUOTE_MISSING: effective_spread_pips 需要 bid/ask 齐全的快照")
    if quote.bid <= _ZERO or quote.ask <= _ZERO:
        raise ValueError("QUOTE_MISSING: bid/ask 必须为正")
    base = base_spread_pips(spec, quote)
    if base <= _ZERO:
        raise ValueError(f"INVALID_BASE_SPREAD: {spec.code} 基准点差必须为正")
    return base * time_multiplier(now, cfg, market_clock)


def apply_spread(
    quote: QuoteSnapshot,
    spec: PairSpec,
    spread_pips: Decimal,
) -> tuple[Decimal, Decimal]:
    """按有效点差生成可成交的 ``(bid, ask)``（均 tick 对齐，向不利方向外扩）。

    - **计算品种**：一律以源 mid 为中心，按 ``spread_pips × pip`` 合成 bid/ask，
      并向外对齐 tick（bid 向下、ask 向上）。源 bid==ask 的幻影点差被丢弃。
    - **直源品种**：源点差 ≥ 目标点差 → 原样返回源 bid/ask（不允许凭空造流动性）；
      源点差 < 目标点差 → 以源 mid 为中心按目标点差对称加宽后再对齐 tick。
    - ``spread_pips <= 0`` 抛 ``ValueError("INVALID_SPREAD")``；
      bid/ask 缺失抛 ``ValueError("QUOTE_MISSING")``。

    返回 ``(bid, ask)``：卖出（用户卖）用 bid，买入用 ask。
    """
    if quote is None or quote.bid is None or quote.ask is None:
        raise ValueError("QUOTE_MISSING: apply_spread 需要 bid/ask 齐全的快照")
    if quote.bid <= _ZERO or quote.ask <= _ZERO:
        raise ValueError("QUOTE_MISSING: bid/ask 必须为正")
    target = _dec(spread_pips)
    if target <= _ZERO:
        raise ValueError(f"INVALID_SPREAD: 点差必须为正，收到 {spread_pips}")

    source_bid, source_ask = quote.bid, quote.ask
    if spec.source_kind == "raw":
        source_spread_pips = (source_ask - source_bid) / spec.pip
        if source_spread_pips >= target:
            return source_bid, source_ask

    mid = (source_bid + source_ask) / _TWO
    half = target * spec.pip / _TWO
    ask = round_up_to_tick(mid + half, spec.tick)
    bid = round_down_to_tick(mid - half, spec.tick)
    return bid, ask


# ---------------------------------------------------------------------------
# 滑点
# ---------------------------------------------------------------------------
def slippage_pips(
    order_type: str,
    spec: PairSpec,
    cfg: Any,
    rng: random.Random | None = None,
) -> Decimal:
    """返回某订单类型的**逆向滑点**（pips，非负）。

    - 基准：``market 0.2`` / ``stop 0.5`` / ``trailing_stop 0.5`` /
      ``rocut 1.5``（强制平仓，由调用方标记）/ ``limit 0``。
    - ``rng`` 给定则在 ``[0, jitter_max]`` 内加**确定性**抖动
      （同 seed 同结果，便于测试与事后复现）；``rng=None`` 时抖动为 0（fixed 模式）。
    - 市价滑点按 ``cfg.spread.market_max_slip_pips`` 封顶（超过则调用方应拒单）。
    - 方向（买 +、卖 −）与成交价叠加由订单引擎负责，本函数只给幅度。
    - ``spec`` 参数预留给 P1 的品种滑点乘数（P0 全品种统一），未知类型抛
      ``ValueError("UNKNOWN_ORDER_TYPE: ...")``。
    """
    key = (order_type or "").strip().lower()
    base = SLIPPAGE_BASE_PIPS.get(key)
    if base is None:
        raise ValueError(f"UNKNOWN_ORDER_TYPE: {order_type!r}")
    slip = base
    jitter_max = SLIPPAGE_JITTER_MAX_PIPS[key]
    if rng is not None and jitter_max > _ZERO:
        # 用整数抽样生成 Decimal 抖动，跨平台可复现（不用 float uniform）
        unit = Decimal(rng.randrange(0, 1001)) / Decimal(1000)
        slip = slip + unit * jitter_max
    if key == "market":
        cap = _dec(cfg.spread.market_max_slip_pips)
        if slip > cap:
            slip = cap
    return slip
