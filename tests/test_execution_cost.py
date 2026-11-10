"""execution_cost.py 单元测试：报价可用性 / 有效点差 / apply_spread / 滑点。

全部离线：固定 UTC 时间 + 固定 Decimal 快照；不联网、不读系统时钟。
"""

from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import execution_cost as ec
import market_clock
from models import QuoteSnapshot
from pairs import PAIRS

#: 2026-11-04 周三 10:00 UTC = 05:00 EST：开市、无 rollover/周初窗口。
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)

#: 2026-11-04 22:00 UTC = 17:00 EST：rollover 窗口内。
ROLLOVER_NOW = datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc)

#: 2026-11-08 周日 22:30 UTC = 17:30 EST：周初窗口内（开市），且已出 rollover 窗。
MONDAY_NOW = datetime(2026, 11, 8, 22, 30, tzinfo=timezone.utc)

#: 2026-11-07 周六 12:00 UTC = 07:00 EST：休市。
CLOSED_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def dec(value) -> Decimal:
    return Decimal(str(value))


def quote(
    code: str,
    bid,
    ask,
    *,
    now: datetime = NOW,
    quote_age: int = 2,
    recv_age: int | None = None,
    quote_ts_missing: bool = False,
) -> QuoteSnapshot:
    """构造快照；价格参数可为 None。"""
    bid_d = None if bid is None else dec(bid)
    ask_d = None if ask is None else dec(ask)
    mid = (bid_d + ask_d) / Decimal(2) if bid_d is not None and ask_d is not None else None
    ts = None if quote_ts_missing else now - timedelta(seconds=quote_age)
    recv = now - timedelta(seconds=quote_age if recv_age is None else recv_age)
    return QuoteSnapshot(
        pair=code,
        bid=bid_d,
        ask=ask_d,
        mid=mid,
        quote_ts=ts,
        recv_ts=recv,
        seq=1,
    )


def cfg(**over):
    """构造 config.MaiFxConfig 的最小替身（不 import config，避免 maibot_sdk）。"""
    spread = {
        "rollover_multiplier": 3.0,
        "monday_open_multiplier": 2.5,
        "news_core_multiplier": 3.0,
        "news_edge_multiplier": 2.0,
        "max_multiplier": 5.0,
        "market_max_slip_pips": 3.0,
    }
    spread.update({k: v for k, v in over.items() if k in spread})
    calendar_enabled = bool(over.get("calendar_enabled", False))
    return SimpleNamespace(
        spread=SimpleNamespace(**spread),
        calendar=SimpleNamespace(enabled=calendar_enabled),
    )


class FakeClock:
    """注入用假时钟：窗口全部关闭，可指定指标窗回调。"""

    def __init__(self, news=None):
        self._news = news

    def is_rollover_window(self, ts):
        return False

    def is_monday_open_window(self, ts):
        return False

    def news_multiplier(self, ts):
        return self._news


