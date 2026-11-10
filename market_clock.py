"""市场时钟：纽约 17:00 日切 / 交易时段 / 日切日期判定。

交易时段口径（``forex-domain.md`` §1.5、``order-engine-spec.md`` §1.4）
---------------------------------------------------------------------
- 汇市按「**纽约当地时间 17:00 收 / 17:00 开**」的惯例：周日 17:00（NY）开盘，
  周五 17:00（NY）收盘；周六全天休市。
- 边界只跟纽约时钟走（伦敦/东京时段无关）。
- 夏令时（DST）由 ``zoneinfo("America/New_York")`` 自动处理：
  2026 年美冬令时 = 3 月 8 日前 / 11 月 1 日后（NY = UTC−5），
  美夏令时 = 3 月 8 日～11 月 1 日（NY = UTC−4）。

函数一览
--------
- :func:`now_utc`              当前 UTC 时间（带时区）
- :func:`is_market_open`       时段判定（周日 17:00 NY ～ 周五 17:00 NY）
- :func:`market_status`        "open" / "closed"
- :func:`next_rollover`        下一个「纽约 17:00」（返回 NY 时区的 aware datetime）
- :func:`is_rollover_window`   换日窗口（默认 16:45–17:15 NY，左闭右开）
- :func:`is_monday_open_window` 周初窗口（周日 17:00–18:00 NY，左闭右开）
- :func:`ny_date`              归属纽约日历日 ``YYYY-MM-DD``
- :func:`current_rollover_date` 最近一个**已经过去**的纽约 17:00 对应的 NY 日期

约定：所有入参 ``ts`` 必须是 **tz-aware** 的 ``datetime``；naive 时间直接抛
``ValueError("NAIVE_DATETIME")``（避免把 UTC 与本地时间混用造成日切错位）。
本模块不触达 I/O，不 import ``maibot_sdk``。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

#: 交易所时区：日切/时段判定唯一基准。
MARKET_TZ = ZoneInfo("America/New_York")

#: 北京/上海时区：仅用于把"北京时刻"展示给调用方（如播报），不作为判定依据。
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

#: 日切小时（纽约当地时间 17:00）。
ROLLOVER_HOUR = 17

_ONE_DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _require_aware(ts: datetime, func: str) -> datetime:
    """校验并返回 tz-aware 时间；naive 时间抛 ``ValueError("NAIVE_DATETIME")``。"""
    if not isinstance(ts, datetime):
        raise TypeError(f"INVALID_DATETIME: {func} 需要 datetime，收到 {type(ts).__name__}")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"NAIVE_DATETIME: {func} 需要带时区的时间（如 market_clock.now_utc()）")
    return ts


def _ny(ts: datetime, func: str = "market_clock") -> datetime:
    """转成纽约当地 aware datetime。"""
    return _require_aware(ts, func).astimezone(MARKET_TZ)


def _ny_rollover_of(ny_dt: datetime) -> datetime:
    """取 ``ny_dt`` 当天（纽约日历日）的 17:00。"""
    return ny_dt.replace(hour=ROLLOVER_HOUR, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# 时间源
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    """当前 UTC 时间（带 ``timezone.utc`` 时区）。"""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 交易时段
# ---------------------------------------------------------------------------
def is_market_open(ts: datetime) -> bool:
    """是否处于可交易时段：周日 17:00（NY）开盘 ～ 周五 17:00（NY）收盘。

    - 周六：全休市。
    - 周日：17:00 前休市，17:00 起开市。
    - 周五：17:00 前开市，17:00 起休市。
    - 周一～周四：全天开市（日内的 17:00 日切不是休市，只是点差放大窗口）。
    """
    ny = _ny(ts, "is_market_open")
    weekday = ny.weekday()  # Mon=0 ... Sun=6
    if weekday == 5:  # 周六
        return False
    if weekday == 6:  # 周日
        return ny >= _ny_rollover_of(ny)
    if weekday == 4:  # 周五
        return ny < _ny_rollover_of(ny)
    return True


def market_status(ts: datetime) -> str:
    """市场状态字符串：``"open"`` / ``"closed"``。"""
    return "open" if is_market_open(ts) else "closed"


def next_rollover(ts: datetime) -> datetime:
    """下一个纽约 17:00（严格晚于 ``ts``；``ts`` 恰为 17:00 时返回次日 17:00）。

    返回值带 ``America/New_York`` 时区（DST 自动），可直接与任意 tz-aware 时间比较。
    """
    ny = _ny(ts, "next_rollover")
    today_rollover = _ny_rollover_of(ny)
    if ny < today_rollover:
        return today_rollover
    return today_rollover + _ONE_DAY


def is_rollover_window(ts: datetime, minutes: int = 15) -> bool:
    """是否处于换日（日切）窗口：纽约 17:00 ± ``minutes`` 分钟，左闭右开。

    默认 16:45–17:15 NY（Rev 1.1 §4.3：流动性瞬间枯竭 + 隔夜利息结算时点）。
    窗口判定用"最近的那个 17:00"，因此横跨 17:00 前后两个自然日也算。
    """
    if minutes <= 0:
        raise ValueError(f"INVALID_WINDOW: minutes 必须为正整数，收到 {minutes}")
    ny = _ny(ts, "is_rollover_window")
    base = _ny_rollover_of(ny)
    delta = timedelta(minutes=minutes)
    for offset in (-1, 0, 1):
        rollover = base + _ONE_DAY * offset
        if rollover - delta <= ny < rollover + delta:
            return True
    return False


def is_monday_open_window(ts: datetime, hours: int = 1) -> bool:
    """是否处于周初窗口：周日 17:00（NY）起 ``hours`` 小时内，左闭右开。

    默认对应北京时间周一 06:00–07:00（美冬令时）/ 05:00–06:00（美夏令时）。
    """
    if hours <= 0:
        raise ValueError(f"INVALID_WINDOW: hours 必须为正整数，收到 {hours}")
    ny = _ny(ts, "is_monday_open_window")
    if ny.weekday() != 6:  # 只有周日 17:00 后的开盘窗口
        return False
    open_at = _ny_rollover_of(ny)
    return open_at <= ny < open_at + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# 日切日期
# ---------------------------------------------------------------------------
def ny_date(ts: datetime) -> str:
    """返回 ``ts`` 对应的纽约日历日 ``YYYY-MM-DD``（仅日切归属用，与北京日期可能差 1 天）。"""
    ny = _ny(ts, "ny_date")
    return f"{ny.year:04d}-{ny.month:02d}-{ny.day:02d}"


def current_rollover_date(ts: datetime) -> str:
    """最近一个**已经过去**的纽约 17:00 对应的 NY 日期（``YYYY-MM-DD``）。

    用途：日切检测 / 隔夜利息计付归属。语义：

    - ``ts`` 在纽约当天 17:00 之后（含 17:00 整）→ 返回当天日期；
    - ``ts`` 在 17:00 之前 → 返回前一天的日期。

    例（美冬令时）：2026-01-14 16:59 NY → ``"2026-01-13"``；
    2026-01-14 17:00 NY → ``"2026-01-14"``；2026-01-15 00:00 NY → ``"2026-01-14"``。
    """
    ny = _ny(ts, "current_rollover_date")
    if ny.time() >= time(ROLLOVER_HOUR, 0, 0):
        return f"{ny.year:04d}-{ny.month:02d}-{ny.day:02d}"
    prev: date = ny.date() - _ONE_DAY
    return f"{prev.year:04d}-{prev.month:02d}-{prev.day:02d}"
