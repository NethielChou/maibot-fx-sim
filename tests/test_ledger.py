"""账本单测：账户/持仓/成交/订单/事件/隔夜利息/权益/决策/恒等式/幂等/重置。

全部离线（tempfile 建库），覆盖正常路径 + 边界 + 异常原因码。
运行：项目根目录 ``<botpython> -m unittest discover -s tests -t . -v``
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # 单测以顶层模块导入（插件运行时走包内相对导入）
    sys.path.insert(0, str(PROJECT_ROOT))

import ledger as ledger_mod  # noqa: E402
import models  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def make_fill(
    order_id: str = "ORD-1",
    *,
    pair: str = "USDJPY",
    side: str = models.SIDE_BUY,
    position_type: str = models.POS_OPEN,
    units: int = 10_000,
    price: str = "158.005",
    fee: int = 0,
    realized_pnl: int = 0,
    position_id: str | None = "POS-1",
) -> models.Fill:
    return models.Fill(
        order_id=order_id,
        pair=pair,
        side=side,
        position_type=position_type,
        units=units,
        price=Decimal(price),
        slippage_pips=Decimal("0.2"),
        fee=fee,
        realized_pnl=realized_pnl,
        position_id=position_id,
        ts=NOW,
    )


def make_position(
    position_id: str = "POS-1",
    *,
    pair: str = "USDJPY",
    side: str = models.SIDE_BUY,
    units: int = 10_000,
    entry_price: str = "158.005",
    margin: int = 2_668,
) -> models.Position:
    return models.Position(
        position_id=position_id,
        pair=pair,
        side=side,
        units=units,
        entry_price=Decimal(entry_price),
        opened_at=NOW,
        margin=margin,
        stop_price=Decimal("157.000"),
        take_price=None,
    )


def make_order(
    order_id: str = "ORD-1",
    *,
    client_order_id: str = "acc:msg:1",
    status: str = models.STATUS_PENDING,
    units: int = 10_000,
) -> models.Order:
    request = models.OrderRequest(
        pair="USDJPY",
        side=models.SIDE_BUY,
        position_type=models.POS_OPEN,
        order_type=models.ORDER_LIMIT,
        units=units,
        client_order_id=client_order_id,
        limit_price=Decimal("158.000"),
        created_by="qq:10001",
        reason="单测",
    )
    return models.Order(order_id=order_id, request=request, status=status, created_at=NOW, updated_at=NOW)


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.sqlite3"
        self.ledger = ledger_mod.Ledger(self.db_path)
        # 固定 100 万本金，便于断言账本恒等式（默认开户金额另有专门用例）
        self.ledger.init(1_000_000)

    def tearDown(self) -> None:
        self.ledger.close()
        self._tmp.cleanup()

    # ---------- 账户 ----------

    def test_init_creates_default_account(self) -> None:
        account = self.ledger.load_account()
        self.assertEqual(account.balance, 1_000_000)
        self.assertEqual(account.initial_balance, 1_000_000)
        self.assertEqual(account.schema_version, ledger_mod.SCHEMA_VERSION)
        self.assertEqual(self.ledger.quick_check(), [])
        mode = self.ledger.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")

    def test_init_without_argument_uses_new_default(self) -> None:
        """修复「首建库固定 100 万」：不传参时按默认 10 万元开户。"""
        path = Path(self._tmp.name) / "default.sqlite3"
        other = ledger_mod.Ledger(path)
        try:
            other.init()
            account = other.load_account()
            self.assertEqual(account.balance, ledger_mod.DEFAULT_INITIAL_BALANCE)
            self.assertEqual(account.initial_balance, 100_000)
        finally:
            other.close()

    def test_legacy_v1_database_is_backed_up_and_rebuilt(self) -> None:
        """旧版（v1 日元列）库：先备份成 *.v1.bak-*，再重建空库开户，不做金额静默迁移。"""
        path = Path(self._tmp.name) / "legacy.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
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
        conn.execute(
            "INSERT INTO account (id, initial_balance_jpy, balance_jpy, day_start_equity_jpy, peak_equity_jpy)"
            " VALUES (1, 1000000, 900000, 1000000, 1200000)"
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        conn.commit()
        conn.close()
        old_bytes = path.read_bytes()

        legacy = ledger_mod.Ledger(path)
        try:
            self.assertIsNotNone(legacy.backup_path)
            assert legacy.backup_path is not None
            self.assertIn(".v1.bak-", legacy.backup_path.name)
            self.assertTrue(legacy.backup_path.exists())
            self.assertEqual(legacy.backup_path.read_bytes(), old_bytes)
            legacy.init(int(100_000))
            account = legacy.load_account()
            self.assertEqual(account.balance, 100_000)
            self.assertEqual(account.initial_balance, 100_000)
            self.assertEqual(account.schema_version, ledger_mod.SCHEMA_VERSION)
            self.assertEqual(legacy.load_positions(), [])
            self.assertEqual(legacy.check_accounting(account, []), [])
        finally:
            legacy.close()

    def test_current_database_is_not_backed_up(self) -> None:
        """已经是 v2 的库再次打开不会触发备份/重建。"""
        reopened = ledger_mod.Ledger(self.db_path)
        try:
            self.assertIsNone(reopened.backup_path)
            self.assertEqual(reopened.load_account().balance, 1_000_000)
        finally:
            reopened.close()
    def test_account_roundtrip(self) -> None:
        account = self.ledger.load_account()
        account.balance = 888_000
        account.realized_pnl = -12_000
        account.fees_paid = 500
        account.swap_paid = 160
        account.day = "2026-11-06"
        account.trades_today = 3
        account.halted = True
        account.loss_cut_pending = True
        account.loss_cut_cooldown_until = NOW
        account.peak_equity = 1_200_000
        self.ledger.save_account(account)
        loaded = self.ledger.load_account()
        self.assertEqual(loaded.balance, 888_000)
        self.assertEqual(loaded.realized_pnl, -12_000)
        self.assertEqual(loaded.fees_paid, 500)
        self.assertEqual(loaded.swap_paid, 160)
        self.assertEqual(loaded.day, "2026-11-06")
        self.assertEqual(loaded.trades_today, 3)
        self.assertTrue(loaded.halted)
        self.assertTrue(loaded.loss_cut_pending)
        self.assertEqual(loaded.loss_cut_cooldown_until, NOW)
        self.assertEqual(loaded.peak_equity, 1_200_000)

    # ---------- 持仓 ----------

    def test_position_roundtrip_and_delete(self) -> None:
        position = make_position()
        self.ledger.upsert_position(position)
        loaded = self.ledger.load_positions()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].position_id, "POS-1")
        self.assertEqual(loaded[0].entry_price, Decimal("158.005"))
        self.assertEqual(loaded[0].stop_price, Decimal("157.000"))
        self.assertIsNone(loaded[0].take_price)
        self.assertEqual(loaded[0].opened_at, NOW)
        position.units = 5_000
        self.ledger.upsert_position(position)
        self.assertEqual(self.ledger.load_positions()[0].units, 5_000)
        self.ledger.delete_position("POS-1")
        self.assertEqual(self.ledger.load_positions(), [])

    def test_position_invalid_units_raises(self) -> None:
        with self.assertRaises(ledger_mod.LedgerError) as ctx:
            self.ledger.upsert_position(make_position(units=0))
        self.assertIn("INVALID_UNITS", str(ctx.exception))

    # ---------- 成交 ----------

    def test_open_trade_updates_balance_and_identity(self) -> None:
        fill = make_fill(fee=200)
        trade_id = self.ledger.insert_trade(fill)
        self.assertEqual(trade_id, "ORD-1")
        account = self.ledger.load_account()
        self.assertEqual(account.balance, 1_000_000 - 200)
        self.assertEqual(account.fees_paid, 200)
        self.assertEqual(account.realized_pnl, 0)
        self.assertEqual(self.ledger.check_accounting(account, []), [])

    def test_close_trade_updates_balance_and_realized(self) -> None:
        self.ledger.insert_trade(make_fill())
        self.ledger.insert_trade(
            make_fill(
                "ORD-2",
                side=models.SIDE_SELL,
                position_type=models.POS_CLOSE,
                price="158.105",
                realized_pnl=1_000,
            )
        )
        account = self.ledger.load_account()
        self.assertEqual(account.balance, 1_001_000)
        self.assertEqual(account.realized_pnl, 1_000)
        self.assertEqual(self.ledger.check_accounting(account, []), [])
        # 恒等式显式核对：余额 = 初始 + Σ已实现 − Σ手续费 + Σswap
        self.assertEqual(account.balance, account.initial_balance + 1_000 - 0 + 0)

    def test_duplicate_trade_is_rejected(self) -> None:
        self.ledger.insert_trade(make_fill())
        with self.assertRaises(ledger_mod.DuplicateTradeError) as ctx:
            self.ledger.insert_trade(make_fill())
        self.assertIn("DUPLICATE_TRADE", str(ctx.exception))
        rows = self.ledger.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(int(rows), 1)

    def test_invalid_fill_units_raises_reason_code(self) -> None:
        with self.assertRaises(ledger_mod.LedgerError) as ctx:
            self.ledger.insert_trade(make_fill(units=0))
        self.assertIn("INVALID_UNITS", str(ctx.exception))

    # ---------- 订单/幂等 ----------

    def test_order_pending_roundtrip(self) -> None:
        order = make_order()
        order.request.expires_at = NOW
        self.ledger.save_order(order)
        pending = self.ledger.load_orders_pending()
        self.assertEqual(len(pending), 1)
        loaded = pending[0]
        self.assertEqual(loaded.order_id, "ORD-1")
        self.assertEqual(loaded.status, models.STATUS_PENDING)
        self.assertEqual(loaded.request.limit_price, Decimal("158.000"))
        self.assertEqual(loaded.request.units, 10_000)
        self.assertEqual(loaded.request.expires_at, NOW)
        self.assertEqual(loaded.request.created_by, "qq:10001")
        found = self.ledger.find_order_by_client_id("acc:msg:1")
        self.assertIsNotNone(found)
        # 成交后不再算 pending
        order.status = models.STATUS_FILLED
        order.updated_at = NOW
        self.ledger.save_order(order)
        self.assertEqual(self.ledger.load_orders_pending(), [])

    def test_duplicate_client_order_id_raises(self) -> None:
        self.ledger.save_order(make_order("ORD-1", client_order_id="dup"))
        with self.assertRaises(ledger_mod.DuplicateClientOrderIdError) as ctx:
            self.ledger.save_order(make_order("ORD-2", client_order_id="dup"))
        self.assertIn("DUPLICATE_CLIENT_ORDER_ID", str(ctx.exception))

    def test_order_events_append_only(self) -> None:
        self.ledger.append_order_event("ORD-1", "created", "来自单测")
        self.ledger.append_order_event("ORD-1", "pending", "")
        self.ledger.append_order_event("ORD-1", "filled", "成交 158.005")
        events = self.ledger.load_order_events("ORD-1")
        self.assertEqual([item["seq"] for item in events], [1, 2, 3])
        self.assertEqual([item["event_type"] for item in events], ["created", "pending", "filled"])
        self.assertEqual(events[2]["detail"], "成交 158.005")

    # ---------- 隔夜利息 ----------

    def test_swap_idempotent_and_updates_identity(self) -> None:
        first = self.ledger.insert_swap("2026-11-06", "USDJPY", models.SIDE_BUY, 10_000, 160, 1)
        second = self.ledger.insert_swap("2026-11-06", "USDJPY", models.SIDE_BUY, 10_000, 160, 1)
        self.assertTrue(first)
        self.assertFalse(second, "同日同参数重复入账必须幂等")
        account = self.ledger.load_account()
        self.assertEqual(account.balance, 1_000_160)
        self.assertEqual(account.swap_paid, 160)
        self.assertEqual(self.ledger.swaps_total(), 160)
        self.assertEqual(self.ledger.check_accounting(account, []), [])
        self.assertEqual(len(self.ledger.list_swaps()), 1)

    def test_negative_swap_reduces_balance(self) -> None:
        self.ledger.insert_swap("2026-11-06", "USDJPY", models.SIDE_SELL, 10_000, -210, 1)
        account = self.ledger.load_account()
        self.assertEqual(account.balance, 999_790)
        self.assertEqual(self.ledger.check_accounting(account, []), [])

    # ---------- 查询/快照/决策/meta ----------

    def test_list_trades_order_and_fields(self) -> None:
        self.ledger.insert_trade(make_fill("ORD-1", price="157.000"))
        self.ledger.insert_trade(make_fill("ORD-2", price="158.000", position_type=models.POS_CLOSE, realized_pnl=10_000))
        trades = self.ledger.list_trades(limit=1)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["order_id"], "ORD-2")
        self.assertEqual(trades[0]["price"], Decimal("158"))
        self.assertEqual(trades[0]["realized_pnl"], 10_000)
        self.assertIsInstance(trades[0]["ts"], datetime)

    def test_equity_snapshot_replace(self) -> None:
        self.ledger.save_equity(NOW, 1_000_000)
        self.ledger.save_equity(NOW, 1_000_500)
        snapshots = self.ledger.list_equity()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["equity"], 1_000_500)

    def test_decision_and_meta_roundtrip(self) -> None:
        self.ledger.save_decision("DEC-1", "llm", '{"action":"hold"}', "hold")
        record = self.ledger.load_decision("DEC-1")
        self.assertIsNotNone(record)
        self.assertEqual(record["outcome"], "hold")
        self.ledger.save_meta("last_rollover", "2026-11-06")
        self.assertEqual(self.ledger.load_meta("last_rollover"), "2026-11-06")
        self.assertIsNone(self.ledger.load_meta("nope"))
        self.ledger.delete_meta("last_rollover")
        self.assertIsNone(self.ledger.load_meta("last_rollover"))

    # ---------- 一致性检查 ----------

    def test_check_accounting_detects_tampered_balance(self) -> None:
        self.ledger.insert_trade(make_fill(fee=100))
        with self.ledger.conn:
            self.ledger.conn.execute("UPDATE account SET balance = balance + 1")
        issues = self.ledger.check_accounting(self.ledger.load_account(), self.ledger.load_positions())
        self.assertTrue(issues)
        self.assertTrue(any("ACCOUNT_IDENTITY_MISMATCH" in item for item in issues), issues)

    def test_check_accounting_detects_position_mismatch(self) -> None:
        self.ledger.upsert_position(make_position())
        mismatched = make_position()
        mismatched.units = 20_000
        issues = self.ledger.check_accounting(self.ledger.load_account(), [mismatched])
        self.assertTrue(any("POSITION_ROW_MISMATCH" in item for item in issues), issues)

    def test_check_accounting_flags_non_lot_units(self) -> None:
        self.ledger.upsert_position(make_position(units=5_500))
        issues = self.ledger.check_accounting(self.ledger.load_account(), self.ledger.load_positions())
        self.assertTrue(any("POSITION_INVALID" in item for item in issues), issues)

    # ---------- 复合事务 ----------

    def test_commit_fill_is_atomic_except_on_conflict(self) -> None:
        self.ledger.save_order(make_order("ORD-0", client_order_id="dup", status=models.STATUS_FILLED))
        position = make_position()
        fill = make_fill("ORD-1")
        trade_id = self.ledger.commit_fill(
            fill,
            position=position,
            order=make_order("ORD-1", client_order_id="acc:msg:2"),
            event="filled",
            event_detail="单测成交",
        )
        self.assertEqual(trade_id, "ORD-1")
        self.assertEqual(len(self.ledger.list_trades()), 1)
        self.assertEqual(len(self.ledger.load_positions()), 1)
        self.assertEqual(len(self.ledger.load_orders_pending()), 1)
        self.assertEqual(len(self.ledger.load_order_events("ORD-1")), 1)
        # 同一事务里订单 client_order_id 冲突 → 整笔回滚（成交不落库）
        with self.assertRaises(ledger_mod.DuplicateClientOrderIdError):
            self.ledger.commit_fill(
                make_fill("ORD-2"),
                order=make_order("ORD-2", client_order_id="dup"),
            )
        self.assertEqual(len(self.ledger.list_trades()), 1, "事务回滚后不应残留成交")

    def test_commit_fill_can_delete_position_atomically(self) -> None:
        self.ledger.upsert_position(make_position())
        self.ledger.commit_fill(
            make_fill("ORD-9", position_type=models.POS_CLOSE, realized_pnl=-50),
            delete_position_id="POS-1",
        )
        self.assertEqual(self.ledger.load_positions(), [])
        self.assertEqual(self.ledger.load_account().realized_pnl, -50)

    # ---------- 重置/重开 ----------

    def test_reset_clears_everything(self) -> None:
        self.ledger.insert_trade(make_fill())
        self.ledger.upsert_position(make_position())
        self.ledger.save_order(make_order())
        self.ledger.insert_swap("2026-11-06", "USDJPY", models.SIDE_BUY, 10_000, 160, 1)
        self.ledger.save_meta("paused", "1")
        self.ledger.reset(500_000)
        account = self.ledger.load_account()
        self.assertEqual(account.balance, 500_000)
        self.assertEqual(account.initial_balance, 500_000)
        self.assertEqual(self.ledger.load_positions(), [])
        self.assertEqual(self.ledger.list_trades(), [])
        self.assertEqual(self.ledger.load_orders_pending(), [])
        self.assertEqual(self.ledger.list_swaps(), [])
        self.assertIsNone(self.ledger.load_meta("paused"))
        self.assertEqual(self.ledger.check_accounting(account, []), [])

    def test_reopen_persists_account_and_pending_orders(self) -> None:
        self.ledger.insert_trade(make_fill(fee=100))
        self.ledger.save_order(make_order())
        self.ledger.close()
        reopened = ledger_mod.Ledger(self.db_path)
        try:
            reopened.init(1_000_000)  # 已有账户时 init 不覆盖余额
            self.assertEqual(reopened.load_account().balance, 999_900)
            self.assertEqual(len(reopened.load_orders_pending()), 1)
            self.assertEqual(reopened.check_accounting(reopened.load_account(), []), [])
        finally:
            reopened.close()
        # tearDown 再 close 一次必须安全（幂等）
        self.ledger.close()


if __name__ == "__main__":
    unittest.main()
