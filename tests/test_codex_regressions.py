"""Codex 发布审议 P0 回归用例固化（离线合成行情 + 临时 SQLite，不依赖 Codex 目录）。

包含：
1. Codex 交付脚本的三个账本回归（成交重启 / 双仓周三隔夜 / 同轮仓位上限）；
2. P0-1 事务性：成交重复抵达幂等、账本写失败不污染内存；
3. P0-2 日切：关闭 swap 仍重置日风控、跨周末不重复计息、停机 >10 天水位只推进到已完成日期；
4. P0-3/P0-4 行情与风控：旧行情不触发强平/成交、新鲜快照恢复后才强平一次、缺失换算锚挂起；
5. P0-5 止损预算与保护单：LLM/手动两条路径、父单成交后按持仓建 OCO 保护单、任一腿成交撤销另一腿；
6. P0-6 人工暂停/慢模型/模型平仓：慢模型不阻塞主 tick、暂停后丢弃旧决策、平空仓用正向买单；
7. §三/§四：合计保证金"先合计再 ceil"、恰好 50% 不强平、v1→v2 含 WAL 一致性备份、旧 v2 swaps 表迁移。

运行：项目根 `<botpython> -m unittest discover -s tests -t .`
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11 由 preflight 拦截
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
PKG = "_mai_fx_codex_regression_pkg"
START = datetime(2026, 11, 25, 13, 0, tzinfo=timezone.utc)  # 周三纽约 08:00
ROLLOVER = START.replace(hour=22, minute=1)  # 周三纽约 17:01
CAPITAL = 100_000
UNITS = 10_000

_MODULES: SimpleNamespace | None = None


def load_modules() -> SimpleNamespace:
    """按宿主包方式加载 scheduler/models/ledger（不需要 maibot_sdk）。"""

    global _MODULES
    if _MODULES is not None:
        return _MODULES
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if PKG not in sys.modules:
        package = ModuleType(PKG)
        package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
        sys.modules[PKG] = package
    _MODULES = SimpleNamespace(
        **{
            name: importlib.import_module(f"{PKG}.{name}")
            for name in ("scheduler", "models", "ledger", "accounting", "pairs", "report")
        }
    )
    return _MODULES


def require_tomllib() -> None:
    if tomllib is None:
        raise unittest.SkipTest("需要 Python 3.11+ 的 tomllib")


def test_config() -> SimpleNamespace:
    """与 Codex 复核脚本一致的配置（固定利率、隔离点差浮亏、关闭日历/自动交易）。"""

    require_tomllib()
    with (ROOT / "config.example.toml").open("rb") as file:
        config = SimpleNamespace(
            **{
                section: SimpleNamespace(**values)
                for section, values in tomllib.load(file).items()
            }
        )
    config.account.initial_balance = CAPITAL
    config.account.leverage = 10
    config.account.swap_enabled = True
    config.account.commission_per_10k = 0
    config.swap.triple_enabled = True
    config.swap.triple_weekday = 2
    config.risk.max_positions = 6
    config.risk.forbid_add_to_loser = False
    config.decision.auto_trade = False
    config.calendar.enabled = False
    config.calendar.source = "static"
    return config


class Quotes:
    """固定合成行情：USDJPY=140、USDCNY=7（与 Codex 复核脚本一致）。"""

    def __init__(self, models: SimpleNamespace) -> None:
        self.models = models

    async def fetch(self, codes, now, seq):
        result = {}
        for pair, price in (("USDJPY", "140"), ("USDCNY", "7")):
            rate = Decimal(price)
            result[pair] = self.models.QuoteSnapshot(
                pair=pair,
                bid=rate,
                ask=rate,
                mid=rate,
                quote_ts=now,
                recv_ts=now,
                seq=seq,
            )
        return result


class ScriptedSource:
    """可切换模式/价格的行情源（P0-4/P0-6 用例用）。"""

    def __init__(self, models: SimpleNamespace) -> None:
        self.models = models
        self.price = "140"
        self.mode = "normal"

    async def fetch(self, codes, now, seq):
        quotes = {}
        for pair, price in (("USDJPY", self.price), ("USDCNY", "7")):
            if pair == "USDCNY" and self.mode == "missing_usdcny":
                continue
            quote_ts = now
            if self.mode == "stale_source":
                quote_ts = now - timedelta(hours=2)
            rate = Decimal(price)
            quotes[pair] = self.models.QuoteSnapshot(
                pair=pair,
                bid=rate,
                ask=rate,
                mid=rate,
                quote_ts=quote_ts,
                recv_ts=now,
                seq=seq,
            )
        return quotes


class Fixture:
    def __init__(self, modules: SimpleNamespace, config: SimpleNamespace, db_path: Path) -> None:
        self.modules = modules
        self.config = config
        self.db_path = db_path

    def database(self):
        return closing(self.modules.ledger.Ledger(self.db_path))

    def engine(self, db, source=None):
        db.init(CAPITAL)
        engine = self.modules.scheduler.TradingEngine(
            ctx=None,
            config=self.config,
            ledger=db,
            quote_source=source or Quotes(self.modules.models),
        )
        # 固定模拟利率，防止未来品种表调价使本例的金额预期发生变化。
        engine.pairs["USDJPY"] = replace(engine.pairs["USDJPY"], swap_long_jpy=Decimal("160"))
        return engine

    def clock(self, when: datetime):
        return patch.object(self.modules.scheduler.market_clock, "now_utc", return_value=when)

    def tick(self, engine, when: datetime):
        with self.clock(when):
            asyncio.run(engine.tick_once())

    def buy(self, engine, when: datetime, *, units: int = UNITS, **kwargs):
        with self.clock(when):
            return engine.manual_order(
                pair_text="USDJPY", side="buy", units=units, user="fixture", **kwargs
            )


def require_order(result) -> str:
    accepted, order_id = result
    if not accepted:
        raise AssertionError(f"测试需要一笔已受理的订单，实际 {order_id}")
    return order_id


def check(expected, observed) -> None:
    if observed != expected:
        raise AssertionError(f"预期 {expected}，实际 {observed}")


# ---------------------------------------------------------------------------
# 一、Codex 三个账本回归（与其交付脚本逐条对应）
# ---------------------------------------------------------------------------


class CodexLedgerRegressionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        require_tomllib()
        self.modules = load_modules()
        self.config = test_config()
        self._tmp = tempfile.TemporaryDirectory(prefix="mai-fx-codex-")
        self.fixture = Fixture(self.modules, self.config, Path(self._tmp.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restart_after_fill_keeps_terminal_state(self) -> None:
        """Codex ①：成交→重启后 order=filled、无异常、成交/内外持仓=1、当日笔数=1。"""

        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            order_id = require_order(fixture.buy(engine, START))
            fixture.tick(engine, START + timedelta(seconds=1))
            stored_order = db.load_order(order_id)
            status = stored_order.status if stored_order else "缺单"
        with fixture.database() as db:
            restored = fixture.engine(db)
            error = "无"
            try:
                fixture.tick(restored, START + timedelta(seconds=2))
            except Exception as exc:  # noqa: BLE001 - 记录后断言，不让异常直接吞掉用例
                error = type(exc).__name__
            observed = (
                status,
                error,
                len(db.list_trades()),
                len(restored.positions),
                len(db.load_positions()),
                db.load_account().trades_today,
            )
        check(("filled", "无", 1, 1, 1, 1), observed)

    def test_two_positions_wednesday_swap(self) -> None:
        """Codex ②：两笔同品种同向同手数持仓，周三各 24 元，流水/应计/余额/总利息一致。"""

        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            require_order(fixture.buy(engine, START))
            require_order(fixture.buy(engine, START + timedelta(seconds=1)))
            fixture.tick(engine, START + timedelta(seconds=2))
            check(2, len(db.load_positions()))
            # 160 日元 × 周三 3 天 ÷ 20 日元/元，每个 1 万单位仓位应得 24 元。
            for when in (ROLLOVER, ROLLOVER + timedelta(seconds=1)):
                fixture.tick(engine, when)
                amounts = sorted(row["amount"] for row in db.list_swaps())
                accrued = sorted(p.swap_accrued for p in db.load_positions())
                account = db.load_account()
                observed = (
                    amounts,
                    accrued,
                    account.balance,
                    account.swap_paid,
                    db.check_accounting(),
                )
                check(([24, 24], [24, 24], CAPITAL + 48, 48, []), observed)

    def test_position_limit_within_one_tick(self) -> None:
        """Codex ③：max_positions=1 时同轮两笔市价单只成 1 笔（1 万单位）。"""

        fixture = self.fixture
        fixture.config.risk.max_positions = 1
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            require_order(fixture.buy(engine, START))
            fixture.buy(engine, START + timedelta(seconds=1))
            fixture.tick(engine, START + timedelta(seconds=2))
            positions = db.load_positions()
            observed = (len(db.list_trades()), len(positions), sum(p.units for p in positions))
        check((1, 1, UNITS), observed)


# ---------------------------------------------------------------------------
# 二、P0-1 事务性 & 幂等
# ---------------------------------------------------------------------------


class FillTransactionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = load_modules()
        self.config = test_config()
        self._tmp = tempfile.TemporaryDirectory(prefix="mai-fx-tx-")
        self.fixture = Fixture(self.modules, self.config, Path(self._tmp.name) / "tx.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_duplicate_fill_is_idempotent(self) -> None:
        """相同成交重复抵达：恢复到同一状态，不再加仓/加钱（P0-1）。"""

        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            order_id = require_order(fixture.buy(engine, START))
            fixture.tick(engine, START + timedelta(seconds=1))
            order = engine.engine.get_order(order_id)
            assert order is not None and order.filled_price is not None
            account_before = db.load_account()
            balance_before = account_before.balance
            duplicate = self.modules.models.Fill(
                order_id=order_id,
                pair="USDJPY",
                side="buy",
                position_type="open",
                units=UNITS,
                price=order.filled_price,
                slippage_pips=order.slippage_pips,
                fee=0,
                realized_pnl=0,
                position_id=order_id,
                ts=START + timedelta(seconds=1),
            )
            engine._apply_fills([duplicate], START + timedelta(seconds=2))
            account_after = db.load_account()
            check(1, len(db.list_trades()))
            check(1, len(engine.positions))
            check(1, len(db.load_positions()))
            check(1, account_after.trades_today)
            check(balance_before, account_after.balance)
            check(account_after.balance, engine.account.balance)

    def test_failed_commit_does_not_mutate_memory(self) -> None:
        """账本事务失败时内存账户/持仓不更新（P0-1：先提交、后改内存）。"""

        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            order_id = require_order(fixture.buy(engine, START))
            with patch.object(db, "commit_fill", side_effect=RuntimeError("DB 写失败")):
                with self.assertRaises(RuntimeError):
                    fixture.tick(engine, START + timedelta(seconds=1))
            self.assertEqual(engine.positions, [])
            self.assertEqual(engine.account.trades_today, 0)
            self.assertEqual(db.list_trades(), [])
            self.assertEqual(db.load_positions(), [])
            stored = db.load_order(order_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.status, "pending")


# ---------------------------------------------------------------------------
# 三、P0-2 日切：开关分离 / 周末 / 停机补算水位
# ---------------------------------------------------------------------------


class RolloverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = load_modules()
        self.config = test_config()
        self._tmp = tempfile.TemporaryDirectory(prefix="mai-fx-rollover-")
        self.fixture = Fixture(self.modules, self.config, Path(self._tmp.name) / "rollover.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_swap_disabled_still_resets_daily_risk(self) -> None:
        """关闭隔夜计息：日切仍重置 day/trades_today/日初净值，不产生利息流水。"""

        self.config.account.swap_enabled = False
        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            require_order(fixture.buy(engine, START))
            fixture.tick(engine, START + timedelta(seconds=1))
            check(1, db.load_account().trades_today)
            equity_before = engine.equity_cny()
            fixture.tick(engine, ROLLOVER)
            account = db.load_account()
            check("2026-11-25", account.day)
            check(0, account.trades_today)
            check(CAPITAL, account.balance)
            check([], db.list_swaps())
            check(int(equity_before), account.day_start_equity)
            check([], db.check_accounting())

    def test_weekend_is_not_charged_twice(self) -> None:
        """跨周末：周四→周一按交易日计息（周四/周五/周一各 1 天），周末不单独计息。"""

        fixture = self.fixture
        start_thu = datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc)  # 周四纽约 08:00
        monday_rollover = datetime(2026, 11, 23, 22, 1, tzinfo=timezone.utc)  # 周一纽约 17:01
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, start_thu)
            require_order(fixture.buy(engine, start_thu))
            fixture.tick(engine, start_thu + timedelta(seconds=1))
            fixture.tick(engine, monday_rollover)
            rows = db.list_swaps()
            amounts = [row["amount"] for row in rows]
            # 三个交易日各记一笔（每笔 8 元），周末不单独计息；合计 24 元
            check([8, 8, 8], amounts)
            check(24, sum(amounts))
            check(CAPITAL + 24, db.load_account().balance)

    def test_catchup_more_than_ten_days_advances_watermark_stepwise(self) -> None:
        """停机 >10 天：只处理已完成日期，水位分两批推进，金额仍完整补算。"""

        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            require_order(fixture.buy(engine, START))
            fixture.tick(engine, START + timedelta(seconds=1))
            # 人为把水位退到 2026-11-04（模拟停机 20 天）
            engine._last_rollover_date = "2026-11-04"
            db.save_meta("last_rollover", "2026-11-04")
            fixture.tick(engine, START + timedelta(seconds=2))
            check("2026-11-14", db.load_meta("last_rollover"))
            check(CAPITAL + 72, db.load_account().balance)
            fixture.tick(engine, START + timedelta(seconds=3))
            check("2026-11-24", db.load_meta("last_rollover"))
            check(CAPITAL + 144, db.load_account().balance)
            check(144, db.load_positions()[0].swap_accrued)
            # 同一日期重复 tick 不再计息（幂等键含交易日 + 持仓 ID）
            fixture.tick(engine, START + timedelta(seconds=4))
            check(CAPITAL + 144, db.load_account().balance)
            check([], db.check_accounting())


# ---------------------------------------------------------------------------
# 四、P0-4 旧行情不得强平/成交；新鲜快照恢复后重新评估
# ---------------------------------------------------------------------------


class StaleQuoteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = load_modules()
        self.config = test_config()
        self._tmp = tempfile.TemporaryDirectory(prefix="mai-fx-stale-")
        self.fixture = Fixture(self.modules, self.config, Path(self._tmp.name) / "stale.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stale_quotes_cannot_trigger_liquidation(self) -> None:
        """旧行情（源时间过期）不触发成交/强平，余额与成交数不变；恢复新鲜后只强平一次。"""

        config = self.config
        config.risk.margin_per_order_ratio = 1.0  # 允许单笔占满净值，构造可强平仓位
        source = ScriptedSource(self.modules.models)
        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db, source=source)
            fixture.tick(engine, START)
            require_order(fixture.buy(engine, START, units=100_000))
            fixture.tick(engine, START + timedelta(seconds=1))
            check(1, len(db.load_positions()))
            trades_before = len(db.list_trades())
            balance_before = db.load_account().balance
            engine.drain_events()  # 只看旧行情阶段的播报
            # 旧行情：源时间倒退 2 小时，价格暴跌也不得强平
            source.mode = "stale_source"
            source.price = "120"
            fixture.tick(engine, START + timedelta(seconds=2))
            fixture.tick(engine, START + timedelta(seconds=3))
            check(trades_before, len(db.list_trades()))
            check(balance_before, db.load_account().balance)
            check(1, len(db.load_positions()))
            guard_events = [text for text in engine.drain_events() if "行情校验未通过" in text]
            check(1, len(guard_events))  # 同一段缺失只播报一次
            # 新鲜完整快照恢复：只在这一轮重新评估并强平
            source.mode = "normal"
            fixture.tick(engine, START + timedelta(seconds=4))
            check(0, len(db.load_positions()))
            check(trades_before + 1, len(db.list_trades()))
            account = db.load_account()
            self.assertIsNotNone(account.loss_cut_cooldown_until, "正常强平入口应设置冷却截止时间")
            check([], db.check_accounting())

    def test_missing_anchor_suspends_and_recovers(self) -> None:
        """缺 USDCNY 换算锚：挂起估值/开仓，恢复后继续（余额与成交数不变）。"""

        source = ScriptedSource(self.modules.models)
        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db, source=source)
            fixture.tick(engine, START)
            source.mode = "missing_usdcny"
            fixture.tick(engine, START + timedelta(seconds=1))
            self.assertIn("RATE_UNAVAILABLE", engine.last_error)
            self.assertEqual(db.list_trades(), [])
            source.mode = "normal"
            fixture.tick(engine, START + timedelta(seconds=2))
            self.assertIsNotNone(engine._rates())
            require_order(fixture.buy(engine, START + timedelta(seconds=2)))
            fixture.tick(engine, START + timedelta(seconds=3))
            check(1, len(db.list_trades()))


# ---------------------------------------------------------------------------
# 五、P0-5 止损预算 / 保护单 / 手动路径
# ---------------------------------------------------------------------------


class ProtectionOrderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = load_modules()
        self.config = test_config()
        self._tmp = tempfile.TemporaryDirectory(prefix="mai-fx-protect-")
        self.fixture = Fixture(self.modules, self.config, Path(self._tmp.name) / "protect.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parent_fill_creates_oco_protection_on_real_position(self) -> None:
        """父单成交后按真实持仓 ID 建止损/止盈两腿（同一 OCO 组）；任一腿成交撤销另一腿。"""

        fixture = self.fixture
        source = ScriptedSource(self.modules.models)
        with fixture.database() as db:
            engine = fixture.engine(db, source=source)
            fixture.tick(engine, START)
            order_id = require_order(
                fixture.buy(
                    engine,
                    START,
                    stop_loss=Decimal("139.000"),
                    take_profit=Decimal("141.000"),
                )
            )
            fixture.tick(engine, START + timedelta(seconds=1))
            self.assertEqual(len(engine.positions), 1)
            position = engine.positions[0]
            self.assertEqual(position.stop_price, Decimal("139.000"))
            self.assertEqual(position.take_price, Decimal("141.000"))
            pending = engine.engine.pending
            self.assertEqual(len(pending), 2, [(o.order_id, o.request.order_type) for o in pending])
            self.assertTrue(all(o.request.position_id == position.position_id for o in pending))
            self.assertEqual(len({o.request.oco_group_id for o in pending}), 1)
            self.assertEqual(len({o.request.client_order_id for o in pending}), 2)
            stored = db.load_orders_pending()
            self.assertEqual(len(stored), 2)
            # 止损腿触发（bid 138.9 <= 139）→ 平仓代价腿成交，止盈腿 OCO 撤销
            stop_leg = next(o for o in pending if o.request.order_type == "stop")
            tp_leg = next(o for o in pending if o.request.order_type == "limit")
            crash_now = START + timedelta(seconds=2)
            source.price = "138.9"
            with fixture.clock(crash_now):
                asyncio.run(engine.tick_once())
            self.assertEqual(engine.positions, [])
            self.assertEqual(engine.engine.get_order(stop_leg.order_id).status, "filled")
            self.assertEqual(engine.engine.get_order(tp_leg.order_id).status, "cancelled")
            self.assertEqual(len(db.list_trades()), 2)
            check([], db.check_accounting())
            self.assertEqual(db.load_positions(), [])
            self.assertEqual(db.load_orders_pending(), [])

    def test_protection_legs_survive_restart_with_stable_ids(self) -> None:
        """保护单落库后重启：两腿仍在、client_order_id 稳定、不会重复建。"""

        fixture = self.fixture
        db_path = fixture.db_path
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            require_order(
                fixture.buy(
                    engine,
                    START,
                    stop_loss=Decimal("139.000"),
                    take_profit=Decimal("141.000"),
                )
            )
            fixture.tick(engine, START + timedelta(seconds=1))
            clients = sorted(o.request.client_order_id for o in engine.engine.pending)
        with fixture.database() as db:
            restored = fixture.engine(db)
            pending = restored.engine.pending
            self.assertEqual(len(pending), 2)
            self.assertEqual(sorted(o.request.client_order_id for o in pending), clients)
            fixture.tick(restored, START + timedelta(seconds=2))
            self.assertEqual(len(restored.engine.pending), 2, "重启后不得重复创建保护单")

    def test_liquidation_cancels_linked_protection_orders(self) -> None:
        """强平与正常成交走同一账本入口，并取消该持仓的关联保护单（P0-1/P0-5）。"""

        config = self.config
        config.risk.margin_per_order_ratio = 1.0  # 允许单笔占满净值
        fixture = self.fixture
        source = ScriptedSource(self.modules.models)
        with fixture.database() as db:
            engine = fixture.engine(db, source=source)
            fixture.tick(engine, START)
            require_order(fixture.buy(engine, START, units=100_000, take_profit=Decimal("150.000")))
            fixture.tick(engine, START + timedelta(seconds=1))
            pending = engine.engine.pending
            self.assertEqual(len(pending), 1, [(o.order_id, o.request.order_type) for o in pending])
            tp_order = pending[0]
            # 暴跌触发强平；止盈腿未触发，应被强平一并撤销
            source.price = "120"
            fixture.tick(engine, START + timedelta(seconds=2))
            self.assertEqual(engine.positions, [])
            self.assertEqual(engine.engine.get_order(tp_order.order_id).status, "cancelled")
            self.assertEqual(len(db.list_trades()), 2)
            self.assertEqual(db.load_orders_pending(), [])
            check([], db.check_accounting())

    def test_manual_protection_risk_is_validated(self) -> None:
        """手动开仓带远端止损：计划风险 >2% 时拒单（RISK_EXCEEDS_LIMIT）。"""

        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            # 140 → 130 止损、1 万单位：计划风险约 5,003 元 > 2,000 元上限
            with fixture.clock(START + timedelta(seconds=1)):
                accepted, reason = engine.manual_order(
                    pair_text="USDJPY",
                    side="buy",
                    units=UNITS,
                    stop_loss=Decimal("130.000"),
                    user="manual",
                )
            self.assertFalse(accepted)
            self.assertEqual(reason, "RISK_EXCEEDS_LIMIT")
            self.assertEqual(engine.engine.pending, [])

    def test_llm_intent_requires_stop_and_sizes_by_risk(self) -> None:
        """LLM 开仓意图：缺止损拒单；远端止损按 2% 预算缩量；止损/止盈进入请求。"""

        fixture = self.fixture
        with fixture.database() as db:
            engine = fixture.engine(db)
            fixture.tick(engine, START)
            now = START + timedelta(seconds=1)
            with fixture.clock(now):
                snapshot = engine._decision_snapshot(now)
                missing = self.modules.models.DecisionIntent(
                    pair="USDJPY", action="open_long", units=UNITS, stop_loss=None
                )
                requests, reasons = engine._intent_to_requests(
                    missing, snapshot, now, engine._rates()
                )
                self.assertEqual(requests, [])
                self.assertEqual(reasons, ["MISSING_STOP_LOSS"])
                wide_stop = self.modules.models.DecisionIntent(
                    pair="USDJPY",
                    action="open_long",
                    units=UNITS,
                    stop_loss=Decimal("130.000"),
                    take_profit=Decimal("142.000"),
                )
                requests, reasons = engine._intent_to_requests(
                    wide_stop, snapshot, now, engine._rates()
                )
                self.assertEqual(reasons, [])
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertLess(request.units, UNITS)
                self.assertGreaterEqual(request.units, 1_000)
                self.assertEqual(request.stop_loss, Decimal("130.000"))
                self.assertEqual(request.take_profit, Decimal("142.000"))


# ---------------------------------------------------------------------------
# 六、P0-6 人工暂停 / 慢模型 / 模型平仓
# ---------------------------------------------------------------------------


class SchedulerAsyncTestCase(unittest.IsolatedAsyncioTestCase):
    """异步用例：直接 await tick_once，用 ScriptedSource 与固定时钟。"""

    def setUp(self) -> None:
        require_tomllib()
        self.modules = load_modules()
        self.config = test_config()
        self.scheduler = self.modules.scheduler
        self._tmp = tempfile.TemporaryDirectory(prefix="mai-fx-async-")
        self.db = self.modules.ledger.Ledger(Path(self._tmp.name) / "async.sqlite3")
        self.db.init(CAPITAL)
        self.source = ScriptedSource(self.modules.models)
        self.engine = self.scheduler.TradingEngine(
            ctx=None, config=self.config, ledger=self.db, quote_source=self.source
        )

    def tearDown(self) -> None:
        if self.engine._decision_task is not None:
            self.engine._decision_task.cancel()
        self.db.close()
        self._tmp.cleanup()

    def clock(self, when: datetime):
        return patch.object(self.scheduler.market_clock, "now_utc", return_value=when)

    async def tick(self, when: datetime) -> None:
        with self.clock(when):
            await self.engine.tick_once()

    async def test_close_short_uses_positive_buy_order(self) -> None:
        """模型 close：按目标持仓逐笔拆反向、正数平仓单（平空仓用买入）。"""

        await self.tick(START)
        with self.clock(START):
            accepted, _order_id = self.engine.manual_order(
                pair_text="USDJPY", side="sell", units=UNITS, user="fixture"
            )
        self.assertTrue(accepted)
        await self.tick(START + timedelta(seconds=1))
        self.assertEqual(len(self.engine.positions), 1)
        self.assertEqual(self.engine.positions[0].side, "sell")
        now = START + timedelta(seconds=2)
        with self.clock(now):
            snapshot = self.engine._decision_snapshot(now)
            intent = self.modules.models.DecisionIntent(
                pair="USDJPY", action="close", order_type="market", units=0
            )
            outcome = self.engine._execute_intent(intent, snapshot)
        self.assertEqual(outcome, "submitted")
        pending = self.engine.engine.pending
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].request.side, "buy")
        self.assertGreater(pending[0].request.units, 0)
        await self.tick(now + timedelta(seconds=1))
        self.assertEqual(self.engine.positions, [])
        trades = self.db.list_trades()
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["position_type"], "close")
        self.assertEqual(trades[0]["side"], "buy")
        check([], self.db.check_accounting())

    async def test_slow_model_does_not_block_tick(self) -> None:
        """慢模型（等待中）不阻塞主 tick；返回后按快照执行缩量开仓。"""

        self.config.decision.auto_trade = True
        gate = asyncio.Event()
        captured: dict = {}

        async def slow_decide(*args, **kwargs):
            captured["called"] = True
            await gate.wait()
            return self.modules.models.DecisionIntent(
                pair="USDJPY",
                action="open_long",
                order_type="market",
                units=UNITS,
                stop_loss=Decimal("130.000"),  # 远端止损：按 2% 预算应缩量
            )

        original = self.modules.scheduler.decision.decide
        self.modules.scheduler.decision.decide = slow_decide
        try:
            await self.tick(START)
            await asyncio.sleep(0)
            task = self.engine._decision_task
            self.assertIsNotNone(task)
            assert task is not None
            self.assertTrue(captured.get("called"))
            # 决策还卡在模型里时，下一轮 tick 仍能立即完成（行情/风控不被阻塞）
            await asyncio.wait_for(self.tick(START + timedelta(seconds=1)), timeout=2)
            self.assertFalse(task.done(), "慢模型不应阻塞主 tick")
            # 释放模型：决策按执行时快照校验后缩量下单
            with self.clock(START + timedelta(seconds=1)):
                gate.set()
                await task
            self.assertEqual(len(self.engine.engine.pending), 1)
            await self.tick(START + timedelta(seconds=2))
            self.assertEqual(len(self.engine.positions), 1)
            self.assertLess(self.engine.positions[0].units, UNITS)
        finally:
            self.modules.scheduler.decision.decide = original

    async def test_pause_discards_stale_decision(self) -> None:
        """暂停后模型才返回：旧决策丢弃，不下单。"""

        self.config.decision.auto_trade = True
        gate = asyncio.Event()

        async def slow_decide(*args, **kwargs):
            await gate.wait()
            return self.modules.models.DecisionIntent(
                pair="USDJPY", action="open_long", units=UNITS, stop_loss=Decimal("139.000")
            )

        original = self.modules.scheduler.decision.decide
        self.modules.scheduler.decision.decide = slow_decide
        try:
            await self.tick(START)
            await asyncio.sleep(0)
            task = self.engine._decision_task
            self.assertIsNotNone(task)
            assert task is not None
            self.engine.pause()
            with self.clock(START + timedelta(seconds=1)):
                gate.set()
                await task
            self.assertEqual(self.engine.last_dropped_decision, "PAUSED")
            self.assertEqual(self.engine.engine.pending, [])
            events = self.engine.drain_events()
            self.assertTrue(any("旧决策已丢弃" in text for text in events), events)
        finally:
            self.modules.scheduler.decision.decide = original
            self.engine.resume()


# ---------------------------------------------------------------------------
# 七、§三 金融口径：合计保证金 / 恰好 50% / 强平状态
# ---------------------------------------------------------------------------


class FinanceRoundingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = load_modules()

    def test_required_margin_total_sums_then_ceils(self) -> None:
        """合计保证金口径固定为「先合计未取整再 ceil」：140.5 → 141，而不是逐品种 ceil 的 142。"""

        models = self.modules.models
        accounting = self.modules.accounting
        pairs = self.modules.pairs.PAIRS
        fx = importlib.import_module(f"{PKG}.fxconv")
        rate_set = fx.FxRates(usdjpy_mid=Decimal("140"), usdcny_mid=Decimal("7"))
        positions = [
            models.Position("P1", "USDJPY", "buy", 1_000, Decimal("140.5"), START),
            models.Position("P2", "EURJPY", "buy", 1_000, Decimal("176.83"), START),
        ]
        quotes = {
            "USDJPY": models.QuoteSnapshot("USDJPY", Decimal("140.5"), Decimal("140.5"), Decimal("140.5"), START, START),
            "EURJPY": models.QuoteSnapshot("EURJPY", Decimal("176.83"), Decimal("176.83"), Decimal("176.83"), START, START),
        }
        total = accounting.required_margin_total(positions, quotes, pairs, rate_set, 100)
        self.assertEqual(total, 159)
        per_pair = sum(
            accounting.required_margin(pairs[pos.pair], pos.units, pos.entry_price, rate_set, 100)
            for pos in positions
        )
        self.assertEqual(per_pair, 160, "逐品种 ceil 再相加是另一种口径，不应与合计口径相同")

    def test_loss_cut_triggers_only_below_fifty_percent(self) -> None:
        """恰好 50% 不强平；严格低于才触发。"""

        accounting = self.modules.accounting
        self.assertFalse(accounting.loss_cut_triggered(50_000, 100_000, Decimal("50")))
        self.assertTrue(accounting.loss_cut_triggered(49_999, 100_000, Decimal("50")))
        self.assertFalse(accounting.loss_cut_triggered(0, 0, Decimal("50")))

    def test_report_status_shows_calendar_failure(self) -> None:
        """财经日历失败/回退状态在状态卡展示。"""

        report = self.modules.report
        text = report.status(
            SimpleNamespace(balance=100_000, realized_pnl=0, fees_paid=0, swap_paid=0, trades_today=0),
            [],
            {},
            {},
            None,
            calendar_status="CALENDAR_ALL_SOURCES_FAILED（已回退静态事件表）",
        )
        self.assertIn("财经日历", text)
        self.assertIn("CALENDAR_ALL_SOURCES_FAILED", text)
        self.assertIn(report.DISCLAIMER, text)


# ---------------------------------------------------------------------------
# 八、§四 账本迁移：v1→v2 一致性备份（含 WAL）/ v2 swaps 表原地升级
# ---------------------------------------------------------------------------

_V1_ACCOUNT_SQL = (
    "CREATE TABLE account (id INTEGER PRIMARY KEY CHECK (id = 1),"
    " initial_balance_jpy INTEGER NOT NULL, balance_jpy INTEGER NOT NULL,"
    " realized_pnl_jpy INTEGER NOT NULL DEFAULT 0, fees_paid_jpy INTEGER NOT NULL DEFAULT 0,"
    " swap_paid_jpy INTEGER NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '',"
    " trades_today INTEGER NOT NULL DEFAULT 0, day_start_equity_jpy INTEGER NOT NULL,"
    " peak_equity_jpy INTEGER NOT NULL, halted INTEGER NOT NULL DEFAULT 0,"
    " loss_cut_pending INTEGER NOT NULL DEFAULT 0, loss_cut_cooldown_until TEXT,"
    " schema_version INTEGER NOT NULL DEFAULT 1);"
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
)


class LedgerMigrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.modules = load_modules()
        self.ledger_mod = self.modules.ledger
        self._tmp = tempfile.TemporaryDirectory(prefix="mai-fx-migrate-")
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_legacy(self, path: Path, *, wal: bool) -> None:
        conn = sqlite3.connect(path)
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.executescript(_V1_ACCOUNT_SQL)
        conn.execute(
            "INSERT INTO account (id, initial_balance_jpy, balance_jpy, day_start_equity_jpy, peak_equity_jpy)"
            " VALUES (1, 1000000, 900000, 1000000, 1200000)"
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        conn.commit()
        if wal:
            # 连接保持打开时复制（模拟 -wal 里还有已提交但未归并的事务）
            copy_dir = self.tmp_path / "walcopy"
            copy_dir.mkdir()
            target = copy_dir / "legacy.sqlite3"
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(path) + suffix)
                if source.exists():
                    target.with_name(target.name + suffix).write_bytes(source.read_bytes())
            conn.close()
            return
        conn.close()

    def test_legacy_v1_with_wal_is_backed_up_consistently(self) -> None:
        """有未归并 WAL 的旧库：SQLite 一致性备份包含已提交数据，校验通过后才切换。"""

        legacy_path = self.tmp_path / "legacy.sqlite3"
        self._make_legacy(legacy_path, wal=True)
        ledger = self.ledger_mod.Ledger(self.tmp_path / "walcopy" / "legacy.sqlite3")
        try:
            self.assertIsNotNone(ledger.backup_path)
            assert ledger.backup_path is not None
            self.assertTrue(ledger.backup_path.exists())
            conn = sqlite3.connect(str(ledger.backup_path))
            try:
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                row = conn.execute("SELECT balance_jpy FROM account WHERE id = 1").fetchone()
                self.assertEqual(int(row[0]), 900_000, "WAL 里的已提交余额必须进入备份")
            finally:
                conn.close()
            ledger.init(CAPITAL)
            self.assertEqual(ledger.load_account().balance, CAPITAL)
        finally:
            ledger.close()

    def test_backup_failure_keeps_original_intact(self) -> None:
        """备份校验失败：抛原因码、原件字节不变、不留半成品备份。"""

        path = self.tmp_path / "legacy.sqlite3"
        self._make_legacy(path, wal=False)
        original = path.read_bytes()
        with patch.object(
            self.ledger_mod.Ledger,
            "_verify_backup",
            side_effect=self.ledger_mod.LedgerError("BACKUP_VERIFY_FAILED"),
        ):
            with self.assertRaises(self.ledger_mod.LedgerError):
                self.ledger_mod.Ledger(path)
        self.assertEqual(path.read_bytes(), original)
        leftovers = [item.name for item in self.tmp_path.iterdir() if ".v1.bak-" in item.name]
        self.assertEqual(leftovers, [])

    def test_v2_swaps_table_is_upgraded_in_place(self) -> None:
        """旧 v2 swaps 表（无 position_id）：原地升级并保留历史流水，新键按持仓 ID 去重。"""

        path = self.tmp_path / "v2.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE swaps (swap_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " rollover_date TEXT NOT NULL, pair TEXT NOT NULL, side TEXT NOT NULL,"
            " units INTEGER NOT NULL, amount INTEGER NOT NULL, days INTEGER NOT NULL,"
            " created_at TEXT NOT NULL,"
            " UNIQUE (rollover_date, pair, side, units, amount, days));"
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
        conn.execute(
            "INSERT INTO swaps (rollover_date, pair, side, units, amount, days, created_at)"
            " VALUES ('2026-11-06', 'USDJPY', 'buy', 10000, 160, 1, '2026-11-06T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()
        ledger = self.ledger_mod.Ledger(path)
        try:
            swaps = ledger.list_swaps()
            self.assertEqual(len(swaps), 1)
            self.assertEqual(swaps[0]["position_id"], "")
            self.assertTrue(
                ledger.insert_swap("2026-11-06", "USDJPY", "buy", 10000, 160, 1, position_id="POS-1")
            )
            self.assertFalse(
                ledger.insert_swap("2026-11-06", "USDJPY", "buy", 10000, 160, 1, position_id="POS-1"),
                "同一交易日 + 同一持仓 ID 必须幂等",
            )
            self.assertTrue(
                ledger.insert_swap("2026-11-06", "USDJPY", "buy", 10000, 160, 1, position_id="POS-2"),
                "不同持仓 ID 必须各记一笔（P0-2 幂等键修复）",
            )
            self.assertEqual(len(ledger.list_swaps()), 3)
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
