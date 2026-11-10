"""orders.py 单测：订单引擎（T01–T20/T26/T30–T33/T37/T40/T42/T44 核心）。

全部离线：手工构造 PairSpec / QuoteSnapshot / AccountState / Position，
滑点默认注入 Fixed(0) 以保证价格可手算；专门滑点用例再注入非零值。
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

import orders  # noqa: E402
from fxconv import FxRates
from models import (  # noqa: E402
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    POS_CLOSE,
    POS_OPEN,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_CANCELLED,
    STATUS_FILLED,
    STATUS_PENDING,
    STATUS_REJECTED,
    AccountState,
    Order,
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


PAIRS = {"USDJPY": make_usdjpy()}


def make_cfg(*, max_pending: int = 20, commission: float = 0.0) -> SimpleNamespace:
    """最小 cfg（按 config.py 结构鸭子类型）。"""
    return SimpleNamespace(
        account=SimpleNamespace(
            initial_balance=1_000_000,
            leverage=25,
            min_units=1_000,
            commission_per_10k=commission,
            allow_hedging=False,
            loss_cut_ratio=50.0,
            margin_call_ratio=100.0,
            liquidation_mode="all",
        ),
        market=SimpleNamespace(stale_quote_seconds=60),
        spread=SimpleNamespace(market_max_slip_pips=3.0, max_multiplier=5.0),
        orders=SimpleNamespace(
            max_pending=max_pending,
            default_tif="GTC",
            allow_limit=True,
            allow_stop=True,
            allow_trailing=False,
            quote_stale_fill_seconds=5,
        ),
        risk=SimpleNamespace(
            risk_per_trade_ratio=0.02,
            margin_per_order_ratio=0.15,
            max_positions=6,
            max_units_per_pair=200_000,
            daily_trade_limit=6,
            daily_loss_limit_ratio=0.05,
            max_drawdown_ratio=0.20,
            forbid_add_to_loser=True,
            loss_cut_cooldown_minutes=10,
            post_loss_cut_resume_ratio=100.0,
        ),
    )


def zero_slip(order_type: str, spec: PairSpec) -> Decimal:
    return Decimal("0")


def make_engine(slippage_fn=zero_slip, cfg=None) -> orders.OrderEngine:
    return orders.OrderEngine(PAIRS, cfg or make_cfg(), slippage_fn=slippage_fn)


def account(balance: int = 1_000_000, **kwargs) -> AccountState:
    base = dict(
        balance=balance,
        initial_balance=1_000_000,
        day_start_equity=1_000_000,
        peak_equity=1_000_000,
    )
    base.update(kwargs)
    return AccountState(**base)


def make_quote(
    pair: str = "USDJPY",
    bid: str = "158.000",
    ask: str = "158.005",
    *,
    now: datetime = NOW,
    seq: int = 1,
    age_seconds: float = 1.0,
) -> QuoteSnapshot:
    """构造报价；quote_ts 早于 now `age_seconds`（用于新鲜度测试）。"""
    b, a = Decimal(bid), Decimal(ask)
    recv = now - timedelta(seconds=age_seconds)
    return QuoteSnapshot(
        pair=pair,
        bid=b,
        ask=a,
        mid=(b + a) / Decimal(2),
        quote_ts=recv,
        recv_ts=recv,
        seq=seq,
    )


def make_quote_at(now: datetime, **kwargs) -> QuoteSnapshot:
    """按 tick 时刻构造新鲜报价（quote_ts = now − 1s，单调递增）。"""
    return make_quote(now=now, **kwargs)


def pos_2l() -> Position:
    """持仓夹具 P2L：2万单位 USDJPY 多头 @158.005（T15/T16/T33 用）。"""
    return Position(
        position_id="P2L",
        pair="USDJPY",
        side=SIDE_BUY,
        units=20_000,
        entry_price=Decimal("158.005"),
        opened_at=NOW - timedelta(hours=1),
    )


def open_req(
    side: str = SIDE_BUY,
    order_type: str = ORDER_MARKET,
    units: int = 10_000,
    *,
    limit_price: Decimal | None = None,
    trigger_price: Decimal | None = None,
    client_order_id: str = "",
    oco_group_id: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        pair="USDJPY",
        side=side,
        position_type=POS_OPEN,
        order_type=order_type,
        units=units,
        client_order_id=client_order_id,
        limit_price=limit_price,
        trigger_price=trigger_price,
        oco_group_id=oco_group_id,
    )


def close_req(
    side: str,
    order_type: str = ORDER_MARKET,
    units: int = 10_000,
    *,
    limit_price: Decimal | None = None,
    trigger_price: Decimal | None = None,
    position_id: str | None = None,
    oco_group_id: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        pair="USDJPY",
        side=side,
        position_type=POS_CLOSE,
        order_type=order_type,
        units=units,
        limit_price=limit_price,
        trigger_price=trigger_price,
        position_id=position_id,
        oco_group_id=oco_group_id,
    )


# ---------------------------------------------------------------------------
# T01–T04：市价 / 滑点
# ---------------------------------------------------------------------------
class TestMarket(unittest.TestCase):
    def test_t01_market_buy_fills_at_ask(self):
        """T01：market buy 1 万 → 成交价 = ask = 158.005；新規 Fill 生成 POS-<order_id>。"""
        engine = make_engine()
        now = NOW
        quote = make_quote_at(now)
        order = engine.submit(open_req(), {"USDJPY": quote}, now, account(), [], RATES)
        self.assertEqual(order.status, STATUS_PENDING)
        result = engine.process_tick({"USDJPY": make_quote_at(now + timedelta(seconds=5))}, now, account(), [], RATES)
        # 改用一个单调递增的 tick 时间避免重复快照
        engine2 = make_engine()
        quote2 = make_quote_at(now)
        o2 = engine2.submit(open_req(), {"USDJPY": quote2}, now, account(), [], RATES)
        now2 = now + timedelta(seconds=5)
        r2 = engine2.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(len(r2.fills), 1)
        fill = r2.fills[0]
        self.assertEqual(fill.price, Decimal("158.005"))
        self.assertEqual(fill.side, SIDE_BUY)
        self.assertEqual(fill.units, 10_000)
        self.assertEqual(fill.position_type, POS_OPEN)
        self.assertEqual(fill.position_id, f"POS-{o2.order_id}")
        self.assertEqual(fill.fee, 0)
        self.assertEqual(o2.status, STATUS_FILLED)
        self.assertEqual(o2.filled_price, Decimal("158.005"))

    def test_t02_market_sell_fills_at_bid(self):
        """T02：market sell 1 万 → 成交价 = bid = 158.000。"""
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(open_req(side=SIDE_SELL), {"USDJPY": make_quote_at(now)}, now, account(), [], RATES)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="158.000", ask="158.005")}, now2, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("158.000"))

    def test_t03_slippage_adverse_only(self):
        """T03：注入固定 0.5 pip → market buy 成交 158.005+0.005 = 158.010；slip=0.5。"""
        engine = make_engine(lambda order_type, spec: Decimal("0.5"))
        now, now2 = NOW, NOW + timedelta(seconds=5)
        order = engine.submit(open_req(), {"USDJPY": make_quote_at(now)}, now, account(), [], RATES)
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(result.fills[0].price, Decimal("158.010"))
        self.assertEqual(result.fills[0].slippage_pips, Decimal("0.5"))
        self.assertEqual(order.slippage_pips, Decimal("0.5"))

    def test_t04_slippage_too_large_rejected(self):
        """T04：注入 4.0 pip > max 3.0 → rejected(SLIPPAGE_TOO_LARGE)，无成交。"""
        engine = make_engine(lambda order_type, spec: Decimal("4.0"))
        now, now2 = NOW, NOW + timedelta(seconds=5)
        order = engine.submit(open_req(), {"USDJPY": make_quote_at(now)}, now, account(), [], RATES)
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(result.fills, [])
        self.assertEqual(order.status, STATUS_REJECTED)
        self.assertEqual(order.reject_reason, "SLIPPAGE_TOO_LARGE")
        self.assertIn((order.order_id, "SLIPPAGE_TOO_LARGE"), result.rejected)


# ---------------------------------------------------------------------------
# T05–T08：限价
# ---------------------------------------------------------------------------
class TestLimit(unittest.TestCase):
    def test_t05_limit_immediate_better_price(self):
        """T05：limit buy 158.010 > ask 158.005 → 即时按更优 ask 成交（≤ 限价）。"""
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.010")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(result.fills[0].price, Decimal("158.005"))
        self.assertLessEqual(result.fills[0].price, Decimal("158.010"))
        self.assertEqual(order.status, STATUS_FILLED)

    def test_t06_limit_waiting_then_touch(self):
        """T06：limit buy 158.000（ask 158.005 未触）→ pending；ask 触达 158.000 → 成交。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.000")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.status, STATUS_PENDING)
        now2 = now + timedelta(seconds=5)
        empty = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(empty.fills, [])
        now3 = now + timedelta(seconds=10)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now3, bid="157.995", ask="158.000")}, now3, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("158.000"))

    def test_t07_limit_sell_not_worse_than_limit(self):
        """T07：limit sell 158.010，bid=158.010（等号触发）→ 成交 ≥ 限价。"""
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(
            open_req(side=SIDE_SELL, order_type=ORDER_LIMIT, limit_price=Decimal("158.010")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="158.010", ask="158.015")}, now2, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("158.010"))
        self.assertGreaterEqual(result.fills[0].price, Decimal("158.010"))

    def test_t08_limit_gap_better_price(self):
        """T08：limit buy 158.000 跳空到 ask 157.950 → 按更优 157.950 成交。"""
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.000")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="157.945", ask="157.950")}, now2, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("157.950"))

    def test_limit_sell_gap_better_price(self):
        """限价卖跳空到更高 bid：limit 158.000，bid 直接 158.500 → 成交 158.500。"""
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(
            open_req(side=SIDE_SELL, order_type=ORDER_LIMIT, limit_price=Decimal("158.000")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="158.500", ask="158.505")}, now2, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("158.500"))

    def test_limit_never_negative_slippage(self):
        """限价不吞滑点：注入 5 pip 滑点也不影响限价成交价。"""
        engine = make_engine(lambda order_type, spec: Decimal("5.0"))
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.010")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(result.fills[0].price, Decimal("158.005"))
        self.assertEqual(result.fills[0].slippage_pips, Decimal("0"))


