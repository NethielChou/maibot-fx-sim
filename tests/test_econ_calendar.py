"""财经日历单测：事件解析容错、事件窗边界、高影响禁开仓、即将公布播报（纯离线）。

模块已从 ``calendar.py`` 改名为 ``econ_calendar.py``（不再与标准库 ``calendar`` 同名），
本文件按文件名直接加载，**不**在测试进程里注册成 ``sys.modules["calendar"]``。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import report  # noqa: E402

CST = ZoneInfo("Asia/Shanghai")


def load_calendar_module():
    name = "_mai_fx_econ_calendar_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(PROJECT_ROOT / "econ_calendar.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 econ_calendar.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cal = load_calendar_module()
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=CST)


def at(hours: float = 0.0, minutes: float = 0.0) -> datetime:
    return NOW + timedelta(hours=hours, minutes=minutes)


class ParseEventTestCase(unittest.TestCase):
    def test_full_line(self) -> None:
        event = cal.parse_event("2026-11-12T21:30:00|USD|美国 CPI|high")
        self.assertEqual(event.at, datetime(2026, 11, 12, 21, 30, tzinfo=CST))
        self.assertEqual(event.currency, "USD")
        self.assertEqual(event.name, "美国 CPI")
        self.assertEqual(event.impact, "high")
        self.assertTrue(event.is_high)
        self.assertEqual(event.impact_text, "高")
        self.assertEqual(event.time_text(), "11-12 21:30")
        self.assertIn("USD 美国 CPI", event.label())

    def test_three_and_two_field_lines(self) -> None:
        three = cal.parse_event("2026-11-19T03:00:00|USD|美联储会议纪要")
        self.assertEqual(three.currency, "USD")
        self.assertEqual(three.name, "美联储会议纪要")
        self.assertEqual(three.impact, "high")  # 缺省=高影响
        two = cal.parse_event("2026-12-04T21:30:00|非农就业")
        self.assertEqual(two.currency, "")
        self.assertEqual(two.name, "非农就业")

    def test_chinese_impact_levels(self) -> None:
        medium = cal.parse_event("2026-12-10T03:00:00|USD|美联储议息|中")
        self.assertEqual(medium.impact, "medium")
        self.assertEqual(medium.impact_text, "中")
        low = cal.parse_event("2026-12-10T03:00:00|USD|美联储议息|low")
        self.assertEqual(low.impact, "low")
        self.assertFalse(low.is_high)

    def test_space_separator_and_offset(self) -> None:
        event = cal.parse_event("2026-11-07 20:30:00+08:00|USD|非农|high")
        self.assertEqual(event.at, datetime(2026, 11, 7, 20, 30, tzinfo=CST))

    def test_naive_time_uses_beijing(self) -> None:
        event = cal.parse_event("2026-11-07T20:30:00|USD|非农|high")
        self.assertEqual(event.at.tzinfo, CST)

    def test_invalid_time_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cal.parse_event("下周三|USD|非农|high")
        self.assertIn("INVALID_EVENT", str(ctx.exception))

    def test_invalid_impact_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cal.parse_event("2026-11-07T20:30:00|USD|非农|极高")
        self.assertIn("INVALID_EVENT", str(ctx.exception))

    def test_missing_name_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cal.parse_event("2026-11-07T20:30:00|USD||high")
        self.assertIn("INVALID_EVENT", str(ctx.exception))

    def test_too_many_fields_raises(self) -> None:
        with self.assertRaises(ValueError):
            cal.parse_event("2026-11-07T20:30:00|USD|非农|high|多余")

    def test_parse_events_ignores_bad_rows_and_records_errors(self) -> None:
        lines = [
            "2026-11-12T21:30:00|USD|美国 CPI|high",
            "写错的一行",
            "# 这是注释",
            "",
            "2026-12-04T21:30:00|USD|非农就业|high",
        ]
        events, errors = cal.parse_events(lines)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("第 2 条", errors[0])
        self.assertEqual(events[0].name, "美国 CPI")  # 已按时间升序
        self.assertEqual(events[1].name, "非农就业")

    def test_to_dict(self) -> None:
        event = cal.parse_event("2026-11-12T21:30:00|USD|美国 CPI|high")
        data = event.to_dict()
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["impact"], "high")
        self.assertIn("at", data)


class QueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.events = [
            cal.CalendarEvent(at=at(hours=2), currency="USD", name="非农", impact="high"),
            cal.CalendarEvent(at=at(hours=10), currency="JPY", name="日银", impact="medium"),
            cal.CalendarEvent(at=at(hours=30), currency="EUR", name="欧洲央行", impact="low"),
        ]

    def test_events_between(self) -> None:
        found = cal.events_between(self.events, NOW, 12)
        self.assertEqual([item.name for item in found], ["非农", "日银"])
        self.assertEqual(cal.events_between(self.events, NOW, 0), [])
        self.assertEqual(len(cal.events_between(self.events, NOW, 48)), 3)

    def test_next_event(self) -> None:
        self.assertEqual(cal.next_event(self.events, NOW).name, "非农")
        self.assertEqual(cal.next_event(self.events, at(hours=3)).name, "日银")
        self.assertIsNone(cal.next_event(self.events, at(hours=48)))

    def test_in_event_window_boundaries(self) -> None:
        event = cal.CalendarEvent(at=at(hours=1), currency="USD", name="非农", impact="high")
        events = [event]
        self.assertIs(cal.in_event_window(events, at(hours=1) - timedelta(minutes=30), 30, 30), event)
        self.assertIsNone(cal.in_event_window(events, at(hours=1) - timedelta(minutes=31), 30, 30))
        self.assertIs(cal.in_event_window(events, at(hours=1) + timedelta(minutes=30), 30, 30), event)
        self.assertIsNone(cal.in_event_window(events, at(hours=1) + timedelta(minutes=31), 30, 30))
        self.assertIs(cal.in_event_window(events, at(hours=1)), event)  # 公布当下

    def test_high_only_filter(self) -> None:
        medium = cal.CalendarEvent(at=at(hours=1), currency="USD", name="CPI", impact="medium")
        self.assertIsNone(cal.in_event_window([medium], at(hours=1), 30, 30, high_only=True))
        self.assertIs(cal.in_event_window([medium], at(hours=1), 30, 30, high_only=False), medium)


class CalendarObjectTestCase(unittest.TestCase):
    def test_disabled_never_blocks(self) -> None:
        calendar = cal.EconomicCalendar(
            [cal.CalendarEvent(at=NOW, currency="USD", name="非农", impact="high")], enabled=False
        )
        blocked, event = calendar.blocks_new_position(NOW)
        self.assertFalse(blocked)
        self.assertIsNone(event)
        self.assertEqual(calendar.prompt_lines(NOW), [])

    def test_high_impact_blocks_and_prompt_mentions(self) -> None:
        calendar = cal.EconomicCalendar(
            [cal.CalendarEvent(at=at(minutes=10), currency="USD", name="非农", impact="high")], enabled=True
        )
        blocked, event = calendar.blocks_new_position(NOW)
        self.assertTrue(blocked)
        self.assertEqual(event.name, "非农")
        text = "\n".join(calendar.prompt_lines(NOW))
        self.assertIn("【财经日历】", text)
        self.assertIn("禁止开新仓", text)
        self.assertIn("非农", text)

    def test_medium_impact_not_blocked_by_default(self) -> None:
        calendar = cal.EconomicCalendar(
            [cal.CalendarEvent(at=NOW, currency="USD", name="CPI", impact="medium")], enabled=True
        )
        self.assertFalse(calendar.blocks_new_position(NOW)[0])
        all_block = cal.EconomicCalendar(
            [cal.CalendarEvent(at=NOW, currency="USD", name="CPI", impact="medium")],
            enabled=True,
            block_high_impact_only=False,
        )
        self.assertTrue(all_block.blocks_new_position(NOW)[0])

    def test_event_soon_window(self) -> None:
        event = cal.CalendarEvent(at=at(minutes=40), currency="USD", name="非农", impact="high")
        calendar = cal.EconomicCalendar([event], enabled=True, announce_minutes=60)
        self.assertIs(calendar.event_soon(NOW), event)
        self.assertIsNone(calendar.event_soon(NOW, minutes=30))
        self.assertIsNone(cal.EconomicCalendar([event], enabled=False).event_soon(NOW))

    def test_events_are_sorted_and_errors_kept(self) -> None:
        calendar = cal.EconomicCalendar(
            [
                "2026-12-04T21:30:00|USD|非农就业|high",
                "坏数据",
                cal.CalendarEvent(at=at(hours=1), currency="JPY", name="日银", impact="high"),
            ],
            enabled=True,
        )
        self.assertEqual(len(calendar), 2)
        self.assertEqual(calendar.events[0].name, "日银")
        self.assertTrue(calendar.errors)

    def test_from_config_reads_section(self) -> None:
        section = SimpleNamespace(
            enabled=True,
            blackout_minutes=15,
            block_high_impact_only=False,
            announce_minutes=30,
            events=["2026-11-12T21:30:00|USD|美国 CPI|high"],
        )
        calendar = cal.EconomicCalendar.from_config(SimpleNamespace(calendar=section))
        self.assertTrue(calendar.enabled)
        self.assertEqual(calendar.before_minutes, 15)
        self.assertEqual(calendar.after_minutes, 15)
        self.assertFalse(calendar.block_high_impact_only)
        self.assertEqual(calendar.announce_minutes, 30)
        self.assertEqual(len(calendar), 1)
        self.assertEqual(calendar.events[0].name, "美国 CPI")

    def test_from_config_empty_events_means_no_events(self) -> None:
        section = SimpleNamespace(
            enabled=True, blackout_minutes=30, block_high_impact_only=True, announce_minutes=60, events=[]
        )
        calendar = cal.EconomicCalendar.from_config(section)
        self.assertEqual(len(calendar), 0)
        self.assertEqual(calendar.errors, ())
        self.assertIsNone(calendar.next_event(NOW))
        self.assertIsNone(calendar.event_soon(NOW))

    def test_from_config_missing_section_defaults_off(self) -> None:
        calendar = cal.EconomicCalendar.from_config(SimpleNamespace())
        self.assertFalse(calendar.enabled)
        self.assertEqual(len(calendar), 0)

    def test_from_config_zero_window_means_no_early_block(self) -> None:
        section = SimpleNamespace(
            enabled=True,
            blackout_minutes=0,
            block_high_impact_only=True,
            announce_minutes=0,
            events=["2026-11-12T21:30:00|USD|美国 CPI|high"],
        )
        calendar = cal.EconomicCalendar.from_config(section)
        self.assertEqual(calendar.before_minutes, 0)
        self.assertEqual(calendar.after_minutes, 0)
        self.assertEqual(calendar.announce_minutes, 0)
        self.assertIsNone(calendar.event_soon(NOW))

    def test_build_helper(self) -> None:
        calendar = cal.build(["2026-11-12T21:30:00|USD|美国 CPI|high"], enabled=True)
        self.assertIsInstance(calendar, cal.EconomicCalendar)
        self.assertEqual(len(calendar), 1)

    def test_module_has_no_stdlib_shadow_layer(self) -> None:
        # 改名后不再需要「把标准库 calendar 属性补到自己身上」的兼容层
        self.assertFalse(hasattr(cal, "INSTALLED_STDLIB_COMPAT"))
        self.assertFalse(hasattr(cal, "DEFAULT_EVENTS"))


class EventSoonTextTestCase(unittest.TestCase):
    def test_event_soon_text_is_chinese_with_disclaimer(self) -> None:
        event = cal.CalendarEvent(at=at(minutes=30), currency="USD", name="非农", impact="high")
        text = report.event_soon_text(event)
        self.assertIn("即将公布", text)
        self.assertIn("USD 非农", text)
        self.assertIn("高影响", text)
        self.assertIn("北京时间", text)
        self.assertTrue(text.endswith(report.DISCLAIMER))

    def test_event_soon_text_accepts_dict(self) -> None:
        text = report.event_soon_text(
            {"currency": "JPY", "name": "日银", "impact": "medium", "at": at(minutes=10)}
        )
        self.assertIn("JPY 日银", text)
        self.assertIn("中影响", text)


if __name__ == "__main__":
    unittest.main()
