"""真实财经日历单测：解析 fixture、多源回退、缓存 TTL、source=static 不触网、事件窗/播报。

全部离线：网络抓取通过注入 ``fetcher`` 替换，绝不产生真实请求。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import calendar_source as cs  # noqa: E402
from econ_calendar import CalendarEvent, EconomicCalendar  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=CST)  # 北京时间 2026-11-08 12:00（周日）
OPEN_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=CST)  # 周五中午：外汇市场开盘

#: ForexFactory 周历真实样例（2026-11-08 抓取，脱敏：只保留结构与时间字段）。
FF_JSON = (
    '[{"title":"RBA Assist Gov Hunter Speaks","country":"AUD",'
    '"date":"2026-11-08T17:30:00-05:00","impact":"Low","forecast":"","previous":""},'
    '{"title":"CPI y\\/y","country":"CNY","date":"2026-11-08T20:30:00-05:00",'
    '"impact":"Low","forecast":"0.8%","previous":"0.9%"},'
    '{"title":"Core CPI m\\/m","country":"USD","date":"2026-11-10T08:30:00-05:00",'
    '"impact":"High","forecast":"0.2%","previous":"0.3%"},'
    '{"title":"Bank Holiday","country":"JPY","date":"2026-11-11T00:00:00-05:00",'
    '"impact":"Holiday","forecast":"","previous":""}]'
)

#: 新浪财经日历 JSONP 真实样例（2025-01-10 抓取，脱敏：只保留结构与时间字段）。
SINA_JSONP = (
    "/*<script>location.href='//sina.com';</script>*/\n"
    'cb({"data":['
    '{"id":"236872","date":"2025-01-10","time":"21:30:00","currency":"","country":"美国",'
    '"event":"非农就业数据","period":"12月","importance":"***","previous":"22.7万",'
    '"median":"16万","ifr_actual":"25.6万","gjpy":null,"eventpy":null},'
    '{"id":"236873","date":"2025-01-10","time":"23:00:00","currency":"","country":"美国",'
    '"event":"实际收入月率","period":"12月","importance":"**","previous":"74%",'
    '"median":"73.8%","ifr_actual":"73.2%","gjpy":"美国","eventpy":"实际收入月率"}'
    '],"meeting":[],"holiday":[]});'
)


class ParseForexFactoryTestCase(unittest.TestCase):
    def test_parse_real_sample(self) -> None:
        events = cs.parse_ff_payload(FF_JSON)
        self.assertEqual(len(events), 4)
        first = events[0]
        self.assertEqual(first.currency, "AUD")
        self.assertEqual(first.impact, "low")
        # 2026-11-08T17:30-05:00 → 北京时间 2026-11-09 06:30
        self.assertEqual(first.at.astimezone(CST), datetime(2026, 11, 9, 6, 30, tzinfo=CST))
        names = [event.name for event in events]
        self.assertIn("消费者物价指数年率", names)       # CPI y/y 已翻译
        self.assertIn("核心消费者物价指数月率", names)   # Core CPI m/m 已翻译
        self.assertIn("RBA Assist Gov Hunter Speaks", names)  # 未收录保留原文

    def test_impact_mapping(self) -> None:
        events = cs.parse_ff_payload(FF_JSON)
        by_name = {event.name: event for event in events}
        self.assertEqual(by_name["核心消费者物价指数月率"].impact, "high")
        self.assertEqual(by_name["银行假期"].impact, "low")  # Holiday → low

    def test_sorted_by_time(self) -> None:
        events = cs.parse_ff_payload(FF_JSON)
        self.assertEqual([event.at for event in events], sorted(event.at for event in events))

    def test_bad_json_raises_parse_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cs.parse_ff_payload("not json")
        self.assertIn("CALENDAR_PARSE_ERROR", str(ctx.exception))

    def test_non_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            cs.parse_ff_payload('{"data": []}')

    def test_rows_without_date_skipped(self) -> None:
        payload = json.dumps([{"title": "CPI y/y", "country": "USD", "date": "", "impact": "High"}])
        self.assertEqual(cs.parse_ff_payload(payload), [])


class ParseSinaTestCase(unittest.TestCase):
    def test_parse_real_sample(self) -> None:
        events = cs.parse_sina_payload(SINA_JSONP)
        self.assertEqual(len(events), 2)
        first = events[0]
        self.assertEqual(first.currency, "USD")  # 中文国家名「美国」→ USD
        self.assertEqual(first.impact, "high")   # *** → 高
        self.assertEqual(first.at, datetime(2025, 1, 10, 21, 30, tzinfo=CST))
        self.assertEqual(events[1].impact, "medium")  # ** → 中
        self.assertEqual(first.name, "非农就业数据")

    def test_null_payload_returns_empty(self) -> None:
        self.assertEqual(cs.parse_sina_payload("cb(null);"), [])
        self.assertEqual(cs.parse_sina_payload('cb({"data":[],"meeting":[],"holiday":[]});'), [])

    def test_bad_jsonp_raises_parse_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cs.parse_sina_payload("cb(not-json);")
        self.assertIn("CALENDAR_PARSE_ERROR", str(ctx.exception))

    def test_meeting_and_holiday_ignored(self) -> None:
        payload = 'cb({"data":[],"meeting":[{"title":"议息"}],"holiday":[{"title":"休市"}]});'
        self.assertEqual(cs.parse_sina_payload(payload), [])


class TranslateNameTestCase(unittest.TestCase):
    def test_known_and_unknown(self) -> None:
        self.assertEqual(cs.translate_name("Non-Farm Employment Change"), "非农就业人数")
        self.assertEqual(cs.translate_name("Some Custom Event"), "Some Custom Event")
        self.assertEqual(cs.translate_name(""), "")


class FetchEventsTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_ff_success_returns_events(self) -> None:
        calls: list[str] = []

        def fetcher(url: str, timeout: int) -> bytes:
            calls.append(url)
            return FF_JSON.encode("utf-8")

        events, reason = await cs.fetch_events(fetcher=fetcher)
        self.assertEqual(reason, "")
        self.assertEqual(len(events), 4)
        self.assertTrue(any("faireconomy" in url for url in calls))

    async def test_all_sources_fail_reports_reason(self) -> None:
        def fetcher(url: str, timeout: int) -> bytes:
            raise RuntimeError("CALENDAR_HTTP_ERROR: HTTP 503")

        events, reason = await cs.fetch_events(fetcher=fetcher)
        self.assertEqual(events, [])
        self.assertIn("CALENDAR_ALL_SOURCES_FAILED", reason)
        self.assertIn("ff:", reason)
        self.assertIn("sina:", reason)

    async def test_ff_empty_falls_back_to_sina(self) -> None:
        def fetcher(url: str, timeout: int) -> bytes:
            if "faireconomy" in url:
                return b"[]"
            return SINA_JSONP.encode("utf-8")

        events, reason = await cs.fetch_events(fetcher=fetcher, now=datetime(2025, 1, 10, 12, 0, tzinfo=CST))
        self.assertEqual(reason, "")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].name, "非农就业数据")

    async def test_ff_bad_payload_falls_back_to_sina(self) -> None:
        def fetcher(url: str, timeout: int) -> bytes:
            if "faireconomy" in url:
                return b"<html>blocked</html>"
            return SINA_JSONP.encode("utf-8")

        events, _reason = await cs.fetch_events(fetcher=fetcher, now=datetime(2025, 1, 10, 12, 0, tzinfo=CST))
        self.assertEqual(events[0].currency, "USD")


class CalendarFeedTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_cache_ttl(self) -> None:
        clock = [1000.0]
        calls: list[int] = []

        async def fetcher():
            calls.append(1)
            return [CalendarEvent(at=NOW + timedelta(hours=1), currency="USD", name="非农", impact="high")], ""

        feed = cs.CalendarFeed(fetcher=fetcher, refresh_minutes=30, clock=lambda: clock[0])
        first, _ = await feed.get_events()
        second, _ = await feed.get_events()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(len(calls), 1, "缓存期内不应重复抓取")
        clock[0] += 31 * 60
        third, _ = await feed.get_events()
        self.assertEqual(len(third), 1)
        self.assertEqual(len(calls), 2, "超过 TTL 后应重新抓取")

    async def test_failure_retry_cooldown(self) -> None:
        clock = [1000.0]
        calls: list[int] = []

        async def fetcher():
            calls.append(1)
            return [], "CALENDAR_ALL_SOURCES_FAILED: ff:CALENDAR_EMPTY"

        feed = cs.CalendarFeed(fetcher=fetcher, refresh_minutes=30, clock=lambda: clock[0])
        events, reason = await feed.get_events()
        self.assertEqual(events, [])
        self.assertIn("CALENDAR_ALL_SOURCES_FAILED", reason)
        await feed.get_events()  # 60 秒冷却内不重试
        self.assertEqual(len(calls), 1)
        clock[0] += 61
        await feed.get_events()
        self.assertEqual(len(calls), 2)

    async def test_clear_resets_cache(self) -> None:
        async def fetcher():
            return [CalendarEvent(at=NOW, currency="USD", name="CPI", impact="high")], ""

        feed = cs.CalendarFeed(fetcher=fetcher, clock=lambda: 1000.0)
        await feed.get_events()
        feed.clear()
        self.assertEqual(feed.events, [])
        self.assertEqual(feed.reason, "")


class MergeEventsTestCase(unittest.TestCase):
    def test_dedupe_and_sort(self) -> None:
        first = CalendarEvent(at=NOW + timedelta(hours=2), currency="USD", name="非农", impact="high")
        second = CalendarEvent(at=NOW + timedelta(hours=1), currency="JPY", name="日银", impact="medium")
        merged = cs.merge_events([first], [first, second])
        self.assertEqual([event.name for event in merged], ["日银", "非农"])


# ---------------------------------------------------------------------------
# 与编排器集成（离线：注入 fetcher）
# ---------------------------------------------------------------------------
PKG = "_mai_fx_calendar_test_pkg"


def load_package():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(
        PKG, str(PROJECT_ROOT / "plugin.py"), submodule_search_locations=[str(PROJECT_ROOT)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件包")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


def submodule(name: str):
    load_package()
    return importlib.import_module(f"{PKG}.{name}")


class FakeSource:
    def __init__(self, quotes=None):
        self.quotes = quotes or {}

    async def fetch(self, codes, now, seq):
        return dict(self.quotes)


class SchedulerCalendarTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config_mod = submodule("config")
        self.ledger_mod = submodule("ledger")
        self.scheduler = submodule("scheduler")
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = self.ledger_mod.Ledger(Path(self.tmp.name) / "ledger.sqlite3")
        self.config = self.config_mod.MaiFxConfig()
        self.config.calendar.enabled = True
        self.ledger.init(int(self.config.account.initial_balance))

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def make_engine(self, fetcher=None, logs=None, quotes=None):
        return self.scheduler.TradingEngine(
            ctx=SimpleNamespace(),
            config=self.config,
            quote_source=FakeSource(quotes),
            ledger=self.ledger,
            log=(logs.append if logs is not None else None),
            calendar_fetcher=fetcher,
        )

    async def test_source_static_never_calls_network(self) -> None:
        self.config.calendar.source = "static"
        self.config.calendar.events = ["2026-11-08T12:20:00|USD|非农|high"]

        def boom():
            raise AssertionError("source=static 不应触网")

        engine = self.make_engine(fetcher=boom)
        await engine.refresh_calendar(NOW, force=True)
        self.assertEqual(len(engine.calendar), 1)
        self.assertEqual(engine.calendar.events[0].name, "非农")
        self.assertEqual(engine.calendar_status, "static")

    async def test_disabled_calendar_never_calls_network(self) -> None:
        self.config.calendar.enabled = False

        def boom():
            raise AssertionError("enabled=false 不应触网")

        engine = self.make_engine(fetcher=boom)
        await engine.refresh_calendar(NOW, force=True)
        self.assertEqual(len(engine.calendar), 0)

    async def test_auto_merges_static_and_real_events(self) -> None:
        self.config.calendar.source = "auto"
        self.config.calendar.events = ["2026-11-08T12:10:00|USD|静态事件|high"]
        fetched = CalendarEvent(at=NOW + timedelta(minutes=5), currency="USD", name="非农", impact="high")

        async def fetcher():
            return [fetched], ""

        engine = self.make_engine(fetcher=fetcher)
        await engine.refresh_calendar(NOW, force=True)
        names = [event.name for event in engine.calendar.events]
        self.assertEqual(names, ["非农", "静态事件"])
        self.assertIn("auto", engine.calendar_status)
        # 事件窗内禁止开新仓（用合并后的事件）
        reason = engine._open_block_reason(NOW)
        self.assertIn("EVENT_WINDOW", reason)
        self.assertIn("非农", reason)
        # 即将公布播报只发一次
        engine._announce_upcoming(NOW)
        engine._announce_upcoming(NOW)
        events = engine.drain_events()
        self.assertEqual(sum("即将公布" in text for text in events), 1)

    async def test_auto_failure_falls_back_to_static_and_warns_once(self) -> None:
        self.config.calendar.source = "auto"
        self.config.calendar.events = ["2026-11-08T12:10:00|USD|静态事件|high"]
        logs: list[str] = []

        async def failing():
            return [], "CALENDAR_ALL_SOURCES_FAILED: ff:CALENDAR_EMPTY,sina:CALENDAR_EMPTY"

        engine = self.make_engine(fetcher=failing, logs=logs)
        await engine.refresh_calendar(NOW, force=True)
        await engine.refresh_calendar(NOW, force=True)
        self.assertEqual([event.name for event in engine.calendar.events], ["静态事件"])
        self.assertIn("CALENDAR_ALL_SOURCES_FAILED", engine.calendar_status)
        warnings = [line for line in logs if "财经日历" in line]
        self.assertEqual(len(warnings), 1, "回退原因只记一次")

    async def test_rate_unavailable_suspends_tick(self) -> None:
        """行情里缺 USDCNY → 本轮不估值/不撮合，播报一次 RATE_UNAVAILABLE。"""
        models = submodule("models")
        quote = models.QuoteSnapshot(
            pair="USDJPY",
            bid=Decimal("158.000"),
            ask=Decimal("158.010"),
            mid=Decimal("158.005"),
            quote_ts=OPEN_NOW,
            recv_ts=OPEN_NOW,
            seq=1,
        )
        engine = self.make_engine(quotes={"USDJPY": quote})  # 只有 USDJPY，没有 USDCNY
        original_now = self.scheduler.market_clock.now_utc
        self.scheduler.market_clock.now_utc = lambda: OPEN_NOW.astimezone(timezone.utc)
        try:
            await engine.tick_once()
            self.assertIn("RATE_UNAVAILABLE", engine.last_error)
            events = engine.drain_events()
            self.assertTrue(any("汇率不可用" in text for text in events))
            await engine.tick_once()
            self.assertEqual(engine.drain_events(), [], "同一段缺失只播报一次")
        finally:
            self.scheduler.market_clock.now_utc = original_now


class SchedulerRatesTestCase(unittest.IsolatedAsyncioTestCase):
    """完整汇率（USDJPY + USDCNY）下的编排器端到端：开仓 → 平仓 → 账本一致。"""

    def setUp(self) -> None:
        self.ledger_mod = submodule("ledger")
        self.scheduler = submodule("scheduler")
        self.models = submodule("models")
        self.config_mod = submodule("config")
        self.config = self.config_mod.MaiFxConfig()
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = self.ledger_mod.Ledger(Path(self.tmp.name) / "ledger.sqlite3")
        self.ledger.init(int(self.config.account.initial_balance))
        self._original_now = self.scheduler.market_clock.now_utc
        self.scheduler.market_clock.now_utc = lambda: OPEN_NOW.astimezone(timezone.utc)

    def tearDown(self) -> None:
        self.scheduler.market_clock.now_utc = self._original_now
        self.ledger.close()
        self.tmp.cleanup()

    def make_engine(self):
        models = self.models
        state = {"n": 0}

        class _FreshSource:
            """每次返回时间戳/序号递增的新快照（否则引擎按重复行情丢弃整 tick）。"""

            async def fetch(self, codes, now, seq):
                state["n"] += 1
                tick = OPEN_NOW + timedelta(seconds=state["n"])
                return {
                    "USDJPY": models.QuoteSnapshot(
                        pair="USDJPY",
                        bid=Decimal("158.000"),
                        ask=Decimal("158.010"),
                        mid=Decimal("158.005"),
                        quote_ts=tick,
                        recv_ts=tick,
                        seq=state["n"],
                    ),
                    "USDCNY": models.QuoteSnapshot(
                        pair="USDCNY",
                        bid=Decimal("6.6650"),
                        ask=Decimal("6.6750"),
                        mid=Decimal("6.6700"),
                        quote_ts=tick,
                        recv_ts=tick,
                        seq=state["n"],
                    ),
                }

        return self.scheduler.TradingEngine(
            ctx=SimpleNamespace(),
            config=self.config,
            quote_source=_FreshSource(),
            ledger=self.ledger,
        )

    async def test_open_fill_and_close_in_cny(self) -> None:
        engine = self.make_engine()
        await engine.tick_once()
        self.assertIn("USDCNY", engine.quotes)
        self.assertIsNotNone(engine._rates())
        ok, message = engine.manual_order(pair_text="美元日元", side="buy", units=10_000, user="smoke")
        self.assertTrue(ok, message)
        await engine.tick_once()
        self.assertEqual(len(engine.positions), 1)
        position = engine.positions[0]
        self.assertGreater(position.margin, 0)
        self.assertLess(position.margin, 20_000, "保证金应为人民币量级")
        equity = engine.equity_cny()
        self.assertIsNotNone(equity)
        self.assertGreater(int(equity), 90_000)
        ok, message = engine.close_position("美元日元", user="smoke")
        self.assertTrue(ok, message)
        await engine.tick_once()
        self.assertEqual(engine.positions, [])
        self.assertEqual(
            self.ledger.check_accounting(engine.account, engine.positions), []
        )
        account = self.ledger.load_account()
        self.assertLess(account.balance, 100_001)
        self.assertGreaterEqual(account.fees_paid, 0)


if __name__ == "__main__":
    unittest.main()