class TestQuoteUsable(unittest.TestCase):
    def test_usable_usdjpy(self) -> None:
        quotes = {"USDJPY": quote("USDJPY", "158.180", "158.190")}
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], quotes, NOW, 60, 5)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_missing_snapshot(self) -> None:
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "QUOTE_MISSING")

    def test_bid_ask_none_or_nonpositive(self) -> None:
        for bid, ask in ((None, "158.190"), ("158.180", None), ("0", "158.190"), ("158.180", "-1")):
            quotes = {"USDJPY": quote("USDJPY", bid, ask)}
            ok, reason = ec.quote_usable(PAIRS["USDJPY"], quotes, NOW, 60, 5)
            self.assertFalse(ok, (bid, ask))
            self.assertEqual(reason, "QUOTE_MISSING", (bid, ask))

    def test_closed_priority_over_stale(self) -> None:
        # 周末 + 僵尸时间戳（1 天前）：必须报 CLOSED，不是 STALE
        stale = quote("USDJPY", "158.180", "158.190", now=CLOSED_NOW, quote_age=86_400)
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {"USDJPY": stale}, CLOSED_NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "MARKET_CLOSED")

    def test_abnormal_inverted(self) -> None:
        bad = quote("USDJPY", "158.190", "158.180")
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {"USDJPY": bad}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "BAD_QUOTE:ASK_LT_BID")

    def test_raw_zero_spread_is_abnormal(self) -> None:
        bad = quote("USDJPY", "158.180", "158.180")
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {"USDJPY": bad}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "BAD_QUOTE:ZERO_SPREAD")

    def test_computed_zero_spread_allowed(self) -> None:
        # 计算品种源 bid==ask 是正常的（点差由 apply_spread 合成）
        computed = quote("EURJPY", "176.830", "176.830")
        ok, reason = ec.quote_usable(PAIRS["EURJPY"], {"EURJPY": computed}, NOW, 60, 5)
        self.assertTrue(ok, reason)

    def test_stale_quote_data(self) -> None:
        stale = quote("USDJPY", "158.180", "158.190", quote_age=90)
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {"USDJPY": stale}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "STALE_QUOTE")

    def test_missing_quote_ts_is_stale(self) -> None:
        no_ts = quote("USDJPY", "158.180", "158.190", quote_ts_missing=True)
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {"USDJPY": no_ts}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "STALE_QUOTE")

    def test_stale_fill_snapshot(self) -> None:
        old_snapshot = quote("USDJPY", "158.180", "158.190", quote_age=20, recv_age=10)
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {"USDJPY": old_snapshot}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "STALE_QUOTE_FILL")

    def test_spread_too_wide(self) -> None:
        wide = quote("USDJPY", "158.000", "158.120")  # 12.0 pip > max 8.0（门户/极端行情模拟）
        ok, reason = ec.quote_usable(PAIRS["USDJPY"], {"USDJPY": wide}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "SPREAD_TOO_WIDE")

    def test_eurusd_requires_usdjpy_leg(self) -> None:
        eurusd = quote("EURUSD", "1.11770", "1.11780")
        ok, reason = ec.quote_usable(PAIRS["EURUSD"], {"EURUSD": eurusd}, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "QUOTE_MISSING:USDJPY")

    def test_eurusd_usable_with_both_legs(self) -> None:
        quotes = {
            "EURUSD": quote("EURUSD", "1.11770", "1.11780"),
            "USDJPY": quote("USDJPY", "158.180", "158.190"),
        }
        ok, reason = ec.quote_usable(PAIRS["EURUSD"], quotes, NOW, 60, 5)
        self.assertTrue(ok, reason)

    def test_eurusd_leg_stale(self) -> None:
        quotes = {
            "EURUSD": quote("EURUSD", "1.11770", "1.11780"),
            "USDJPY": quote("USDJPY", "158.180", "158.190", quote_age=90),
        }
        ok, reason = ec.quote_usable(PAIRS["EURUSD"], quotes, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "STALE_QUOTE:USDJPY")

    def test_eurusd_leg_missing_bid(self) -> None:
        quotes = {
            "EURUSD": quote("EURUSD", "1.11770", "1.11780"),
            "USDJPY": quote("USDJPY", None, "158.190"),
        }
        ok, reason = ec.quote_usable(PAIRS["EURUSD"], quotes, NOW, 60, 5)
        self.assertFalse(ok)
        self.assertEqual(reason, "QUOTE_MISSING:USDJPY")


class TestBaseSpread(unittest.TestCase):
    def test_raw_uses_larger_of_source_and_base(self) -> None:
        q = quote("USDJPY", "158.180", "158.190")  # 源 1.0 pip
        self.assertEqual(ec.base_spread_pips(PAIRS["USDJPY"], q), dec("1.0"))

    def test_raw_floor_at_base_spread(self) -> None:
        q = quote("USDJPY", "158.183", "158.185")  # 源 0.2 pip < 基准 0.3
        self.assertEqual(ec.base_spread_pips(PAIRS["USDJPY"], q), dec("0.3"))

    def test_computed_ignores_source_phantom_spread(self) -> None:
        q = quote("EURJPY", "176.830", "176.920")  # 源 9 pip 幻影
        self.assertEqual(ec.base_spread_pips(PAIRS["EURJPY"], q), dec("0.7"))