# ---------------------------------------------------------------------------
# T09–T11：止损
# ---------------------------------------------------------------------------
class TestStop(unittest.TestCase):
    def test_t09_stop_buy_trigger_uses_ask(self):
        """T09：stop buy trigger=158.010，注入 0.5 pip；ask 158.005 不触发，158.010 触发 → 158.015。"""
        engine = make_engine(lambda order_type, spec: Decimal("0.5"))
        now = NOW
        order = engine.submit(
            open_req(order_type=ORDER_STOP, trigger_price=Decimal("158.010")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.status, STATUS_PENDING)
        now2 = now + timedelta(seconds=5)
        first = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(first.fills, [])
        now3 = now + timedelta(seconds=10)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now3, bid="158.005", ask="158.010")}, now3, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("158.015"))

    def test_t10_stop_sell_judge_bid_not_mid(self):
        """T10：stop sell trigger=157.990；mid 未穿而 bid 已穿 → 以 bid 判定成交（不用 mid）。"""
        engine = make_engine()
        now = NOW
        engine.submit(
            open_req(side=SIDE_SELL, order_type=ORDER_STOP, trigger_price=Decimal("157.990")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        now2 = now + timedelta(seconds=5)
        # bid=157.995 > trigger，mid=157.9975 也 > trigger → 不触发
        first = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="157.995", ask="158.000")}, now2, account(), [], RATES
        )
        self.assertEqual(first.fills, [])
        now3 = now + timedelta(seconds=10)
        # bid=157.983 <= trigger → 触发；mid=(157.983+158.000)/2=157.9915 > trigger（证明用 bid 而非 mid）
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now3, bid="157.983", ask="158.000")}, now3, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("157.983"))

    def test_t11_stop_sell_gap_records_gap_pips(self):
        """T11：stop sell trigger=157.900 跳空到 bid 157.700 → 成交 157.700，gap_pips=20。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(side=SIDE_SELL, order_type=ORDER_STOP, trigger_price=Decimal("157.900")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        now2 = now + timedelta(seconds=5)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="157.700", ask="157.705")}, now2, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("157.700"))
        self.assertEqual(order.gap_pips, Decimal("20.00"))


# ---------------------------------------------------------------------------
# T15–T17：平仓 / 部分平仓 / 超量
# ---------------------------------------------------------------------------
class TestClose(unittest.TestCase):
    def test_t15_close_limit_realizes_pnl(self):
        """T15：P2L 2 万 @158.005，close limit sell 158.500，bid 158.510 → +10,100 日元 ÷ 23.6881559 ≈ +426 元。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            close_req(SIDE_SELL, ORDER_LIMIT, 20_000, limit_price=Decimal("158.500")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        self.assertEqual(order.status, STATUS_PENDING)
        now2 = now + timedelta(seconds=5)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="158.510", ask="158.515")}, now2, account(), [pos_2l()], RATES
        )
        fill = result.fills[0]
        self.assertEqual(fill.price, Decimal("158.510"))
        self.assertEqual(fill.realized_pnl, 426)
        self.assertEqual(fill.position_id, "P2L")

    def test_t16_partial_close(self):
        """T16：P2L 2 万，close market sell 1 万 @158.000 → 部分平仓 −50 日元 ≈ −2 元。"""
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        order = engine.submit(
            close_req(SIDE_SELL, ORDER_MARKET, 10_000),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [pos_2l()], RATES)
        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        self.assertEqual(fill.units, 10_000)
        self.assertEqual(fill.price, Decimal("158.000"))
        self.assertEqual(fill.realized_pnl, -2)
        self.assertEqual(order.status, STATUS_FILLED)

    def test_t17_close_units_exceed_rejected(self):
        """T17：P2L 2 万，close market sell 3 万 → rejected(CLOSE_UNITS_EXCEED)。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            close_req(SIDE_SELL, ORDER_MARKET, 30_000),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        self.assertEqual(order.status, STATUS_REJECTED)
        self.assertEqual(order.reject_reason, "CLOSE_UNITS_EXCEED")

    def test_close_position_not_found(self):
        engine = make_engine()
        now = NOW
        order = engine.submit(
            close_req(SIDE_SELL, ORDER_MARKET, 10_000),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.status, STATUS_REJECTED)
        self.assertEqual(order.reject_reason, "POSITION_NOT_FOUND")

    def test_close_uses_explicit_position_id(self):
        """双向持仓时可显式指定平哪条（第二个持仓）。"""
        other = Position(
            position_id="P2S",
            pair="USDJPY",
            side=SIDE_SELL,
            units=10_000,
            entry_price=Decimal("158.180"),
            opened_at=NOW - timedelta(hours=2),
        )
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(
            close_req(SIDE_BUY, ORDER_MARKET, 10_000, position_id="P2S"),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [pos_2l(), other],
            RATES,
        )
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2)}, now2, account(), [pos_2l(), other], RATES
        )
        self.assertEqual(result.fills[0].position_id, "P2S")
        # 平空 = 买回 ask=158.005：dir=-1 → (158.180−158.005)×1万 = +1,750
        self.assertEqual(result.fills[0].realized_pnl, 74)


# ---------------------------------------------------------------------------
# T31/T30/T19/T20：校验 / 改单
# ---------------------------------------------------------------------------
class TestValidation(unittest.TestCase):
    def test_t31_invalid_units(self):
        engine = make_engine()
        now = NOW
        order = engine.submit(open_req(units=500), {"USDJPY": make_quote_at(now)}, now, account(), [], RATES)
        self.assertEqual(order.status, STATUS_REJECTED)
        self.assertEqual(order.reject_reason, "INVALID_UNITS")

    def test_rate_unavailable_rejects_open(self):
        """缺 USDCNY/USDJPY 汇率：开仓单直接拒绝，原因码 RATE_UNAVAILABLE（不用假汇率）。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(open_req(), {"USDJPY": make_quote_at(now)}, now, account(), [], None)
        self.assertEqual(order.status, STATUS_REJECTED)
        self.assertEqual(order.reject_reason, "RATE_UNAVAILABLE")

    def test_t31_invalid_price_tick(self):
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.0005")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.reject_reason, "INVALID_PRICE_TICK")

    def test_t30_price_out_of_range(self):
        """T30：limit buy 100.000（mid≈158，偏离 >20%）→ PRICE_OUT_OF_RANGE。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("100.000")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.reject_reason, "PRICE_OUT_OF_RANGE")

    def test_stale_quote_rejected_at_submit(self):
        """新单报价过期（>60s）→ QUOTE_STALE。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(),
            {"USDJPY": make_quote_at(now, age_seconds=70)},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.reject_reason, "QUOTE_STALE")

    def test_missing_quote_rejected(self):
        engine = make_engine()
        now = NOW
        order = engine.submit(open_req(), {}, now, account(), [], RATES)
        self.assertEqual(order.reject_reason, "QUOTE_MISSING")

    def test_spread_too_wide_new_market_rejected(self):
        """点差 3.5 pip > 上限 2.0 → 新规市价拒单 SPREAD_TOO_WIDE。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(),
            {"USDJPY": make_quote_at(now, bid="158.000", ask="158.035")},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.reject_reason, "SPREAD_TOO_WIDE")

    def test_max_pending_orders(self):
        engine = make_engine(cfg=make_cfg(max_pending=1))
        now = NOW
        quotes = {"USDJPY": make_quote_at(now)}
        first = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("157.000")), quotes, now, account(), [], RATES
        )
        self.assertEqual(first.status, STATUS_PENDING)
        second = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("157.100")), quotes, now, account(), [], RATES
        )
        self.assertEqual(second.reject_reason, "MAX_OPEN_ORDERS")

    def test_t19_modify_filled_not_modifiable(self):
        engine = make_engine()
        now = NOW
        order = engine.submit(open_req(), {"USDJPY": make_quote_at(now)}, now, account(), [], RATES)
        now2 = now + timedelta(seconds=5)
        engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        ok, reason = engine.modify(order.order_id, limit_price=Decimal("158.500"), now=now2)
        self.assertFalse(ok)
        self.assertEqual(reason, "NOT_MODIFIABLE")

    def test_t18_modify_pending_then_fill_new_price(self):
        """T18：pending limit 158.000 → 改 157.900 → 下一口 ask 157.900 成交；version+1。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.000")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        self.assertEqual(order.version, 1)
        ok, reason = engine.modify(order.order_id, limit_price=Decimal("157.900"), now=now)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(order.version, 2)
        now2 = now + timedelta(seconds=5)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="157.895", ask="157.900")}, now2, account(), [], RATES
        )
        self.assertEqual(result.fills[0].price, Decimal("157.900"))

    def test_modify_immutable_field(self):
        engine = make_engine()
        now = NOW
        order = engine.submit(open_req(), {"USDJPY": make_quote_at(now)}, now, account(), [], RATES)
        ok, reason = engine.modify(order.order_id, limit_price=Decimal("158.000"), now=now)
        self.assertFalse(ok)
        self.assertEqual(reason, "IMMUTABLE_FIELD")


