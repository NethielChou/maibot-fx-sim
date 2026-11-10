"""market_clock.py 单元测试：开盘/休市、日切、时间窗、日切日期（DST 覆盖）。

全部使用固定 UTC 时间，不读系统时钟、不联网。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from market_clock import (
    MARKET_TZ,
    SHANGHAI_TZ,
    current_rollover_date,
    is_market_open,
    is_monday_open_window,
    is_rollover_window,
    market_status,
    next_rollover,
    now_utc,
    ny_date,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestNowUtc(unittest.TestCase):
    def test_now_utc_is_aware_utc(self) -> None:
        ts = now_utc()
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(ts.utcoffset(), timedelta(0))


class TestMarketOpen(unittest.TestCase):
    """美冬令时（EST = UTC−5）：2026-01-xx。"""

    def test_weekday_daytime_open(self) -> None:
        # 2026-01-14 周三 10:00 NY = 15:00 UTC
        self.assertTrue(is_market_open(utc(2026, 1, 14, 15, 0)))
        self.assertEqual(market_status(utc(2026, 1, 14, 15, 0)), "open")

    def test_sunday_before_open_closed(self) -> None:
        # 周日 16:59 NY = 21:59 UTC
        self.assertFalse(is_market_open(utc(2026, 1, 18, 21, 59)))
        self.assertEqual(market_status(utc(2026, 1, 18, 21, 59)), "closed")

    def test_sunday_open_boundary(self) -> None:
        # 周日 17:00 NY = 22:00 UTC（左闭）
        self.assertTrue(is_market_open(utc(2026, 1, 18, 22, 0)))

    def test_friday_close_boundary(self) -> None:
        # 周五 16:59 NY 开；17:00 NY 收（右开）
        self.assertTrue(is_market_open(utc(2026, 1, 16, 21, 59)))
        self.assertFalse(is_market_open(utc(2026, 1, 16, 22, 0)))

    def test_saturday_closed(self) -> None:
        self.assertFalse(is_market_open(utc(2026, 1, 17, 17, 0)))

    def test_weekday_rollover_hour_is_not_closed(self) -> None:
        # 周三 17:00 NY 只是点差放大窗口，不是休市
        self.assertTrue(is_market_open(utc(2026, 1, 14, 22, 0)))


class TestMarketOpenDst(unittest.TestCase):
    """美夏令时（EDT = UTC−4）：2026-07-xx，与冬令时相差 1 小时。"""

    def test_summer_sunday_open_boundary(self) -> None:
        # 周日 16:59 EDT = 20:59 UTC 收；17:00 EDT = 21:00 UTC 开
        self.assertFalse(is_market_open(utc(2026, 7, 12, 20, 59)))
        self.assertTrue(is_market_open(utc(2026, 7, 12, 21, 0)))

    def test_summer_friday_close_boundary(self) -> None:
        self.assertTrue(is_market_open(utc(2026, 7, 10, 20, 59)))
        self.assertFalse(is_market_open(utc(2026, 7, 10, 21, 0)))

    def test_dst_switch_day_2026_03_08(self) -> None:
        # 2026-03-08（周日）02:00 EST → EDT，当天 17:00 已是 EDT = 21:00 UTC
        self.assertFalse(is_market_open(utc(2026, 3, 8, 20, 59)))
        self.assertTrue(is_market_open(utc(2026, 3, 8, 21, 0)))

    def test_dst_switch_day_2026_11_01(self) -> None:
        # 2026-11-01（周日）02:00 EDT → EST，当天 17:00 已是 EST = 22:00 UTC
        self.assertFalse(is_market_open(utc(2026, 11, 1, 21, 59)))
        self.assertTrue(is_market_open(utc(2026, 11, 1, 22, 0)))


class TestNextRollover(unittest.TestCase):
    def test_next_rollover_before_today(self) -> None:
        # 2026-01-14 10:00 NY（15:00 UTC）→ 当天 17:00 NY（22:00 UTC）
        result = next_rollover(utc(2026, 1, 14, 15, 0))
        self.assertEqual(result.astimezone(timezone.utc), utc(2026, 1, 14, 22, 0))
        self.assertEqual(result.hour, 17)
        self.assertEqual(result.astimezone(MARKET_TZ).minute, 0)

    def test_next_rollover_strictly_after(self) -> None:
        # 恰好在 17:00 时，下一个 = 次日 17:00
        result = next_rollover(utc(2026, 1, 14, 22, 0))
        self.assertEqual(result.astimezone(timezone.utc), utc(2026, 1, 15, 22, 0))
        self.assertGreater(result, utc(2026, 1, 14, 22, 0))

    def test_next_rollover_across_dst(self) -> None:
        # 2026-03-06（周五）18:00 EST → 下一个 17:00 = 03-07 17:00 EST = 22:00 UTC
        result = next_rollover(utc(2026, 3, 6, 23, 0))
        self.assertEqual(result.astimezone(timezone.utc), utc(2026, 3, 7, 22, 0))

    def test_next_rollover_result_aware_in_market_tz(self) -> None:
        result = next_rollover(utc(2026, 7, 10, 12, 0))
        self.assertEqual(result.tzinfo, MARKET_TZ)


class TestRolloverWindow(unittest.TestCase):
    def test_window_bounds_winter(self) -> None:
        # 2026-01-14 周三：16:45–17:15 EST = 21:45–22:15 UTC
        self.assertFalse(is_rollover_window(utc(2026, 1, 14, 21, 44, 59)))
        self.assertTrue(is_rollover_window(utc(2026, 1, 14, 21, 45, 0)))
        self.assertTrue(is_rollover_window(utc(2026, 1, 14, 21, 59, 59)))
        self.assertTrue(is_rollover_window(utc(2026, 1, 14, 22, 14, 59)))
        self.assertFalse(is_rollover_window(utc(2026, 1, 14, 22, 15, 0)))

    def test_window_custom_minutes(self) -> None:
        self.assertFalse(is_rollover_window(utc(2026, 1, 14, 16, 0)))
        # 30 分钟窗口：16:30–17:30 EST = 21:30–22:30 UTC
        self.assertTrue(is_rollover_window(utc(2026, 1, 14, 21, 30), minutes=30))
        self.assertTrue(is_rollover_window(utc(2026, 1, 14, 22, 29), minutes=30))
        self.assertFalse(is_rollover_window(utc(2026, 1, 14, 22, 30), minutes=30))

    def test_invalid_minutes_raises(self) -> None:
        with self.assertRaises(ValueError):
            is_rollover_window(utc(2026, 1, 14, 22, 0), minutes=0)


class TestMondayOpenWindow(unittest.TestCase):
    def test_default_hour_window(self) -> None:
        # 周日 17:00–18:00 NY = 22:00–23:00 UTC（2026-01-18 周日）
        self.assertFalse(is_monday_open_window(utc(2026, 1, 18, 21, 59)))
        self.assertTrue(is_monday_open_window(utc(2026, 1, 18, 22, 0)))
        self.assertTrue(is_monday_open_window(utc(2026, 1, 18, 22, 59, 59)))
        self.assertFalse(is_monday_open_window(utc(2026, 1, 18, 23, 0)))

    def test_monday_weekday_returns_false(self) -> None:
        # 纽约周一 00:30 不是"周一开盘"（开盘是周日 17:00）
        self.assertFalse(is_monday_open_window(utc(2026, 1, 19, 5, 30)))

    def test_custom_hours(self) -> None:
        self.assertTrue(is_monday_open_window(utc(2026, 1, 18, 23, 30), hours=2))
        self.assertFalse(is_monday_open_window(utc(2026, 1, 19, 0, 0), hours=2))

    def test_invalid_hours_raises(self) -> None:
        with self.assertRaises(ValueError):
            is_monday_open_window(utc(2026, 1, 18, 22, 0), hours=0)


class TestDates(unittest.TestCase):
    def test_ny_date_crosses_utc_midnight(self) -> None:
        # 2026-01-19 04:00 UTC = 2026-01-18 23:00 EST
        self.assertEqual(ny_date(utc(2026, 1, 19, 4, 0)), "2026-01-18")
        self.assertEqual(ny_date(utc(2026, 1, 19, 5, 0)), "2026-01-19")

    def test_ny_date_summer(self) -> None:
        # 2026-07-13 03:59 UTC = 2026-07-12 23:59 EDT
        self.assertEqual(ny_date(utc(2026, 7, 13, 3, 59)), "2026-07-12")
        self.assertEqual(ny_date(utc(2026, 7, 13, 4, 0)), "2026-07-13")

    def test_current_rollover_date_before_1700(self) -> None:
        # 2026-01-14 16:59 EST（21:59 UTC）→ 最近日切是 01-13 17:00
        self.assertEqual(current_rollover_date(utc(2026, 1, 14, 21, 59)), "2026-01-13")

    def test_current_rollover_date_at_boundary(self) -> None:
        # 17:00 整 → 归当天
        self.assertEqual(current_rollover_date(utc(2026, 1, 14, 22, 0)), "2026-01-14")

    def test_current_rollover_date_after_1700(self) -> None:
        # 2026-01-15 02:00 UTC = 2026-01-14 21:00 EST
        self.assertEqual(current_rollover_date(utc(2026, 1, 15, 2, 0)), "2026-01-14")

    def test_current_rollover_date_weekend(self) -> None:
        # 周六 12:00 NY（2026-01-17 17:00 UTC）→ 最近日切 = 周五 01-16
        self.assertEqual(current_rollover_date(utc(2026, 1, 17, 17, 0)), "2026-01-16")

    def test_current_rollover_date_summer(self) -> None:
        # 2026-07-12（周日）16:59 EDT = 20:59 UTC → 最近日切 = 07-11
        self.assertEqual(current_rollover_date(utc(2026, 7, 12, 20, 59)), "2026-07-11")
        self.assertEqual(current_rollover_date(utc(2026, 7, 12, 21, 0)), "2026-07-12")


class TestNaiveDatetime(unittest.TestCase):
    def test_naive_datetime_rejected(self) -> None:
        naive = datetime(2026, 1, 14, 10, 0)
        with self.assertRaises(ValueError) as ctx:
            is_market_open(naive)
        self.assertIn("NAIVE_DATETIME", str(ctx.exception))

        for func in (market_status, next_rollover, ny_date, current_rollover_date):
            with self.assertRaises(ValueError):
                func(naive)
        with self.assertRaises(ValueError):
            is_rollover_window(naive)
        with self.assertRaises(ValueError):
            is_monday_open_window(naive)


class TestTimezoneConstants(unittest.TestCase):
    def test_timezone_constants(self) -> None:
        self.assertEqual(MARKET_TZ, ZoneInfo("America/New_York"))
        self.assertEqual(SHANGHAI_TZ, ZoneInfo("Asia/Shanghai"))


if __name__ == "__main__":
    unittest.main()
