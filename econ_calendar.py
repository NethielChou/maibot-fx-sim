"""财经日历 / 事件窗：静态事件表 + 「高影响事件前后禁止新开仓」。

职责
----
1. 解析配置里的静态事件表：每行 ``时间|币种|名称|影响``，例如
   ``2026-11-07T20:30:00|USD|非农|high``；时间**按北京时间（Asia/Shanghai）解释**。
2. 提供查询：``events_between(now, hours)``、``in_event_window(now, before_min, after_min)``、
   ``next_event(now)``。
3. 事件窗默认 **±30 分钟**；高影响事件窗内禁止新开仓（原因码 :data:`EVENT_WINDOW`），
   由 scheduler 在提交开仓前调用；已有持仓照常平仓/止损，不强平。
4. 解析失败的行**忽略并记录**（``EconomicCalendar.errors``），不影响其它事件；不抛异常。

约定
----
- 全中文口径；影响级别用 ``high`` / ``medium`` / ``low``（也接受 高/中/低）。
- 纯数据 + 纯函数，不触达 I/O、不 import 宿主 SDK；时间一律带时区 ``datetime``。
- 文件名**不再**与标准库 ``calendar`` 同名（旧 ``calendar.py`` 已改名），
  因此模块里不需要任何标准库兼容层，宿主 ``import calendar`` 也不会被遮蔽。
- ``[calendar] events`` 留空 = **没有事件**（不内置示例事件表）；``[calendar] enabled``
  默认 ``False``，不改变既有交易行为。
- v0.3.0 起真实数据源由 :mod:`calendar_source` 抓取（``[calendar] source="auto"|"static"``、
  ``refresh_minutes``）；本模块只负责事件表/事件窗/提示词，**不触网**，
  合并后的事件通过 :meth:`EconomicCalendar.replace_events` 整表替换。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

#: 事件窗原因码（订单被拒绝时展示）。
EVENT_WINDOW = "EVENT_WINDOW"
#: 默认事件窗半径（分钟）：事件公布前后各 30 分钟。
DEFAULT_WINDOW_MINUTES = 30
#: 默认提前播报窗口（分钟）：事件前 60 分钟内播报一次「即将公布」。
DEFAULT_ANNOUNCE_MINUTES = 60

IMPACT_HIGH = "high"
IMPACT_MEDIUM = "medium"
IMPACT_LOW = "low"
IMPACT_LEVELS: tuple[str, ...] = (IMPACT_HIGH, IMPACT_MEDIUM, IMPACT_LOW)
IMPACT_TEXT: dict[str, str] = {IMPACT_HIGH: "高", IMPACT_MEDIUM: "中", IMPACT_LOW: "低"}
_IMPACT_ALIASES: dict[str, str] = {
    "high": IMPACT_HIGH,
    "h": IMPACT_HIGH,
    "高": IMPACT_HIGH,
    "重要": IMPACT_HIGH,
    "medium": IMPACT_MEDIUM,
    "med": IMPACT_MEDIUM,
    "m": IMPACT_MEDIUM,
    "中": IMPACT_MEDIUM,
    "一般": IMPACT_MEDIUM,
    "low": IMPACT_LOW,
    "l": IMPACT_LOW,
    "低": IMPACT_LOW,
}

EVENT_FORMAT_HINT = "时间|币种|名称|影响，例如 2026-11-07T20:30:00|USD|非农|high"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarEvent:
    """一条财经日历事件（时间已归一化到北京时间）。"""

    at: datetime
    currency: str = ""
    name: str = ""
    impact: str = IMPACT_HIGH
    raw: str = ""

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            object.__setattr__(self, "at", self.at.replace(tzinfo=CST))
        else:
            object.__setattr__(self, "at", self.at.astimezone(CST))
        object.__setattr__(self, "currency", str(self.currency or "").upper())
        object.__setattr__(self, "name", str(self.name or "").strip())
        impact = str(self.impact or "").strip().lower()
        object.__setattr__(self, "impact", impact if impact in IMPACT_LEVELS else IMPACT_HIGH)

    @property
    def is_high(self) -> bool:
        """是否高影响事件。"""

        return self.impact == IMPACT_HIGH

    @property
    def impact_text(self) -> str:
        """影响级别中文（高/中/低）。"""

        return IMPACT_TEXT.get(self.impact, "高")

    def time_text(self) -> str:
        """事件时间文本（北京时间 ``MM-DD HH:MM``）。"""

        return self.at.strftime("%m-%d %H:%M")

    def label(self) -> str:
        """``USD 非农（高影响）`` 形式的一行标题。"""

        subject = " ".join(part for part in (self.currency, self.name) if part)
        return f"{subject or '重要数据'}（{self.impact_text}影响）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "currency": self.currency,
            "name": self.name,
            "impact": self.impact,
            "raw": self.raw,
        }


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _normalize_impact(text: Any) -> str | None:
    key = str(text or "").strip().lower()
    return _IMPACT_ALIASES.get(key)


def _parse_time(text: Any) -> datetime:
    raw = str(text or "").strip().replace("/", "-")
    if not raw:
        raise ValueError("INVALID_EVENT：时间不能为空")
    candidate = raw.replace(" ", "T", 1) if " " in raw and "T" not in raw.upper() else raw
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        at = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"INVALID_EVENT：时间无法解析 {text!r}") from exc
    if at.tzinfo is None:
        return at.replace(tzinfo=CST)
    return at.astimezone(CST)


def parse_event(line: Any) -> CalendarEvent:
    """解析一行事件；格式非法抛 ``ValueError``（消息带 ``INVALID_EVENT``）。"""

    text = str(line or "").strip()
    if not text:
        raise ValueError("INVALID_EVENT：空行")
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 2:
        raise ValueError(f"INVALID_EVENT：字段不足（{EVENT_FORMAT_HINT}）")
    if len(parts) > 4:
        raise ValueError(f"INVALID_EVENT：字段过多（{EVENT_FORMAT_HINT}）")
    at = _parse_time(parts[0])
    currency = ""
    name = ""
    impact = IMPACT_HIGH
    if len(parts) == 2:
        name = parts[1]
    elif len(parts) == 3:
        currency, name = parts[1], parts[2]
    else:
        currency, name, impact = parts[1], parts[2], parts[3]
    if not name:
        raise ValueError("INVALID_EVENT：事件名称不能为空")
    impact_key = _normalize_impact(impact)
    if impact_key is None:
        raise ValueError(f"INVALID_EVENT：影响级别非法 {impact!r}（可用 high/medium/low 或 高/中/低）")
    return CalendarEvent(at=at, currency=currency, name=name, impact=impact_key, raw=text)


def parse_events(lines: Iterable[Any] | None) -> tuple[list[CalendarEvent], list[str]]:
    """批量解析事件行：返回 ``(事件列表, 错误说明列表)``。

    空行与 ``#`` 注释直接跳过；解析失败的行忽略并记一条中文错误说明（不抛异常）。
    结果按时间升序排列。
    """

    events: list[CalendarEvent] = []
    errors: list[str] = []
    for index, line in enumerate(list(lines or []), start=1):
        text = str(line or "").strip()
        if not text or text.startswith("#"):
            continue
        try:
            events.append(parse_event(text))
        except ValueError as exc:
            errors.append(f"第 {index} 条事件已忽略：{exc}")
    events.sort(key=lambda item: item.at)
    return events, errors


# ---------------------------------------------------------------------------
# 纯函数查询（模块级，便于单测与复用）
# ---------------------------------------------------------------------------


def _sorted_events(events: Iterable[CalendarEvent] | None) -> list[CalendarEvent]:
    return sorted(list(events or []), key=lambda item: item.at)


def events_between(
    events: Iterable[CalendarEvent] | None, now: datetime, hours: float
) -> list[CalendarEvent]:
    """``[now, now + hours]`` 区间内的事件（含两端，按时间升序）。"""

    if now is None:
        return []
    horizon = max(0.0, float(hours or 0.0))
    end = now + timedelta(hours=horizon)
    return [event for event in _sorted_events(events) if now <= event.at <= end]


def next_event(events: Iterable[CalendarEvent] | None, now: datetime) -> CalendarEvent | None:
    """``now`` 之后（含此刻）最近的一条事件；没有返回 ``None``。"""

    if now is None:
        return None
    for event in _sorted_events(events):
        if event.at >= now:
            return event
    return None


def in_event_window(
    events: Iterable[CalendarEvent] | None,
    now: datetime,
    before_min: float = DEFAULT_WINDOW_MINUTES,
    after_min: float = DEFAULT_WINDOW_MINUTES,
    *,
    high_only: bool = False,
) -> CalendarEvent | None:
    """``now`` 是否落在某事件的事件窗内（``[时间-before, 时间+after]``，两端闭区间）。

    命中返回该事件（多条命中取最早的一条），否则返回 ``None``。
    ``high_only=True`` 时只判定高影响事件。
    """

    if now is None:
        return None
    before = max(0.0, float(before_min if before_min is not None else DEFAULT_WINDOW_MINUTES))
    after = max(0.0, float(after_min if after_min is not None else DEFAULT_WINDOW_MINUTES))
    for event in _sorted_events(events):
        if high_only and not event.is_high:
            continue
        if event.at - timedelta(minutes=before) <= now <= event.at + timedelta(minutes=after):
            return event
    return None


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


def _get(source: Any, name: str, default: Any = None) -> Any:
    """按 dict / 对象两种形态取配置值。"""

    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def calendar_section(cfg: Any) -> Any:
    """从完整配置里取出 ``[calendar]`` 段；传入的本身就是该段时原样返回。"""

    if cfg is None or isinstance(cfg, Mapping):
        return cfg
    inner = getattr(cfg, "calendar", None)
    if inner is not None:
        return inner
    return cfg


# ---------------------------------------------------------------------------
# 日历对象
# ---------------------------------------------------------------------------


class EconomicCalendar:
    """经济指标日历（不可变事件表 + 事件窗口径）。

    参数：
    - ``events``：已解析事件（或原始行字符串，构造时自动解析）；
    - ``enabled``：总开关；关闭时 ``blocks_new_position`` 恒为 ``False``；
    - ``before_minutes`` / ``after_minutes``：事件窗前后半径（默认各 30 分钟）；
    - ``block_high_impact_only``：只禁止高影响事件（默认 ``True``）；
    - ``announce_minutes``：事件前多少分钟内播报「即将公布」（默认 60）。
    """

    def __init__(
        self,
        events: Sequence[CalendarEvent | str] | None = None,
        *,
        enabled: bool = True,
        before_minutes: float = DEFAULT_WINDOW_MINUTES,
        after_minutes: float = DEFAULT_WINDOW_MINUTES,
        block_high_impact_only: bool = True,
        announce_minutes: int = DEFAULT_ANNOUNCE_MINUTES,
        errors: Iterable[str] | None = None,
    ) -> None:
        parsed: list[CalendarEvent] = []
        collected: list[str] = list(errors or [])
        for item in list(events or []):
            if isinstance(item, CalendarEvent):
                parsed.append(item)
            else:
                try:
                    parsed.append(parse_event(item))
                except ValueError as exc:
                    collected.append(f"事件已忽略：{exc}")
        parsed.sort(key=lambda item: item.at)
        self._events: tuple[CalendarEvent, ...] = tuple(parsed)
        self.enabled = bool(enabled)
        self.before_minutes = max(0.0, float(before_minutes))
        self.after_minutes = max(0.0, float(after_minutes))
        self.block_high_impact_only = bool(block_high_impact_only)
        self.announce_minutes = max(0, int(announce_minutes))
        self.errors: tuple[str, ...] = tuple(collected)

    # ---------- 构造 ----------

    @classmethod
    def from_config(cls, cfg: Any) -> "EconomicCalendar":
        """按配置构造日历；``events`` 留空表示**没有事件**（不内置示例表）。"""

        section = calendar_section(cfg)
        raw_lines = list(_get(section, "events", []) or [])
        events, errors = parse_events(raw_lines)
        raw_window = _get(section, "blackout_minutes", DEFAULT_WINDOW_MINUTES)
        try:
            window = max(0, int(raw_window))
        except (TypeError, ValueError):
            window = DEFAULT_WINDOW_MINUTES
        try:
            announce = int(_get(section, "announce_minutes", DEFAULT_ANNOUNCE_MINUTES) or 0)
        except (TypeError, ValueError):
            announce = DEFAULT_ANNOUNCE_MINUTES
        return cls(
            events,
            enabled=bool(_get(section, "enabled", False)),
            before_minutes=window,
            after_minutes=window,
            block_high_impact_only=bool(_get(section, "block_high_impact_only", True)),
            announce_minutes=announce,
            errors=errors,
        )

    # ---------- 只读视图 ----------

    @property
    def events(self) -> tuple[CalendarEvent, ...]:
        """已解析事件（按时间升序）。"""

        return self._events

    def replace_events(self, events: Iterable[CalendarEvent]) -> None:
        """整表替换事件（真实日历刷新后与静态表合并时用；保持启用/窗口配置不变）。"""

        parsed = sorted(list(events), key=lambda item: item.at)
        self._events = tuple(parsed)

    def __len__(self) -> int:
        return len(self._events)

    # ---------- 查询 ----------

    def events_between(self, now: datetime, hours: float = 24.0) -> list[CalendarEvent]:
        """``[now, now + hours]`` 内的事件。"""

        return events_between(self._events, now, hours)

    def next_event(self, now: datetime) -> CalendarEvent | None:
        """下一条事件；没有返回 ``None``。"""

        return next_event(self._events, now)

    def in_event_window(
        self,
        now: datetime,
        before_min: float | None = None,
        after_min: float | None = None,
        *,
        high_only: bool | None = None,
    ) -> CalendarEvent | None:
        """``now`` 是否在事件窗内；返回命中的事件或 ``None``。"""

        if not self.enabled:
            return None
        return in_event_window(
            self._events,
            now,
            self.before_minutes if before_min is None else before_min,
            self.after_minutes if after_min is None else after_min,
            high_only=self.block_high_impact_only if high_only is None else high_only,
        )

    def blocks_new_position(self, now: datetime) -> tuple[bool, CalendarEvent | None]:
        """事件窗内是否禁止新开仓：``(是否禁止, 命中事件)``。"""

        if not self.enabled:
            return False, None
        event = self.in_event_window(now)
        return (True, event) if event is not None else (False, None)

    def event_soon(self, now: datetime, minutes: int | None = None) -> CalendarEvent | None:
        """``now`` 之后 ``minutes`` 分钟内最近的一条事件（用于「即将公布」播报）。"""

        if not self.enabled:
            return None
        window = self.announce_minutes if minutes is None else max(0, int(minutes))
        if window <= 0:
            return None
        horizon = now + timedelta(minutes=window)
        for event in self._events:
            if now <= event.at <= horizon:
                return event
        return None

    # ---------- 提示词 ----------

    def prompt_lines(self, now: datetime, hours: float = 24.0) -> list[str]:
        """决策提示词用的中文日历片段；未启用返回空列表。"""

        if not self.enabled:
            return []
        lines = ["【财经日历】"]
        blocked = self.in_event_window(now)
        if blocked is not None:
            lines.append(
                f"- 现在处于「{blocked.currency} {blocked.name}」事件窗（{blocked.impact_text}影响）："
                "禁止开新仓；已有持仓可以照常平仓或止损。"
            )
        upcoming = self.events_between(now, hours)
        if upcoming:
            head = "；".join(f"{item.time_text()} {item.label()}" for item in upcoming[:3])
            lines.append(f"- 未来 {hours:g} 小时有 {len(upcoming)} 个事件：{head}")
            if len(upcoming) > 3:
                lines.append(f"- ……另有 {len(upcoming) - 3} 个事件未列出。")
        else:
            lines.append(f"- 未来 {hours:g} 小时没有已登记的重要事件。")
        scope = "高影响事件" if self.block_high_impact_only else "所有登记事件"
        lines.append(
            f"- 口径：{scope}公布前后各 {self.before_minutes:g} 分钟禁止开新仓"
            f"（事件窗内想交易就等，不要抢数据）。"
        )
        return lines


def build(events: Iterable[CalendarEvent | str] | None = None, **kwargs: Any) -> EconomicCalendar:
    """便捷构造（等价于 ``EconomicCalendar(...)``）。"""

    return EconomicCalendar(events, **kwargs)
