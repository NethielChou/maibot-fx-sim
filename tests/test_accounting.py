"""accounting.py / fxconv.py 单测：人民币口径手算用例 + 缺汇率拒绝 + 边界/异常。

全部离线：手工构造 PairSpec / QuoteSnapshot / Position / AccountState 与固定汇率 fixture，
不联网、不读 DB。汇率 fixture：**USDJPY = 158.00，USDCNY = 6.67**（示意值，非实时行情），
因此 ``jpy_per_cny = 158.00 / 6.67 ≈ 23.6881559``，``cny_per_usd = 6.67``。

运行：项目根 `python -m unittest discover -s tests -t . -v`
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import accounting  # noqa: E402
from fxconv import FxRates, RateUnavailable  # noqa: E402
from models import (  # noqa: E402
    SIDE_BUY,
    SIDE_SELL,
    AccountState,
    PairSpec,
    Position,
    QuoteSnapshot,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
RATES = FxRates(usdjpy_mid=Decimal("158.00"), usdcny_mid=Decimal("6.67"))
JPY_PER_CNY = RATES.jpy_per_cny


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def make_usdjpy() -> PairSpec:
    return PairSpec(
        code="USDJPY",
        name_cn="美元/日元",
        digits=3,
        tick=Decimal("0.001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_susdjpy",
        source_kind="raw",
        base_spread_pips=Decimal("0.3"),
        max_spread_pips=Decimal("2.0"),
        swap_long_jpy=Decimal("160"),
        swap_short_jpy=Decimal("-210"),
        max_units=100_000,
    )


def make_eurjpy() -> PairSpec:
    return PairSpec(
        code="EURJPY",
        name_cn="欧元/日元",
        digits=3,
        tick=Decimal("0.001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_seurjpy",
        source_kind="computed",
        base_spread_pips=Decimal("0.7"),
        max_spread_pips=Decimal("2.0"),
        swap_long_jpy=Decimal("80"),
        swap_short_jpy=Decimal("-130"),
        max_units=100_000,
    )


def make_eurusd() -> PairSpec:
    return PairSpec(
        code="EURUSD",
        name_cn="欧元/美元",
        digits=5,
        tick=Decimal("0.00001"),
        pip=Decimal("0.0001"),
        quote_currency="USD",
        sina_code="fx_seurusd",
        source_kind="raw",
        base_spread_pips=Decimal("0.5"),
        max_spread_pips=Decimal("2.0"),
        swap_long_jpy=Decimal("-101"),
        swap_short_jpy=Decimal("55"),
        max_units=100_000,
    )


def make_tryjpy() -> PairSpec:
    return PairSpec(
        code="TRYJPY",
        name_cn="土耳其里拉/日元",
        digits=4,
        tick=Decimal("0.0001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_stryjpy",
        source_kind="computed",
        base_spread_pips=Decimal("0.8"),
        max_spread_pips=Decimal("4.0"),
        swap_long_jpy=Decimal("33"),
        swap_short_jpy=Decimal("-60"),
        max_units=200_000,
    )


PAIRS = {
    "USDJPY": make_usdjpy(),
    "EURJPY": make_eurjpy(),
    "EURUSD": make_eurusd(),
    "TRYJPY": make_tryjpy(),
}


def q(pair: str, bid: str, ask: str, mid: str | None = None) -> QuoteSnapshot:
    b, a = Decimal(bid), Decimal(ask)
    return QuoteSnapshot(
        pair=pair,
        bid=b,
        ask=a,
        mid=Decimal(mid) if mid is not None else (b + a) / Decimal(2),
        quote_ts=NOW,
        recv_ts=NOW,
        source="sina",
        seq=1,
    )


def pos(
    pair: str,
    side: str,
    units: int,
    entry: str,
    position_id: str = "P1",
    opened_at: datetime = NOW,
) -> Position:
    return Position(
        position_id=position_id,
        pair=pair,
        side=side,
        units=units,
        entry_price=Decimal(entry),
        opened_at=opened_at,
    )


def account(balance: int = 100_000) -> AccountState:
    return AccountState(balance=balance, initial_balance=100_000)


# ---------------------------------------------------------------------------
# 换算基础：汇率锚
# ---------------------------------------------------------------------------
class TestFxConv(unittest.TestCase):
    def test_jpy_per_cny(self):
        """1 元 = USDJPY / USDCNY = 158.00 / 6.67 ≈ 23.6881559 日元。"""
        self.assertAlmostEqual(float(JPY_PER_CNY), 23.6881559, places=5)
        self.assertEqual(RATES.jpy_to_cny(JPY_PER_CNY), Decimal("1"))

    def test_usd_to_cny(self):
        self.assertEqual(RATES.usd_to_cny(Decimal("100")), Decimal("667.00"))

    def test_account_rate_jpy_and_usd(self):
        """1 单位基础货币的元值：JPY 报价 = 价格 ÷ jpy_per_cny；USD 报价 = 价格 × 6.67。"""
        self.assertEqual(
            accounting.to_account_rate(PAIRS["USDJPY"], Decimal("158.00"), RATES).quantize(Decimal("0.0001")),
            Decimal("6.6700"),
        )
        self.assertEqual(
            accounting.to_account_rate(PAIRS["EURUSD"], Decimal("1.00000"), RATES),
            Decimal("6.67000"),
        )

    def test_pip_value(self):
        """1 pip 元值：USDJPY = 0.01 ÷ 23.6881559 ≈ 0.00042215；EURUSD = 0.0001 × 6.67。"""
        self.assertEqual(
            RATES.pip_value(PAIRS["USDJPY"]).quantize(Decimal("0.00000001")),
            Decimal("0.00042215"),
        )
        self.assertEqual(RATES.pip_value(PAIRS["EURUSD"]), Decimal("0.000667"))

    def test_invalid_rates_rejected(self):
        with self.assertRaises(ValueError):
            FxRates(usdjpy_mid=Decimal("0"), usdcny_mid=Decimal("6.67"))
        with self.assertRaises(ValueError):
            FxRates(usdjpy_mid=Decimal("158"), usdcny_mid=Decimal("-1"))

    def test_missing_rates_raise(self):
        with self.assertRaises(RateUnavailable) as ctx:
            accounting.to_account_rate(PAIRS["USDJPY"], Decimal("158.00"), None)
        self.assertIn("RATE_UNAVAILABLE", str(ctx.exception))


# ---------------------------------------------------------------------------
# W1 / W2：持仓額、所需保证金
# ---------------------------------------------------------------------------
class TestNotionalAndMargin(unittest.TestCase):
    def test_w1_notional_usdjpy(self):
        """W1：USDJPY 1万单位 @158.185 → 1,581,850 日元 ÷ 23.6881559 = 66,778.0981… → 66,778.10 元。"""
        value = accounting.notional(PAIRS["USDJPY"], 10_000, Decimal("158.185"), RATES)
        self.assertEqual(value.quantize(Decimal("0.01")), Decimal("66778.10"))
        self.assertEqual(value, Decimal("1581850") / JPY_PER_CNY)

    def test_w1_required_margin(self):
        """W1：所需保证金 ceil(66,778.098…/25) = ceil(2,671.12…) = 2,672 元。"""
        self.assertEqual(
            accounting.required_margin(PAIRS["USDJPY"], 10_000, Decimal("158.185"), RATES, 25),
            2_672,
        )

    def test_w1_required_margin_default_leverage(self):
        """默认 10 倍：ceil(66,778.098…/10) = 6,678 元。"""
        self.assertEqual(
            accounting.required_margin(PAIRS["USDJPY"], 10_000, Decimal("158.185"), RATES, 10),
            6_678,
        )

    def test_w1_required_margin_1000_units_ceils(self):
        """W1 附加：0.1万单位 = ceil(6,677.809…/25) = 268 元（元以下向上取整）。"""
        self.assertEqual(
            accounting.required_margin(PAIRS["USDJPY"], 1_000, Decimal("158.185"), RATES, 25),
            268,
        )

    def test_w6_eurusd_notional_and_margin(self):
        """W6：EURUSD 1 万 @1.11775 = 11,177.5 USD × 6.67 = 74,553.925 元 → 保证金 ceil → 2,983。"""
        spec = PAIRS["EURUSD"]
        notional = accounting.notional(spec, 10_000, Decimal("1.11775"), RATES)
        self.assertEqual(notional, Decimal("74553.9250000"))
        self.assertEqual(accounting.required_margin(spec, 10_000, Decimal("1.11775"), RATES, 25), 2_983)
        self.assertEqual(accounting.required_margin(spec, 10_000, Decimal("1.11775"), RATES, 10), 7_456)

    def test_invalid_units_raises(self):
        with self.assertRaises(ValueError) as ctx:
            accounting.notional(PAIRS["USDJPY"], 0, Decimal("158.185"), RATES)
        self.assertIn("INVALID_UNITS", str(ctx.exception))

    def test_invalid_leverage_raises(self):
        with self.assertRaises(ValueError) as ctx:
            accounting.required_margin(PAIRS["USDJPY"], 10_000, Decimal("158.185"), RATES, 0)
        self.assertIn("INVALID_LEVERAGE", str(ctx.exception))

    def test_missing_rates_blocks_margin(self):
        with self.assertRaises(RateUnavailable) as ctx:
            accounting.required_margin(PAIRS["USDJPY"], 10_000, Decimal("158.185"), None, 25)
        self.assertIn("RATE_UNAVAILABLE", str(ctx.exception))


# ---------------------------------------------------------------------------
# W7：双向持仓取大边 / 跨货币对相加
# ---------------------------------------------------------------------------
class TestMarginTotal(unittest.TestCase):
    def test_w7_two_sided_margin_max_side(self):
        """W7：USDJPY 买 2 万 + 卖 1 万 → ceil(max(133,556.20, 66,778.10)/25) = 5,343。"""
        positions = [
            pos("USDJPY", SIDE_BUY, 20_000, "158.190", "P-LONG"),
            pos("USDJPY", SIDE_SELL, 10_000, "158.180", "P-SHORT"),
        ]
        quotes = {"USDJPY": q("USDJPY", "158.180", "158.190")}
        total = accounting.required_margin_total(positions, quotes, PAIRS, RATES, 25)
        self.assertEqual(total, 5_343)
        self.assertNotEqual(total, 5_343 + 2_672)  # 不是两侧合计
        self.assertNotEqual(total, 2_672)          # 不是净额

    def test_cross_pair_sum(self):
        """跨货币对相加后统一取整：ceil((66,778.10 + 74,668.12)/25) = ceil(5,657.85) = 5,658。"""
        positions = [
            pos("USDJPY", SIDE_BUY, 10_000, "158.190", "P1"),
            pos("EURJPY", SIDE_BUY, 10_000, "176.880", "P2"),
        ]
        quotes = {
            "USDJPY": q("USDJPY", "158.180", "158.190"),
            "EURJPY": q("EURJPY", "176.870", "176.880"),
        }
        total = accounting.required_margin_total(positions, quotes, PAIRS, RATES, 25)
        self.assertEqual(total, 5_658)

    def test_no_positions_zero(self):
        self.assertEqual(accounting.required_margin_total([], {}, PAIRS, RATES, 25), 0)

    def test_missing_quote_falls_back_to_entry(self):
        """无行情时按开仓价退化（仅恢复期展示）：1万 @158.185 → 2,672。"""
        positions = [pos("USDJPY", SIDE_BUY, 10_000, "158.185")]
        total = accounting.required_margin_total(positions, {}, PAIRS, RATES, 25)
        self.assertEqual(total, 2_672)

    def test_unknown_pair_raises(self):
        positions = [pos("XXXYYY", SIDE_BUY, 10_000, "1.0")]
        with self.assertRaises(ValueError) as ctx:
            accounting.required_margin_total(positions, {}, PAIRS, RATES, 25)
        self.assertIn("UNKNOWN_PAIR", str(ctx.exception))

    def test_missing_rates_raise(self):
        positions = [pos("USDJPY", SIDE_BUY, 10_000, "158.185")]
        with self.assertRaises(RateUnavailable):
            accounting.required_margin_total(positions, {}, PAIRS, None, 25)


# ---------------------------------------------------------------------------
# W1/W2/W6：浮动盈亏 / 账户净值 / 维持率
# ---------------------------------------------------------------------------
class TestValuation(unittest.TestCase):
    def test_w1_unrealized_long_minus_100_yen(self):
        """W1：多单 1 万 @158.190，bid=158.180 → −100 日元 ÷ 23.6881559 = −4.22 → −4 元。"""
        p = pos("USDJPY", SIDE_BUY, 10_000, "158.190")
        quote = q("USDJPY", "158.180", "158.190")
        self.assertEqual(accounting.unrealized_pnl(p, quote, PAIRS["USDJPY"], RATES), -4)

    def test_w2_unrealized_short_minus_100_yen(self):
        """W2：空单 1 万 @158.180，评估用 ask=158.190 → −100 日元 → −4 元（点差成本）。"""
        p = pos("USDJPY", SIDE_SELL, 10_000, "158.180")
        quote = q("USDJPY", "158.180", "158.190")
        self.assertEqual(accounting.unrealized_pnl(p, quote, PAIRS["USDJPY"], RATES), -4)

    def test_w6_eurusd_unrealized_two_stage(self):
        """W6：EURUSD 多 1 万 @1.11780，bid=1.12780 → +100 USD × 6.67 = +667 元。"""
        p = pos("EURUSD", SIDE_BUY, 10_000, "1.11780")
        quote = q("EURUSD", "1.12780", "1.12790")
        self.assertEqual(accounting.unrealized_pnl(p, quote, PAIRS["EURUSD"], RATES), 667)

    def test_missing_quote_returns_zero(self):
        p = pos("USDJPY", SIDE_BUY, 10_000, "158.190")
        self.assertEqual(accounting.unrealized_pnl(p, None, PAIRS["USDJPY"], RATES), 0)  # type: ignore[arg-type]

    def test_missing_rates_raises(self):
        p = pos("USDJPY", SIDE_BUY, 10_000, "158.190")
        with self.assertRaises(RateUnavailable):
            accounting.unrealized_pnl(p, q("USDJPY", "158.180", "158.190"), PAIRS["USDJPY"], None)

    def test_w1_equity(self):
        """W1：账户净值 = 100,000 − 4 = 99,996 元。"""
        p = pos("USDJPY", SIDE_BUY, 10_000, "158.190")
        quotes = {"USDJPY": q("USDJPY", "158.180", "158.190")}
        self.assertEqual(accounting.equity(account(), [p], quotes, PAIRS, RATES), 99_996)

    def test_maintenance_ratio(self):
        """W1：维持率 = 99,996 / 2,672 × 100 = 3,742.37%。"""
        self.assertEqual(
            accounting.maintenance_ratio(99_996, 2_672),
            Decimal("3742.37"),
        )

    def test_maintenance_ratio_none_without_positions(self):
        self.assertIsNone(accounting.maintenance_ratio(100_000, 0))


# ---------------------------------------------------------------------------
# W8：强制平仓边界（整数严格比较）
# ---------------------------------------------------------------------------
class TestLossCut(unittest.TestCase):
    def test_w8_boundaries(self):
        """W8：13,356(50.003%)不触发 / 13,354(49.996%)触发 / 13,355(恰好50%)不触发。"""
        margin = 26_710
        self.assertFalse(accounting.loss_cut_triggered(13_356, margin, Decimal("50")))
        self.assertTrue(accounting.loss_cut_triggered(13_354, margin, Decimal("50")))
        # 恰好 50.00%：13,355 × 100 = 1,335,500 = 26,710 × 50 → 严格小于才触发 → False
        self.assertFalse(accounting.loss_cut_triggered(13_355, margin, Decimal("50")))

    def test_ratio_float_and_int(self):
        self.assertTrue(accounting.loss_cut_triggered(100, 1_000, 50.0))
        self.assertFalse(accounting.loss_cut_triggered(1_000, 1_000, 50))

    def test_no_position_never_triggers(self):
        self.assertFalse(accounting.loss_cut_triggered(-999, 0, Decimal("50")))

    def test_invalid_ratio_raises(self):
        with self.assertRaises(ValueError) as ctx:
            accounting.loss_cut_triggered(100, 1_000, Decimal("0"))
        self.assertIn("INVALID_RATIO", str(ctx.exception))


# ---------------------------------------------------------------------------
# W3/W4：隔夜利息（日元单价 → 元）
# ---------------------------------------------------------------------------
class TestSwap(unittest.TestCase):
    def test_w3_tryjpy_long_daily(self):
        """TRYJPY 买 1 万：+33 日元 ÷ 23.6881559 = 1.39 → +1 元。"""
        p = pos("TRYJPY", SIDE_BUY, 10_000, "3.1565")
        self.assertEqual(accounting.swap_amount(p, PAIRS["TRYJPY"], 1, RATES), 1)

    def test_w4_wednesday_triple(self):
        """W4：TRYJPY 买 1 万 × 3 日分 = +99 日元 → +4 元；卖 1 万 3 日分 = −180 → −8 元。"""
        self.assertEqual(
            accounting.swap_amount(pos("TRYJPY", SIDE_BUY, 10_000, "3.1565"), PAIRS["TRYJPY"], 3, RATES), 4
        )
        self.assertEqual(
            accounting.swap_amount(pos("TRYJPY", SIDE_SELL, 10_000, "3.1555"), PAIRS["TRYJPY"], 3, RATES), -8
        )

    def test_w4_usdjpy_and_eurusd(self):
        self.assertEqual(
            accounting.swap_amount(pos("USDJPY", SIDE_BUY, 10_000, "158.190"), PAIRS["USDJPY"], 1, RATES), 7
        )
        self.assertEqual(
            accounting.swap_amount(pos("USDJPY", SIDE_SELL, 10_000, "158.180"), PAIRS["USDJPY"], 1, RATES), -9
        )
        self.assertEqual(
            accounting.swap_amount(pos("EURUSD", SIDE_BUY, 10_000, "1.11780"), PAIRS["EURUSD"], 1, RATES), -4
        )
        self.assertEqual(
            accounting.swap_amount(pos("EURUSD", SIDE_SELL, 10_000, "1.11770"), PAIRS["EURUSD"], 1, RATES), 2
        )

    def test_w4_1000_units_round_half_up(self):
        """W4：TRYJPY 1,000 货币 = 33 × 0.1 = 3.3 日元 → 0.139 元 → 0。"""
        self.assertEqual(
            accounting.swap_amount(pos("TRYJPY", SIDE_BUY, 1_000, "3.1565"), PAIRS["TRYJPY"], 1, RATES), 0
        )

    def test_zero_days(self):
        self.assertEqual(
            accounting.swap_amount(pos("USDJPY", SIDE_BUY, 10_000, "158.190"), PAIRS["USDJPY"], 0, RATES), 0
        )

    def test_negative_days_raises(self):
        with self.assertRaises(ValueError) as ctx:
            accounting.swap_amount(pos("USDJPY", SIDE_BUY, 10_000, "158.190"), PAIRS["USDJPY"], -1, RATES)
        self.assertIn("INVALID_DAYS", str(ctx.exception))

    def test_missing_rates_raises(self):
        with self.assertRaises(RateUnavailable):
            accounting.swap_amount(pos("USDJPY", SIDE_BUY, 10_000, "158.190"), PAIRS["USDJPY"], 1, None)


# ---------------------------------------------------------------------------
# 已实现損益 / 手续费
# ---------------------------------------------------------------------------
class TestClosePnl(unittest.TestCase):
    def test_w1_close_profit_1000_yen(self):
        """W1：平 @158.290 → +1,000 日元 ÷ 23.6881559 = 42.2 → +42 元。"""
        p = pos("USDJPY", SIDE_BUY, 10_000, "158.190")
        self.assertEqual(accounting.close_pnl(p, Decimal("158.290"), PAIRS["USDJPY"], RATES), 42)

    def test_t16_partial_close_loss(self):
        """T16：2 万 @158.005 平 1 万 @158.000 → −50 日元 → −2.11 → −2 元。"""
        p = pos("USDJPY", SIDE_BUY, 10_000, "158.005")
        self.assertEqual(accounting.close_pnl(p, Decimal("158.000"), PAIRS["USDJPY"], RATES), -2)

    def test_w6_eurusd_pnl_round_half_up(self):
        """W6：+100 USD × 6.67 = +667 元（整数，无四舍五入误差）。"""
        p = pos("EURUSD", SIDE_BUY, 10_000, "1.11780")
        self.assertEqual(accounting.close_pnl(p, Decimal("1.12780"), PAIRS["EURUSD"], RATES), 667)

    def test_eurusd_short_round_down(self):
        """空单 −0.0001 × 1万 = −6.67 元 → ROUND_HALF_UP → −7。"""
        p = pos("EURUSD", SIDE_SELL, 10_000, "1.11770")
        self.assertEqual(accounting.close_pnl(p, Decimal("1.11780"), PAIRS["EURUSD"], RATES), -7)

    def test_short_close_profit(self):
        p = pos("USDJPY", SIDE_SELL, 10_000, "158.180")
        self.assertEqual(accounting.close_pnl(p, Decimal("158.080"), PAIRS["USDJPY"], RATES), 42)

    def test_missing_rates_raises(self):
        p = pos("USDJPY", SIDE_BUY, 10_000, "158.190")
        with self.assertRaises(RateUnavailable):
            accounting.close_pnl(p, Decimal("158.290"), PAIRS["USDJPY"], None)

    def test_commission_ceil(self):
        self.assertEqual(accounting.commission(10_000, Decimal("0")), 0)
        self.assertEqual(accounting.commission(10_000, Decimal("1.5")), 2)
        self.assertEqual(accounting.commission(1_000, Decimal("1.5")), 1)

    def test_round_helpers(self):
        self.assertEqual(accounting.round_money(Decimal("15818.5")), 15_819)
        self.assertEqual(accounting.round_money(Decimal("-15818.5")), -15_819)
        self.assertEqual(accounting.ceil_money(Decimal("6327.4")), 6_328)


if __name__ == "__main__":
    unittest.main()