# ---------------------------------------------------------------------------
# 撤单 / 幂等 / 恢复 / 挂起 / 乱序
# ---------------------------------------------------------------------------
class TestCancelIdempotencyRestore(unittest.TestCase):
    def test_cancel_and_idempotent_cancel(self):
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("157.000")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        ok, reason = engine.cancel(order.order_id, now)
        self.assertTrue(ok)
        self.assertEqual(order.status, STATUS_CANCELLED)
        ok2, reason2 = engine.cancel(order.order_id, now)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "ALREADY_CANCELLED")
        ok3, reason3 = engine.cancel("ORD-999999", now)
        self.assertFalse(ok3)
        self.assertEqual(reason3, "ORDER_NOT_FOUND")

    def test_t40_idempotent_submit(self):
        """T40：同一 client_order_id 连续 submit 两次 → 同一 order_id、只 1 条挂单。"""
        engine = make_engine()
        now = NOW
        req = open_req(
            order_type=ORDER_LIMIT, limit_price=Decimal("157.000"), client_order_id="acct:msg1:1"
        )
        quotes = {"USDJPY": make_quote_at(now)}
        first = engine.submit(req, quotes, now, account(), [], RATES)
        second = engine.submit(req, quotes, now, account(), [], RATES)
        self.assertEqual(first.order_id, second.order_id)
        self.assertEqual(len(engine.pending), 1)
        self.assertEqual(len(engine.all_orders()), 1)

    def test_t40_after_fill_resubmit_returns_filled(self):
        engine = make_engine()
        now = NOW
        req = open_req(client_order_id="acct:msg1:2")
        quotes = {"USDJPY": make_quote_at(now)}
        first = engine.submit(req, quotes, now, account(), [], RATES)
        now2 = now + timedelta(seconds=5)
        engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        again = engine.submit(req, quotes, now2, account(), [], RATES)
        self.assertEqual(again.order_id, first.order_id)
        self.assertEqual(again.status, STATUS_FILLED)

    def test_t44_restore_pending(self):
        """T44：restore 两条 pending 后参与撮合；新单 ID 不冲突。"""
        engine = make_engine()
        limit_order = Order(
            order_id="ORD-000001",
            request=OrderRequest(
                pair="USDJPY",
                side=SIDE_BUY,
                position_type=POS_OPEN,
                order_type=ORDER_LIMIT,
                units=10_000,
                limit_price=Decimal("157.000"),
                client_order_id="old:1",
            ),
            status=STATUS_PENDING,
            created_at=NOW - timedelta(hours=1),
        )
        stop_order = Order(
            order_id="ORD-000002",
            request=OrderRequest(
                pair="USDJPY",
                side=SIDE_BUY,
                position_type=POS_OPEN,
                order_type=ORDER_STOP,
                units=10_000,
                trigger_price=Decimal("159.000"),
            ),
            status=STATUS_PENDING,
            created_at=NOW - timedelta(hours=1),
        )
        engine.restore([limit_order, stop_order])
        self.assertEqual(len(engine.pending), 2)
        self.assertIs(engine.get_order("ORD-000001"), limit_order)
        # 新单 ID 应跳过已用序号
        new_order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("156.000")),
            {"USDJPY": make_quote_at(NOW)},
            NOW,
            account(),
            [],
            RATES,
        )
        self.assertNotIn(new_order.order_id, ("ORD-000001", "ORD-000002"))
        # 第一口新鲜报价触发哨兵：restore 的 limit 157.000 不成交；stop 159.000 也不成交
        now2 = NOW + timedelta(seconds=5)
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(result.fills, [])
        now3 = NOW + timedelta(seconds=10)
        result2 = engine.process_tick(
            {"USDJPY": make_quote_at(now3, bid="156.995", ask="157.000")}, now3, account(), [], RATES
        )
        self.assertEqual(len(result2.fills), 1)
        self.assertEqual(result2.fills[0].order_id, "ORD-000001")

    def test_stale_quote_suspends_then_resumes(self):
        """行情过期（>5s）→ 挂起 QUOTE_STALE；恢复新鲜报价 → resumed 并成交。"""
        engine = make_engine()
        now = NOW
        order = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.005")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [],
            RATES,
        )
        stale_now = now + timedelta(seconds=10)
        stale_quote = make_quote(now=stale_now, age_seconds=10)  # quote_ts 更早 → 新鲜度不足
        result = engine.process_tick({"USDJPY": stale_quote}, stale_now, account(), [], RATES)
        self.assertIn((order.order_id, "QUOTE_STALE"), result.suspended)
        self.assertEqual(order.suspend_reason, "QUOTE_STALE")
        self.assertEqual(order.status, STATUS_PENDING)
        fresh_now = stale_now + timedelta(seconds=5)
        result2 = engine.process_tick(
            {"USDJPY": make_quote(now=fresh_now, age_seconds=1)}, fresh_now, account(), [], RATES
        )
        self.assertEqual(len(result2.fills), 1)
        self.assertEqual(order.suspend_reason, "")

    def test_t42_duplicate_quote_ignored(self):
        """T42：同一 quote_ts 的快照重复投递 → 丢弃，不成交，挂单保留。"""
        engine = make_engine()
        now = NOW
        q1 = make_quote_at(now)
        engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.000")),
            {"USDJPY": q1},
            now,
            account(),
            [],
            RATES,
        )
        now2 = now + timedelta(seconds=5)
        q2 = make_quote_at(now2, bid="157.995", ask="158.000")
        first = engine.process_tick({"USDJPY": q2}, now2, account(), [], RATES)
        self.assertEqual(len(first.fills), 1)
        # 新挂单，然后用“重复快照”喂：不应成交
        engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("158.000"), client_order_id="dup1"),
            {"USDJPY": make_quote_at(now2 + timedelta(seconds=5))},
            now2 + timedelta(seconds=5),
            account(),
            [],
            RATES,
        )
        replay = engine.process_tick({"USDJPY": q2}, now2 + timedelta(seconds=10), account(), [], RATES)
        self.assertEqual(replay.fills, [])
        self.assertEqual(len(engine.pending), 1)


