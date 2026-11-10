"""risk.py 单测：2% 止损定量 / 保证金上限 / 日亏回撤 / 亏损禁加仓 / 对冲限制。

全部离线：构造轻量 cfg（SimpleNamespace，按 config.py 结构鸭子类型）+ 手工对象。
运行：项目根 `python -m unittest discover -s tests -t . -v`
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import risk  # noqa: E402
from fxconv import FxRates
from models import (  # noqa: E402
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    POS_CLOSE,
    POS_OPEN,
    SIDE_BUY,
    SIDE_SELL,
    AccountState,
    OrderRequest,
    PairSpec,
    Position,
    QuoteSnapshot,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
RATES = FxRates(usdjpy_mid=Decimal("158.185"), usdcny_mid=Decimal("6.67"))


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


PAIRS = {"USDJPY": make_usdjpy(), "EURUSD": make_eurusd()}


def make_cfg(*, margin_per_order_ratio: float = 0.15, forbid_add_to_loser: bool = True,
             allow_hedging: bool = False, risk_per_trade_ratio: float = 0.02,
             daily_trade_limit: int = 6):
    """构造最小 cfg（鸭子类型，按 config.py 结构）。"""
    return SimpleNamespace(
        account=SimpleNamespace(
            initial_balance=100_000,
            leverage=25,
            min_units=1_000,
            commission_per_10k=0.0,
            allow_hedging=allow_hedging,
            loss_cut_ratio=50.0,
            margin_call_ratio=100.0,
            liquidation_mode="all",
        ),
        market=SimpleNamespace(stale_quote_seconds=60),
        spread=SimpleNamespace(market_max_slip_pips=3.0, max_multiplier=5.0),
        orders=SimpleNamespace(
            max_pending=20,
            default_tif="GTC",
            allow_limit=True,
            allow_stop=True,
            allow_trailing=False,
            quote_stale_fill_seconds=5,
        ),
        risk=SimpleNamespace(
            risk_per_trade_ratio=risk_per_trade_ratio,
            margin_per_order_ratio=margin_per_order_ratio,
            max_positions=6,
            max_units_per_pair=200_000,
            daily_trade_limit=daily_trade_limit,
            daily_loss_limit_ratio=0.05,
            max_drawdown_ratio=0.20,
            forbid_add_to_loser=forbid_add_to_loser,
            loss_cut_cooldown_minutes=10,
            post_loss_cut_resume_ratio=100.0,
        ),
    )


def account(balance: int = 100_000, **kwargs) -> AccountState:
    base = dict(
        balance=balance,
        initial_balance=100_000,
        day_start_equity=100_000,
        peak_equity=100_000,
    )
    base.update(kwargs)
    return AccountState(**base)


def q(pair: str = "USDJPY", bid: str = "158.180", ask: str = "158.190") -> QuoteSnapshot:
    b, a = Decimal(bid), Decimal(ask)
    return QuoteSnapshot(pair=pair, bid=b, ask=a, mid=(b + a) / 2, quote_ts=NOW, recv_ts=NOW)


def pos(side: str = SIDE_BUY, units: int = 10_000, entry: str = "158.190", pair: str = "USDJPY") -> Position:
    return Position(
        position_id="P1",
        pair=pair,
        side=side,
        units=units,
        entry_price=Decimal(entry),
        opened_at=NOW,
    )


def req_open(
    *,
    pair: str = "USDJPY",
    side: str = SIDE_BUY,
    order_type: str = ORDER_MARKET,
    units: int = 10_000,
    limit_price: Decimal | None = None,
    trigger_price: Decimal | None = None,
) -> OrderRequest:
    return OrderRequest(
        pair=pair,
        side=side,
        position_type=POS_OPEN,
        order_type=order_type,
        units=units,
        limit_price=limit_price,
        trigger_price=trigger_price,
    )


# ---------------------------------------------------------------------------
# size_by_risk
# ---------------------------------------------------------------------------
class TestSizeByRisk(unittest.TestCase):
    def test_basic_2pct(self):
        """账户净值 10 万 × 2% = 2,000 元；止损 100 pip → 2,000 ÷ (100×0.00042215) ≈ 47,376 → 47,000 货币。"""
        spec = PAIRS["USDJPY"]
        units = risk.size_by_risk(account(), spec, Decimal("158.100"), Decimal("157.100"), RATES, make_cfg())
        self.assertEqual(units, 47_000)

    def test_rounds_down_to_1000(self):
        """止损 150 pip：2,000 / (150×0.00042215) = 31,584.7 → 31,000（向下取 1,000 整数倍）。"""
        spec = PAIRS["USDJPY"]
        units = risk.size_by_risk(account(), spec, Decimal("158.000"), Decimal("156.500"), RATES, make_cfg())
        self.assertEqual(units, 31_000)

    def test_eurusd_two_stage(self):
        """EURUSD：pip 元值 = 0.0001 × 6.67；止损 50 pip → 2,000 ÷ (50×0.000667) ≈ 59,970 → 59,000 货币。"""
        spec = PAIRS["EURUSD"]
        units = risk.size_by_risk(account(), spec, Decimal("1.11800"), Decimal("1.11300"), RATES, make_cfg())
        self.assertEqual(units, 59_000)

    def test_zero_stop_returns_zero(self):
        spec = PAIRS["USDJPY"]
        self.assertEqual(
            risk.size_by_risk(account(), spec, Decimal("158.100"), Decimal("158.100"), RATES, make_cfg()), 0
        )

    def test_missing_stop_returns_zero(self):
        self.assertEqual(risk.size_by_risk(account(), PAIRS["USDJPY"], Decimal("158.1"), None, RATES, make_cfg()), 0)

    def test_too_small_returns_zero(self):
        """止损 5,000 pip → 2,000/(5,000×0.00042215)=947 < 1,000 → 0。"""
        spec = PAIRS["USDJPY"]
        units = risk.size_by_risk(account(), spec, Decimal("158.000"), Decimal("108.000"), RATES, make_cfg())
        self.assertEqual(units, 0)

    def test_capped_by_spec_max_units(self):
        """高权益 + 1 pip 止损会算出巨大数量 → 被 spec.max_units 截断到 10 万。"""
        spec = PAIRS["USDJPY"]
        units = risk.size_by_risk(
            account(balance=100_000_000), spec, Decimal("158.100"), Decimal("158.090"), RATES, make_cfg()
        )
        self.assertEqual(units, 100_000)

    def test_huge_balance_rounding(self):
        """EURUSD 止损 30 pip、账户净值 10 万 ×2% → 99,000 货币（舍去尾数）。"""
        spec = PAIRS["EURUSD"]
        units = risk.size_by_risk(account(), spec, Decimal("1.11800"), Decimal("1.11500"), RATES, make_cfg())
        # 2,000 / (30 × 0.000667) = 99,950 → 99,000
        self.assertEqual(units, 99_000)


# ---------------------------------------------------------------------------
# 日亏 / 回撤
# ---------------------------------------------------------------------------
class TestDailyAndDrawdown(unittest.TestCase):
    def test_daily_loss_strict_boundary(self):
        """日亏上限 5% = 5,000：亏 5,001 → 命中；恰好 5,000 → 不命中。"""
        cfg = make_cfg()
        self.assertTrue(risk.daily_loss_hit(account(), 94_999, cfg))
        self.assertFalse(risk.daily_loss_hit(account(), 95_000, cfg))

    def test_daily_loss_profit_never_hits(self):
        self.assertFalse(risk.daily_loss_hit(account(), 110_000, make_cfg()))

    def test_drawdown_strict_boundary(self):
        """回撤上限 20% = 20,000：亏 20,001 命中；恰好 20,000 不命中。"""
        cfg = make_cfg()
        self.assertTrue(risk.drawdown_hit(account(), 79_999, cfg))
        self.assertFalse(risk.drawdown_hit(account(), 80_000, cfg))

    def test_zero_peak_drawdown(self):
        acc = account(peak_equity=0)
        self.assertFalse(risk.drawdown_hit(acc, 0, make_cfg()))


# ---------------------------------------------------------------------------
# 亏损加仓
# ---------------------------------------------------------------------------
class TestAllowAddToLoser(unittest.TestCase):
    def test_same_side_loser_forbidden(self):
        """同货币对同向 + 浮亏（多单 entry 158.190，bid 157.900）→ NANPIN_FORBIDDEN。"""
        position = pos(SIDE_BUY, 10_000, "158.190")
        quote = q("USDJPY", "157.900", "157.910")
        ok, reason = risk.allow_add_to_loser(
            position, req_open(side=SIDE_BUY), PAIRS["USDJPY"], quote, RATES, make_cfg()
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "NANPIN_FORBIDDEN")

    def test_winner_add_allowed(self):
        position = pos(SIDE_BUY, 10_000, "158.190")
        quote = q("USDJPY", "158.300", "158.310")
        ok, _ = risk.allow_add_to_loser(
            position, req_open(side=SIDE_BUY), PAIRS["USDJPY"], quote, RATES, make_cfg()
        )
        self.assertTrue(ok)

    def test_opposite_side_allowed(self):
        """双向持仓（反向开仓）不受逆势加仓规则限制（由 hedge 规则单独判）。"""
        position = pos(SIDE_BUY, 10_000, "158.190")
        quote = q("USDJPY", "157.900", "157.910")
        ok, _ = risk.allow_add_to_loser(
            position, req_open(side=SIDE_SELL), PAIRS["USDJPY"], quote, RATES, make_cfg()
        )
        self.assertTrue(ok)

    def test_config_disabled(self):
        position = pos(SIDE_BUY, 10_000, "158.190")
        quote = q("USDJPY", "157.900", "157.910")
        ok, _ = risk.allow_add_to_loser(
            position,
            req_open(side=SIDE_BUY),
            PAIRS["USDJPY"],
            quote,
            RATES,
            make_cfg(forbid_add_to_loser=False),
        )
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# check_open
# ---------------------------------------------------------------------------
class TestCheckOpen(unittest.TestCase):
    def test_rate_unavailable_rejected(self):
        """缺汇率：开仓校验直接拒（RATE_UNAVAILABLE），不估算保证金。"""
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], account(), [], {"USDJPY": q()}, None, make_cfg(), NOW, False, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "RATE_UNAVAILABLE")

    def test_pass_market_open(self):
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], account(), [], {"USDJPY": q()}, RATES, make_cfg(), NOW, False, False
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_units_not_multiple_of_1000(self):
        ok, reason = risk.check_open(
            req_open(units=5_500), PAIRS["USDJPY"], account(), [], {"USDJPY": q()}, RATES, make_cfg(), NOW, False, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "INVALID_UNITS")

    def test_close_request_invalid_here(self):
        request = req_open()
        request.position_type = POS_CLOSE
        ok, reason = risk.check_open(
            request, PAIRS["USDJPY"], account(), [], {"USDJPY": q()}, RATES, make_cfg(), NOW, False, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "INVALID_POSITION_TYPE")

    def test_account_halted(self):
        acc = account(halted=True)
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], acc, [], {"USDJPY": q()}, RATES, make_cfg(), NOW, False, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "ACCOUNT_HALTED")

    def test_loss_cut_cooldown(self):
        acc = account(loss_cut_cooldown_until=NOW + timedelta(minutes=5))
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], acc, [], {"USDJPY": q()}, RATES, make_cfg(), NOW, False, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "LOSS_CUT_COOLDOWN")

    def test_daily_trade_limit(self):
        acc = account(trades_today=6)
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], acc, [], {"USDJPY": q()}, RATES, make_cfg(), NOW, False, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "DAILY_TRADE_LIMIT")

    def test_daily_loss_flag(self):
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], account(), [], {"USDJPY": q()}, RATES, make_cfg(), NOW, True, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "DAILY_LOSS_LIMIT")

    def test_drawdown_flag(self):
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], account(), [], {"USDJPY": q()}, RATES, make_cfg(), NOW, False, True
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "MAX_DRAWDOWN")

    def test_hedge_not_allowed(self):
        """P0 allow_hedging=false：持多时反向开空 → HEDGE_NOT_ALLOWED。"""
        positions = [pos(SIDE_BUY)]
        ok, reason = risk.check_open(
            req_open(side=SIDE_SELL),
            PAIRS["USDJPY"],
            account(),
            positions,
            {"USDJPY": q()},
            RATES,
            make_cfg(),
            NOW,
            False,
            False,
            pairs=PAIRS,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "HEDGE_NOT_ALLOWED")

    def test_nanpin_forbidden_via_check_open(self):
        positions = [pos(SIDE_BUY, 10_000, "158.190")]
        quote = q("USDJPY", "157.900", "157.910")
        ok, reason = risk.check_open(
            req_open(side=SIDE_BUY),
            PAIRS["USDJPY"],
            account(),
            positions,
            {"USDJPY": quote},
            RATES,
            make_cfg(),
            NOW,
            False,
            False,
            pairs=PAIRS,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "NANPIN_FORBIDDEN")

    def test_margin_per_order_exceeded(self):
        """默认 15%：10 万单位 @158.185 → 保证金 26,712 > 100,000×15% = 15,000 → 拒。"""
        ok, reason = risk.check_open(
            req_open(units=100_000),
            PAIRS["USDJPY"],
            account(),
            [],
            {"USDJPY": q()},
            RATES,
            make_cfg(),
            NOW,
            False,
            False,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "MARGIN_PER_ORDER_EXCEEDED")

    def test_insufficient_margin(self):
        """已持 1 万（保证金 2,672），余额 3,000：新 1 万后需 5,344 > 账户净值 → 拒。"""
        positions = [pos(SIDE_BUY, 10_000, "158.190")]
        ok, reason = risk.check_open(
            req_open(),
            PAIRS["USDJPY"],
            account(balance=3_000),
            positions,
            {"USDJPY": q()},
            RATES,
            make_cfg(margin_per_order_ratio=1.0, forbid_add_to_loser=False),
            NOW,
            False,
            False,
            pairs=PAIRS,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "INSUFFICIENT_MARGIN")

    def test_max_positions(self):
        positions = [
            pos(SIDE_BUY, 1_000, "158.190") for _ in range(6)
        ]
        for i, p in enumerate(positions):
            p.position_id = f"P{i}"
        ok, reason = risk.check_open(
            req_open(),
            PAIRS["USDJPY"],
            account(),
            positions,
            {"USDJPY": q()},
            RATES,
            make_cfg(margin_per_order_ratio=1.0),
            NOW,
            False,
            False,
            pairs=PAIRS,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "MAX_POSITIONS")

    def test_pair_units_cap(self):
        """已持 9.5 万 + 新 1 万 > 单品种上限 10 万 → MAX_UNITS_EXCEEDED。"""
        positions = [pos(SIDE_BUY, 95_000, "158.190")]
        ok, reason = risk.check_open(
            req_open(),
            PAIRS["USDJPY"],
            account(),
            positions,
            {"USDJPY": q()},
            RATES,
            make_cfg(margin_per_order_ratio=1.0),
            NOW,
            False,
            False,
            pairs=PAIRS,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "MAX_UNITS_EXCEEDED")

    def test_quote_missing_for_market(self):
        ok, reason = risk.check_open(
            req_open(), PAIRS["USDJPY"], account(), [], {}, RATES, make_cfg(), NOW, False, False
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "QUOTE_MISSING")

    def test_limit_order_price_estimate(self):
        """限价用 limit_price 估算保证金（1万 @158.000 ÷ 23.688 ÷ 25 = 2,668 元）→ 通过。"""
        ok, reason = risk.check_open(
            req_open(order_type=ORDER_LIMIT, limit_price=Decimal("158.000")),
            PAIRS["USDJPY"],
            account(),
            [],
            {"USDJPY": q()},
            RATES,
            make_cfg(),
            NOW,
            False,
            False,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