class TestEffectiveSpread(unittest.TestCase):
    def test_normal_time_raw(self) -> None:
        q = quote("USDJPY", "158.180", "158.190")
        self.assertEqual(ec.effective_spread_pips(PAIRS["USDJPY"], q, NOW, cfg()), dec("1.0"))

    def test_normal_time_computed(self) -> None:
        q = quote("EURJPY", "176.830", "176.920")
        self.assertEqual(ec.effective_spread_pips(PAIRS["EURJPY"], q, NOW, cfg()), dec("0.7"))

    def test_rollover_multiplier(self) -> None:
        q = quote("USDJPY", "158.180", "158.190", now=ROLLOVER_NOW)
        self.assertEqual(
            ec.effective_spread_pips(PAIRS["USDJPY"], q, ROLLOVER_NOW, cfg()),
            dec("3.0"),
        )

    def test_monday_multiplier(self) -> None:
        q = quote("EURJPY", "176.830", "176.920", now=MONDAY_NOW)
        self.assertEqual(
            ec.effective_spread_pips(PAIRS["EURJPY"], q, MONDAY_NOW, cfg()),
            dec("1.75"),
        )

    def test_total_multiplier_capped(self) -> None:
        q = quote("USDJPY", "158.180", "158.190", now=ROLLOVER_NOW)
        self.assertEqual(
            ec.effective_spread_pips(PAIRS["USDJPY"], q, ROLLOVER_NOW, cfg(max_multiplier=2.0)),
            dec("2.0"),
        )

    def test_news_core_multiplier(self) -> None:
        q = quote("EURJPY", "176.830", "176.920")
        clock = FakeClock(news="core")
        self.assertEqual(
            ec.effective_spread_pips(PAIRS["EURJPY"], q, NOW, cfg(calendar_enabled=True), clock),
            dec("2.1"),
        )

    def test_news_edge_multiplier(self) -> None:
        q = quote("EURJPY", "176.830", "176.920")
        clock = FakeClock(news="edge")
        self.assertEqual(
            ec.effective_spread_pips(PAIRS["EURJPY"], q, NOW, cfg(calendar_enabled=True), clock),
            dec("1.4"),
        )

    def test_news_numeric_multiplier(self) -> None:
        q = quote("EURJPY", "176.830", "176.920")
        clock = FakeClock(news=2.0)
        self.assertEqual(
            ec.effective_spread_pips(PAIRS["EURJPY"], q, NOW, cfg(calendar_enabled=True), clock),
            dec("1.4"),
        )

    def test_news_ignored_when_calendar_disabled(self) -> None:
        q = quote("EURJPY", "176.830", "176.920")
        clock = FakeClock(news="core")
        self.assertEqual(
            ec.effective_spread_pips(PAIRS["EURJPY"], q, NOW, cfg(calendar_enabled=False), clock),
            dec("0.7"),
        )

    def test_missing_quote_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ec.effective_spread_pips(PAIRS["USDJPY"], quote("USDJPY", None, None), NOW, cfg())
        self.assertIn("QUOTE_MISSING", str(ctx.exception))


class TestApplySpread(unittest.TestCase):
    def test_computed_pair_synthesizes_spread(self) -> None:
        q = quote("EURJPY", "176.870", "176.880")
        bid, ask = ec.apply_spread(q, PAIRS["EURJPY"], dec("0.7"))
        self.assertEqual(bid, dec("176.871"))
        self.assertEqual(ask, dec("176.879"))

    def test_raw_pair_returns_source_when_wide_enough(self) -> None:
        q = quote("USDJPY", "158.180", "158.190")
        bid, ask = ec.apply_spread(q, PAIRS["USDJPY"], dec("1.0"))
        self.assertEqual((bid, ask), (dec("158.180"), dec("158.190")))

    def test_raw_pair_widened_when_target_wider(self) -> None:
        q = quote("USDJPY", "158.180", "158.190")
        bid, ask = ec.apply_spread(q, PAIRS["USDJPY"], dec("2.0"))
        self.assertEqual((bid, ask), (dec("158.175"), dec("158.195")))

    def test_computed_tick_alignment(self) -> None:
        q = quote("TRYJPY", "3.1555", "3.1565")
        bid, ask = ec.apply_spread(q, PAIRS["TRYJPY"], dec("0.8"))
        self.assertEqual(bid, bid.quantize(PAIRS["TRYJPY"].tick))
        self.assertEqual(ask, ask.quantize(PAIRS["TRYJPY"].tick))
        self.assertLess(bid, ask)

    def test_invalid_spread_rejected(self) -> None:
        q = quote("EURJPY", "176.870", "176.880")
        for bad in (dec("0"), dec("-1")):
            with self.assertRaises(ValueError) as ctx:
                ec.apply_spread(q, PAIRS["EURJPY"], bad)
            self.assertIn("INVALID_SPREAD", str(ctx.exception))

    def test_missing_bid_ask_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ec.apply_spread(quote("EURJPY", None, None), PAIRS["EURJPY"], dec("0.7"))
        self.assertIn("QUOTE_MISSING", str(ctx.exception))


