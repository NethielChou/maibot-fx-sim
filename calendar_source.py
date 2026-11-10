"""真实财经日历数据源：多源尝试 + 纯函数解析 + 内存缓存（不落盘）。

数据源（2026-11-08 实测，只读公开接口，不伪造数据）
----------------------------------------------------
1. **ForexFactory 周历 JSON**（首选，当前可用）
   ``https://nfs.faireconomy.media/ff_calendar_thisweek.json``
   返回 JSON 数组，每项形如::

       {"title": "CPI y/y", "country": "CNY", "date": "2026-11-08T20:30:00-05:00",
        "impact": "Low", "forecast": "0.8%", "previous": "0.9%"}

   ``country`` 就是货币代码（USD/EUR/JPY/CNY…），``date`` 带时区偏移（美东），
   由本模块统一换算成北京时间；``impact`` = High / Medium / Low / Holiday。
2. **新浪财经日历 JSONP**（备源，中文；实测数据只到 2026-07 左右，当前日期多为空）
   ``http://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/cb/DailyFX_AllService.getOnedayEventsNew?date=YYYY-MM-DD``
   （服务名取自页面脚本 ``forex_n.js``；响应 ``cb({"data":[...],"meeting":[],"holiday":[]});``）
   字段：``date`` / ``time``（北京时间）/ ``country``（中文国家名）/ ``event`` /
   ``importance``（``***`` 高、``**`` 中、``*`` 低）。

不可用（2026-11-08 探测记录，写在这里避免以后重复踩坑）
--------------------------------------------------------
- 华尔街见闻 ``api-one-wscn.awtmt.com/apiv1/finance/calendar`` → ``{"code":71404,"message":"Not Found"}``；
- 金十 ``cdn-rili.jin10.com`` / ``rili.jin10.com`` 数据接口 → DNS 解析失败或 404；
- 东方财富 ``RPT_ECONOMICVALUE`` → ``报表配置不存在``；``data.fx678.com`` → DNS 失败。

约定
----
- 解析函数是**纯函数**（fixture 可离线单测）：``parse_ff_payload`` / ``parse_sina_payload``；
- 抓取统一走 ``urllib.request`` + ``asyncio.to_thread``，可注入 ``fetcher`` 保证离线测试；
- 内存缓存默认 30 分钟（``[calendar] refresh_minutes``），**不落盘、不写日志正文**；
- 全部源失败返回 ``([], 原因说明)``；任一源成功返回 ``(events, "")``；
- 事件名尽量翻译成中文（常见指标），未收录的保留源标题（英文专名不译）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

try:  # 宿主把 plugin.py 当包加载时走相对导入
    from .econ_calendar import (  # type: ignore[import-not-found]
        IMPACT_HIGH,
        IMPACT_LOW,
        IMPACT_MEDIUM,
        CalendarEvent,
    )
except ImportError:  # 项目根直接加载（单测 / 脚本）
    from econ_calendar import (  # type: ignore[no-redef]
        IMPACT_HIGH,
        IMPACT_LOW,
        IMPACT_MEDIUM,
        CalendarEvent,
    )

CST = ZoneInfo("Asia/Shanghai")

#: 默认缓存时长（分钟）。
DEFAULT_REFRESH_MINUTES = 30
#: 抓取失败后至少间隔这么多秒再重试（避免每 tick 打网络）。
FAILURE_RETRY_SECONDS = 60
#: 默认单次请求超时（秒）。
DEFAULT_TIMEOUT_SECONDS = 8
#: 普通浏览器 UA。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 原因码（日志与播报用，不带数据正文）。
REASON_FETCH_FAILED = "CALENDAR_FETCH_FAILED"
REASON_HTTP_ERROR = "CALENDAR_HTTP_ERROR"
REASON_EMPTY = "CALENDAR_EMPTY"
REASON_PARSE_ERROR = "CALENDAR_PARSE_ERROR"
REASON_ALL_SOURCES_FAILED = "CALENDAR_ALL_SOURCES_FAILED"

#: 数据源定义：(名称, URL 构造器)。按顺序尝试。
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SINA_URL_TEMPLATE = (
    "http://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/cb/"
    "DailyFX_AllService.getOnedayEventsNew?date={date}"
)

#: 新浪中文国家名 → 货币代码（源里 currency 字段为空，只能按国家映射）。
SINA_COUNTRY_CURRENCY: dict[str, str] = {
    "美国": "USD",
    "中国": "CNY",
    "欧元区": "EUR",
    "德国": "EUR",
    "法国": "EUR",
    "意大利": "EUR",
    "西班牙": "EUR",
    "英国": "GBP",
    "日本": "JPY",
    "澳大利亚": "AUD",
    "加拿大": "CAD",
    "瑞士": "CHF",
    "新西兰": "NZD",
}

#: 常见指标中文名（源标题 → 中文）。未收录的保留源标题，避免翻译错数据含义。
_EVENT_NAME_ZH: dict[str, str] = {
    "non-farm employment change": "非农就业人数",
    "nonfarm payrolls": "非农就业人数",
    "unemployment rate": "失业率",
    "unemployment claims": "初请失业金人数",
    "adp employment change": "ADP 就业人数",
    "average hourly earnings m/m": "平均时薪月率",
    "cpi m/m": "消费者物价指数月率",
    "cpi y/y": "消费者物价指数年率",
    "core cpi m/m": "核心消费者物价指数月率",
    "core cpi y/y": "核心消费者物价指数年率",
    "ppi m/m": "生产者物价指数月率",
    "ppi y/y": "生产者物价指数年率",
    "gdp m/m": "国内生产总值月率",
    "gdp q/q": "国内生产总值季率",
    "gdp y/y": "国内生产总值年率",
    "retail sales m/m": "零售销售月率",
    "retail sales y/y": "零售销售年率",
    "industrial production m/m": "工业产出月率",
    "manufacturing pmi": "制造业采购经理人指数",
    "services pmi": "服务业采购经理人指数",
    "ism manufacturing pmi": "ISM 制造业采购经理人指数",
    "ism services pmi": "ISM 服务业采购经理人指数",
    "interest rate decision": "利率决议",
    "fomc statement": "美联储政策声明",
    "fomc economic projections": "美联储经济预期",
    "fed chair powell speaks": "美联储主席鲍威尔讲话",
    "ecb interest rate decision": "欧洲央行利率决议",
    "ecb press conference": "欧洲央行新闻发布会",
    "boj policy rate": "日本央行政策利率",
    "boj press conference": "日本央行新闻发布会",
    "trade balance": "贸易帐",
    "current account": "经常帐",
    "building permits": "营建许可",
    "housing starts": "新屋开工",
    "consumer confidence": "消费者信心指数",
    "business confidence": "商业信心指数",
    "crude oil inventories": "原油库存",
    "natural gas storage": "天然气库存",
    "bank holiday": "银行假期",
}


def _impact(value: Any) -> str:
    """统一影响级别：high / medium / low。"""

    text = str(value or "").strip().lower()
    if text in ("high", "h", "3", "***", "高"):
        return IMPACT_HIGH
    if text in ("medium", "med", "m", "2", "**", "中"):
        return IMPACT_MEDIUM
    return IMPACT_LOW


def impact_from_stars(stars: Any) -> str:
    """新浪 ``importance`` 星号 → 影响级别（``***`` 高 / ``**`` 中 / 其余低）。"""

    text = str(stars or "").strip()
    if text.startswith("***"):
        return IMPACT_HIGH
    if text.startswith("**"):
        return IMPACT_MEDIUM
    return IMPACT_LOW


def translate_name(title: Any) -> str:
    """把源标题翻成中文（只翻常见指标；未收录原样保留，不臆造含义）。"""

    text = str(title or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for key, value in _EVENT_NAME_ZH.items():
        if lowered == key or lowered.startswith(key):
            suffix = lowered[len(key):].strip()
            return f"{value}{(' ' + text[len(key):].strip()) if suffix else ''}".strip()
    return text


# ---------------------------------------------------------------------------
# 解析（纯函数）
# ---------------------------------------------------------------------------


def _parse_iso_beijing(text: str) -> datetime | None:
    """ISO8601（可带偏移）→ 北京时间；无时区按北京时间解释。"""

    raw = str(text or "").strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=CST)
    return value.astimezone(CST)


def parse_ff_payload(text: str) -> list[CalendarEvent]:
    """解析 ForexFactory 周历 JSON（纯函数）。

    - 只接受能解析出日期的事件；缺标题/日期的事件跳过（不抛异常）；
    - ``impact`` 的 Holiday 归为低影响；``currency`` 取 ``country`` 字段；
    - 结果按时间升序。
    """

    try:
        data = json.loads(text or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{REASON_PARSE_ERROR}：不是合法 JSON（{exc}）") from exc
    if not isinstance(data, list):
        raise ValueError(f"{REASON_PARSE_ERROR}：JSON 根节点应为数组")
    events: list[CalendarEvent] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        at = _parse_iso_beijing(str(item.get("date") or ""))
        title = translate_name(item.get("title"))
        if at is None or not title:
            continue
        events.append(
            CalendarEvent(
                at=at,
                currency=str(item.get("country") or "").strip().upper(),
                name=title,
                impact=_impact(item.get("impact")),
                raw=str(item.get("title") or "").strip(),
            )
        )
    events.sort(key=lambda item: item.at)
    return events


_JSONP_RE = re.compile(r"cb\((.*)\)\s*;?\s*$", re.DOTALL)


def _jsonp_payload(text: str) -> str:
    """从新浪 JSONP 响应里取出 JSON 主体（``cb({...});``）。"""

    body = str(text or "").strip()
    match = _JSONP_RE.search(body)
    if match is not None:
        return match.group(1).strip()
    start = body.find("(")
    end = body.rfind(")")
    if start >= 0 and end > start:
        return body[start + 1 : end].strip()
    return body


def parse_sina_payload(text: str) -> list[CalendarEvent]:
    """解析新浪财经日历 JSONP（纯函数）。

    - 兼容 ``cb(null)`` / 空 ``data`` → 返回空列表（不算解析错误）；
    - ``date`` + ``time`` 按北京时间解释；``importance`` 星号决定影响级别；
    - 只取 ``data``（指标事件），``meeting`` / ``holiday`` 无确定时刻，忽略；
    - 结果按时间升序。
    """

    payload = _jsonp_payload(text)
    if payload in ("", "null"):
        return []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{REASON_PARSE_ERROR}：JSONP 内容不是合法 JSON（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{REASON_PARSE_ERROR}：JSONP 根节点应为对象")
    rows = data.get("data") or []
    if not isinstance(rows, list):
        return []
    events: list[CalendarEvent] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        date_text = str(item.get("date") or "").strip()
        time_text = str(item.get("time") or "").strip()
        if not date_text or not time_text:
            continue
        at = _parse_iso_beijing(f"{date_text}T{time_text}")
        name = translate_name(item.get("event"))
        if at is None or not name:
            continue
        country = str(item.get("country") or "").strip()
        currency = str(item.get("currency") or "").strip().upper()
        if not currency:
            currency = SINA_COUNTRY_CURRENCY.get(country, "")
        events.append(
            CalendarEvent(
                at=at,
                currency=currency,
                name=name,
                impact=impact_from_stars(item.get("importance")),
                raw=str(item.get("event") or "").strip(),
            )
        )
    events.sort(key=lambda item: item.at)
    return events


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def _fetch_bytes(url: str, timeout: int) -> bytes:
    """同步 GET（在线程池里跑）；网络异常转 ``RuntimeError`` 带原因码。"""

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/javascript,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{REASON_HTTP_ERROR}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{REASON_FETCH_FAILED}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"{REASON_FETCH_FAILED}: 超时 {timeout}s") from exc
    except OSError as exc:
        raise RuntimeError(f"{REASON_FETCH_FAILED}: {exc}") from exc


def _decode(raw: bytes) -> str:
    """UTF-8 解码（源都是 UTF-8；坏字节替换，不抛异常）。"""

    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError(f"{REASON_PARSE_ERROR}：需要 bytes，收到 {type(raw).__name__}")
    return bytes(raw).decode("utf-8", errors="replace")


def _beijing_dates(now: datetime | None, days: int = 2) -> list[str]:
    """北京时间的连续日期（用于新浪按日接口）。"""

    base = (now or datetime.now(CST)).astimezone(CST)
    return [(base + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days)]


async def fetch_events(
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    now: datetime | None = None,
    fetcher: Callable[[str, int], bytes] | None = None,
) -> tuple[list[CalendarEvent], str]:
    """依次尝试多个数据源，返回 ``(事件列表, 失败原因)``。

    - 任一源解析出事件 → ``(events, "")``；
    - 全部源失败/为空 → ``([], "sources=ff:原因,sina:原因")``（原因码，不含正文）；
    - 抓取统一在线程池执行；``fetcher`` 可注入，保证离线单测不触网。
    """

    fetch = fetcher or _fetch_bytes
    failures: list[str] = []

    async def _run(url: str) -> bytes:
        return await asyncio.to_thread(fetch, url, int(timeout))

    # 1) ForexFactory 周历
    try:
        raw = await _run(FF_URL)
        events = parse_ff_payload(_decode(raw))
    except (RuntimeError, ValueError, TypeError) as exc:
        failures.append(f"ff:{_reason_of(exc)}")
    else:
        if events:
            return events, ""
        failures.append(f"ff:{REASON_EMPTY}")

    # 2) 新浪按日接口（今天 + 明天，北京时间）
    sina_events: list[CalendarEvent] = []
    sina_failures: list[str] = []
    for date_text in _beijing_dates(now, 2):
        try:
            raw = await _run(SINA_URL_TEMPLATE.format(date=date_text))
            sina_events.extend(parse_sina_payload(_decode(raw)))
        except (RuntimeError, ValueError, TypeError) as exc:
            sina_failures.append(_reason_of(exc))
    if sina_events:
        unique = {(item.at, item.currency, item.name): item for item in sina_events}
        return sorted(unique.values(), key=lambda item: item.at), ""
    failures.append(f"sina:{sina_failures[0] if sina_failures else REASON_EMPTY}")
    return [], f"{REASON_ALL_SOURCES_FAILED}: " + ",".join(failures)


def _reason_of(exc: BaseException) -> str:
    """异常 → 原因码（只取消息里的第一个原因码，不泄露正文）。"""

    text = str(exc or "")
    for code in (
        REASON_HTTP_ERROR,
        REASON_FETCH_FAILED,
        REASON_PARSE_ERROR,
        REASON_EMPTY,
        REASON_ALL_SOURCES_FAILED,
    ):
        if text.startswith(code) or code in text:
            return code
    return type(exc).__name__


# ---------------------------------------------------------------------------
# 内存缓存
# ---------------------------------------------------------------------------


class CalendarFeed:
    """财经日历内存缓存（默认 30 分钟；失败后 60 秒内不重试；不落盘）。"""

    def __init__(
        self,
        fetcher: Callable[..., Any] | None = None,
        *,
        refresh_minutes: int = DEFAULT_REFRESH_MINUTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self.refresh_seconds = max(60.0, float(refresh_minutes) * 60.0)
        self._clock = clock or time.monotonic
        self._events: list[CalendarEvent] = []
        self._reason: str = ""
        self._last_ok: float | None = None
        self._last_attempt: float | None = None

    @property
    def events(self) -> list[CalendarEvent]:
        """最近一次成功抓取的事件（副本）。"""

        return list(self._events)

    @property
    def reason(self) -> str:
        """最近一次失败原因码（成功时为空串）。"""

        return self._reason

    def clear(self) -> None:
        """清空缓存（reset / 重新加载配置时用）。"""

        self._events = []
        self._reason = ""
        self._last_ok = None
        self._last_attempt = None

    async def get_events(self, *, force: bool = False) -> tuple[list[CalendarEvent], str]:
        """取事件：命中缓存直接返回；否则抓取并更新缓存。"""

        now = self._clock()
        if not force and self._events and self._last_ok is not None:
            if now - self._last_ok < self.refresh_seconds:
                return list(self._events), ""
        if not force and self._last_attempt is not None and now - self._last_attempt < FAILURE_RETRY_SECONDS:
            return list(self._events), self._reason
        self._last_attempt = now
        if self._fetcher is None:
            events, reason = await fetch_events()
        else:
            events, reason = await self._call_fetcher()
        if events:
            self._events = list(events)
            self._last_ok = now
            self._reason = ""
        else:
            self._events = []
            self._reason = reason or REASON_EMPTY
        return list(self._events), self._reason

    async def _call_fetcher(self) -> tuple[list[CalendarEvent], str]:
        """调用注入的抓取器（支持同步/异步、可返回 (events, reason) 或事件列表）。"""

        assert self._fetcher is not None
        result = self._fetcher()
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            result = await result
        if isinstance(result, tuple) and len(result) == 2:
            events = list(result[0] or [])
            reason = str(result[1] or "")
        else:
            events = list(result or [])
            reason = ""
        if not events:
            return [], reason or REASON_EMPTY
        return events, ""


def merge_events(*groups: Iterable[CalendarEvent] | None) -> list[CalendarEvent]:
    """合并多组事件并按 (时间, 货币, 名称) 去重，返回时间升序列表。"""

    merged: dict[tuple[datetime, str, str], CalendarEvent] = {}
    for group in groups:
        for event in group or []:
            merged[(event.at, event.currency, event.name)] = event
    return sorted(merged.values(), key=lambda item: item.at)