# ---------------------------------------------------------------------------
# T26 / T33 / T37 / S3 强平判定
# ---------------------------------------------------------------------------
class TestWideOcoHedgeLossCut(unittest.TestCase):
    def test_t26_close_stop_allowed_with_wide_spread(self):
        """点差 3.5 pip 超限：平仓止损仍成交（风险单必须能止损），成交价用 bid。"""
        engine = make_engine()
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(
            close_req(SIDE_SELL, ORDER_STOP, 2_000, trigger_price=Decimal("158.010")),
            {"USDJPY": make_quote_at(now)},
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="158.000", ask="158.035")}, now2, account(), [pos_2l()], RATES
        )
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].price, Decimal("158.000"))

    def test_t33_oco_cancels_other_leg(self):
        """T33：OCO{SL stop sell 157.900, TP limit sell 158.100}；bid 157.850 → SL 成交、TP OCO_CANCELLED。"""
        engine = make_engine()
        now = NOW
        quotes = {"USDJPY": make_quote_at(now)}
        sl = engine.submit(
            close_req(SIDE_SELL, ORDER_STOP, 20_000, trigger_price=Decimal("157.900"), oco_group_id="G1"),
            quotes,
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        tp = engine.submit(
            close_req(SIDE_SELL, ORDER_LIMIT, 20_000, limit_price=Decimal("158.100"), oco_group_id="G1"),
            quotes,
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        self.assertEqual(sl.status, STATUS_PENDING)
        self.assertEqual(tp.status, STATUS_PENDING)
        now2 = now + timedelta(seconds=5)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(now2, bid="157.850", ask="157.860")}, now2, account(), [pos_2l()], RATES
        )
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].order_id, sl.order_id)
        self.assertEqual(result.fills[0].price, Decimal("157.850"))
        self.assertEqual(tp.status, STATUS_CANCELLED)
        events = [e for e in engine.drain_events() if e[0] == tp.order_id]
        self.assertTrue(any(e[1] == "cancelled" and e[2] == "OCO_CANCELLED" for e in events))
        now3 = now + timedelta(seconds=10)
        later = engine.process_tick(
            {"USDJPY": make_quote_at(now3, bid="158.200", ask="158.205")}, now3, account(), [pos_2l()], RATES
        )
        self.assertEqual(later.fills, [])

    def test_t34_oco_units_conflict_rejects_both(self):
        """OCO 两腿数量不一致 → 两条都 rejected(OCO_CONFLICT)，不会半组生效。"""
        engine = make_engine()
        now = NOW
        quotes = {"USDJPY": make_quote_at(now)}
        sl = engine.submit(
            close_req(SIDE_SELL, ORDER_STOP, 20_000, trigger_price=Decimal("157.900"), oco_group_id="G2"),
            quotes,
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        tp = engine.submit(
            close_req(SIDE_SELL, ORDER_LIMIT, 10_000, limit_price=Decimal("158.100"), oco_group_id="G2"),
            quotes,
            now,
            account(),
            [pos_2l()],
            RATES,
        )
        self.assertEqual(tp.status, STATUS_REJECTED)
        self.assertEqual(tp.reject_reason, "OCO_CONFLICT")
        self.assertEqual(sl.status, STATUS_REJECTED)
        self.assertEqual(sl.reject_reason, "OCO_CONFLICT")
        self.assertEqual(engine.pending, [])

    def test_t37_two_sided_positions_not_netted(self):
        """T37：buy 1万 成交后 sell 1万 成交（allow_hedging=true）；平 sell 1万 只平多头。"""
        cfg = make_cfg()
        cfg.account.allow_hedging = True
        cfg.risk.forbid_add_to_loser = False
        engine = make_engine(cfg=cfg)
        now = NOW
        quotes = {"USDJPY": make_quote_at(now)}
        buy = engine.submit(open_req(side=SIDE_BUY), quotes, now, account(), [], RATES)
        now2 = now + timedelta(seconds=5)
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(result.fills[0].side, SIDE_BUY)
        positions = [
            pos_2l()
        ]  # 用 2 万保证 sell 1 万也能成交（若非双向持仓会被 hedge 规则拒；此处验证双向持仓放行）
        sell = engine.submit(
            open_req(side=SIDE_SELL, client_order_id="sell1"),
            {"USDJPY": make_quote_at(now2 + timedelta(seconds=5))},
            now2 + timedelta(seconds=5),
            account(),
            positions,
            RATES,
        )
        self.assertEqual(sell.status, STATUS_PENDING)
        now3 = now2 + timedelta(seconds=10)
        result2 = engine.process_tick(
            {"USDJPY": make_quote_at(now3)}, now3, account(), positions, RATES
        )
        self.assertEqual(result2.fills[0].side, SIDE_SELL)
        # 平仓只针对多头：close sell 1 万 → Fill.position_id = P2L（多头）
        engine.submit(
            close_req(SIDE_SELL, ORDER_MARKET, 10_000),
            {"USDJPY": make_quote_at(now3 + timedelta(seconds=5))},
            now3 + timedelta(seconds=5),
            account(),
            positions,
            RATES,
        )
        now4 = now3 + timedelta(seconds=10)
        result3 = engine.process_tick(
            {"USDJPY": make_quote_at(now4)}, now4, account(), positions, RATES
        )
        self.assertEqual(result3.fills[0].position_id, "P2L")
        self.assertEqual(result3.fills[0].side, SIDE_SELL)

    def test_s3_loss_cut_cancels_new_orders(self):
        """S3：行情暴跌后维持率 <50% → 取消剩余新规挂单，last_loss_cut_triggered=True。"""
        cfg = make_cfg()
        cfg.risk.forbid_add_to_loser = False  # 本用例要挂同向新规单，绕开逆势加仓限制
        engine = make_engine(cfg=cfg)
        now = NOW
        big_long = Position(
            position_id="PBIG",
            pair="USDJPY",
            side=SIDE_BUY,
            units=90_000,
            entry_price=Decimal("160.000"),
            opened_at=NOW - timedelta(days=1),
        )
        # 先在正常行情下挂一条新规限价（day_start 校准到 80 万，避免日亏停手拦截）
        acc = account(day_start_equity=800_000)
        quotes0 = {"USDJPY": make_quote_at(now)}
        pending = engine.submit(
            open_req(order_type=ORDER_LIMIT, limit_price=Decimal("157.000")),
            quotes0,
            now,
            acc,
            [big_long],
            RATES,
        )
        self.assertEqual(pending.status, STATUS_PENDING)
        # 暴跌 tick：小余额账户下行权：净值 = 30,000 + (151.300−160.000)×90,000 ÷ 23.688 ≈ −3,054
        crash_now = now + timedelta(seconds=5)
        result = engine.process_tick(
            {"USDJPY": make_quote_at(crash_now, bid="151.300", ask="151.310")},
            crash_now,
            account(balance=30_000),
            [big_long],
            RATES,
        )
        self.assertTrue(engine.last_loss_cut_triggered)
        self.assertEqual(result.fills, [])  # 引擎不自行强平（scheduler 负责）
        self.assertEqual(pending.status, STATUS_CANCELLED)
        events = engine.drain_events()
        self.assertTrue(any(e[0] == pending.order_id and e[2] == "RISK_CANCELLED" for e in events))

    def test_commission_fee(self):
        """手续费单列模式：1万单位 × 1.5 元/万 → ceil(1.5) = 2 元。"""
        engine = make_engine(cfg=make_cfg(commission=1.5))
        now, now2 = NOW, NOW + timedelta(seconds=5)
        engine.submit(open_req(), {"USDJPY": make_quote_at(now)}, now, account(), [], RATES)
        result = engine.process_tick({"USDJPY": make_quote_at(now2)}, now2, account(), [], RATES)
        self.assertEqual(result.fills[0].fee, 2)

    def test_all_orders_and_get_order_views(self):
        engine = make_engine()
        now = NOW
        quotes = {"USDJPY": make_quote_at(now)}
        o1 = engine.submit(open_req(order_type=ORDER_LIMIT, limit_price=Decimal("157.000")), quotes, now, account(), [], RATES)
        o2 = engine.submit(open_req(order_type=ORDER_LIMIT, limit_price=Decimal("156.000")), quotes, now, account(), [], RATES)
        all_ids = [o.order_id for o in engine.all_orders()]
        self.assertEqual(all_ids, sorted([o1.order_id, o2.order_id]))
        self.assertEqual([o.order_id for o in engine.pending], sorted([o1.order_id, o2.order_id]))
        self.assertIs(engine.get_order(o1.order_id), o1)
        self.assertIsNone(engine.get_order("NOPE"))


if __name__ == "__main__":
    unittest.main()