class TestTickHelpers(unittest.TestCase):
    def test_round_helpers(self) -> None:
        tick = PAIRS["EURJPY"].tick
        self.assertEqual(ec.round_up_to_tick(dec("176.8785"), tick), dec("176.879"))
        self.assertEqual(ec.round_down_to_tick(dec("176.8785"), tick), dec("176.878"))
        self.assertEqual(ec.round_up_to_tick(dec("176.878"), tick), dec("176.878"))

    def test_is_tick_aligned(self) -> None:
        self.assertTrue(ec.is_tick_aligned(dec("158.180"), PAIRS["USDJPY"]))
        self.assertFalse(ec.is_tick_aligned(dec("158.1805"), PAIRS["USDJPY"]))


class _MaxRng:
    """伪造 rng：randrange 永远返回上界−1（用于触发 jitter 上限）。"""

    def randrange(self, start: int, stop: int) -> int:
        return stop - 1


class TestSlippage(unittest.TestCase):
    def test_base_slippage_per_order_type(self) -> None:
        conf = cfg()
        self.assertEqual(ec.slippage_pips("market", PAIRS["USDJPY"], conf), dec("0.2"))
        self.assertEqual(ec.slippage_pips("stop", PAIRS["USDJPY"], conf), dec("0.5"))
        self.assertEqual(ec.slippage_pips("trailing_stop", PAIRS["USDJPY"], conf), dec("0.5"))
        self.assertEqual(ec.slippage_pips("rocut", PAIRS["USDJPY"], conf), dec("1.5"))
        self.assertEqual(ec.slippage_pips("loss_cut", PAIRS["USDJPY"], conf), dec("1.5"))
        self.assertEqual(ec.slippage_pips("limit", PAIRS["USDJPY"], conf), dec("0"))

    def test_order_type_normalized(self) -> None:
        self.assertEqual(ec.slippage_pips(" MARKET ", PAIRS["USDJPY"], cfg()), dec("0.2"))

    def test_unknown_order_type_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ec.slippage_pips("market_ifd", PAIRS["USDJPY"], cfg())
        self.assertIn("UNKNOWN_ORDER_TYPE", str(ctx.exception))

    def test_deterministic_jitter_with_same_seed(self) -> None:
        a = ec.slippage_pips("market", PAIRS["USDJPY"], cfg(), random.Random(42))
        b = ec.slippage_pips("market", PAIRS["USDJPY"], cfg(), random.Random(42))
        self.assertEqual(a, b)
        self.assertGreaterEqual(a, dec("0.2"))
        self.assertLessEqual(a, dec("0.5"))  # 0.2 + jitter_max 0.3

    def test_rocut_jitter_range(self) -> None:
        slip = ec.slippage_pips("rocut", PAIRS["USDJPY"], cfg(), random.Random(7))
        self.assertGreaterEqual(slip, dec("1.5"))
        self.assertLessEqual(slip, dec("2.0"))  # 1.5 + jitter_max 0.5

    def test_market_slippage_capped_by_config(self) -> None:
        slip = ec.slippage_pips(
            "market", PAIRS["USDJPY"], cfg(market_max_slip_pips=0.21), _MaxRng()
        )
        self.assertEqual(slip, dec("0.21"))

    def test_limit_never_jitters(self) -> None:
        self.assertEqual(ec.slippage_pips("limit", PAIRS["USDJPY"], cfg(), _MaxRng()), dec("0"))


class TestMarketClockDependency(unittest.TestCase):
    """quote_usable 直接使用 market_clock 判休市（不是注入）。"""

    def test_market_clock_module_used(self) -> None:
        self.assertTrue(market_clock.is_market_open(NOW))
        self.assertFalse(market_clock.is_market_open(CLOSED_NOW))


if __name__ == "__main__":
    unittest.main()
