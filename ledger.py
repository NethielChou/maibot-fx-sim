"""SQLite 虚拟账本：账户 / 持仓 / 成交 / 订单 / 订单事件 / 隔夜利息 / 权益 / 决策。

设计要点（对应 ``order-engine-spec`` §6、``margin-swap-spec`` Rev 1.1）：
- 单文件 SQLite（stdlib，无第三方依赖），``journal_mode=WAL``、``synchronous=FULL``、
  ``busy_timeout=5000``、``foreign_keys=ON``；启动时做 ``PRAGMA quick_check``。
- **金额一律整数元（int，虚拟人民币）**；价格/点数一律 TEXT（规范十进制字符串），读回即 ``Decimal``；
  时间一律 ISO8601 UTC 字符串。数据库里不出现 float，避免累积误差。
- 每个写方法自身是一个事务（``with self._conn``）；``commit_fill()`` 把
  「成交 + 持仓 + 订单 + 事件」放进**同一个事务**，供撮合层一次提交。
- 幂等：``trades.order_id`` UNIQUE（同一订单不会写两条成交）、
  ``orders.client_order_id`` UNIQUE（QQ 侧重复提交返回原单）、
  ``swaps`` 以 (日切日, 品种, 方向, 数量, 金额, 天数) 去重（重启不重复计息）。
- **账本恒等式**（``check_accounting()`` 的核心，符号约定写死如下）：

      balance = initial_balance
                + Σ trades.realized_pnl     # 平仓已实现盈亏（可正可负）
                - Σ trades.fee              # 手续费（成交时扣）
                + Σ swaps.amount            # 隔夜利息（正=收取，负=支付）

  注意：``insert_trade()`` / ``insert_swap()`` 在写明细的**同一事务**里按上式增量更新
  ``account`` 行，所以即使调度层没来得及 ``save_account()``，崩溃重启后账本仍自洽；
  ``save_account()`` 则是以内存对象为准的全量覆盖（用于日切等整状态落盘）。
- 空仓持仓不会出现在 ``positions``：部分平仓后 ``units`` 归零即删除（成交记录保留在 trades）。
- **schema 版本 2（v0.3.0，账户币种改为虚拟元）**：account/positions/trades/swaps/equity
  的金额列改成币种中性名（``balance`` / ``margin`` / ``fee`` / ``amount`` / ``equity``）。
  启动时若发现旧版（v1，``*_jpy`` 列）库，**先备份旧库文件**
  （``<库名>.v1.bak-<时间戳>``，同目录），再重建空库开户，**不做金额静默迁移**。

异常均带原因码：``INVALID_UNITS`` / ``INVALID_FILL`` / ``DUPLICATE_TRADE`` /
``DUPLICATE_CLIENT_ORDER_ID`` / ``ACCOUNT_NOT_FOUND`` / ``LEDGER_CLOSED`` /
``LEGACY_SCHEMA``。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

try:  # 宿主把 plugin.py 当包加载时走相对导入
    from .models import (
        ORDER_MARKET,
        POS_CLOSE,
        POS_OPEN,
        SIDE_BUY,
        SIDE_SELL,
        STATUS_CANCELLED,
        STATUS_CREATED,
        STATUS_PENDING,
        AccountState,
        Fill,
        Order,
        OrderRequest,
        Position,
    )
except ImportError:  # 单测/脚本直接以顶层模块导入
    from models import (  # type: ignore[no-redef]
        ORDER_MARKET,
        POS_CLOSE,
        POS_OPEN,
        SIDE_BUY,
        SIDE_SELL,
        STATUS_CANCELLED,
        STATUS_CREATED,
        STATUS_PENDING,
        AccountState,
        Fill,
        Order,
        OrderRequest,
        Position,
    )

SCHEMA_VERSION = 2
DEFAULT_INITIAL_BALANCE = 100_000

# 订单「在途」状态（重启后需要恢复的挂单）
PENDING_STATUSES = (STATUS_CREATED, STATUS_PENDING)

#: 旧版（v1）账户表里出现过的日元列名；检测到任意一个即视为旧库。
LEGACY_ACCOUNT_COLUMNS = ("balance_jpy", "initial_balance_jpy", "day_start_equity_jpy")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance INTEGER NOT NULL,
    balance INTEGER NOT NULL,
    realized_pnl INTEGER NOT NULL DEFAULT 0,
    fees_paid INTEGER NOT NULL DEFAULT 0,
    swap_paid INTEGER NOT NULL DEFAULT 0,
    day TEXT NOT NULL DEFAULT '',
    trades_today INTEGER NOT NULL DEFAULT 0,
    day_start_equity INTEGER NOT NULL,
    peak_equity INTEGER NOT NULL,
    halted INTEGER NOT NULL DEFAULT 0,
    loss_cut_pending INTEGER NOT NULL DEFAULT 0,
    loss_cut_cooldown_until TEXT,
    schema_version INTEGER NOT NULL DEFAULT 2
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    pair TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    units INTEGER NOT NULL CHECK (units > 0),
    entry_price TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    margin INTEGER NOT NULL DEFAULT 0,
    swap_accrued INTEGER NOT NULL DEFAULT 0,
    stop_price TEXT,
    take_price TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    position_type TEXT NOT NULL,
    units INTEGER NOT NULL,
    price TEXT NOT NULL,
    slippage_pips TEXT NOT NULL DEFAULT '0',
    fee INTEGER NOT NULL DEFAULT 0,
    realized_pnl INTEGER NOT NULL DEFAULT 0,
    position_id TEXT,
    decision_id TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades (ts);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT UNIQUE,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    position_type TEXT NOT NULL,
    order_type TEXT NOT NULL,
    units INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_orders_status_pair ON orders (status, pair);

CREATE TABLE IF NOT EXISTS order_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (order_id, seq)
);

CREATE TABLE IF NOT EXISTS swaps (
    swap_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rollover_date TEXT NOT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    units INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    days INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (rollover_date, pair, side, units, amount, days)
);

CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# 序列化辅助（DB 里只存 int / str，不存 float / Decimal 对象）
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """当前 UTC 时间（账本默认时间戳）。"""

    return datetime.now(timezone.utc)


def dt_to_text(value: datetime | None) -> str | None:
    """``datetime`` → ISO8601 UTC 字符串；``None`` 原样返回。"""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def text_to_dt(value: str | None) -> datetime | None:
    """ISO8601 字符串 → ``datetime``（带 UTC）；空值返回 ``None``。"""

    if value is None or str(value).strip() == "":
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def dec_to_text(value: Decimal | None) -> str | None:
    """``Decimal`` → 规范十进制字符串（不用科学计数法）。"""

    if value is None:
        return None
    return format(Decimal(value), "f")


def text_to_dec(value: str | None) -> Decimal | None:
    """十进制字符串 → ``Decimal``；空值返回 ``None``。"""

    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


class LedgerError(RuntimeError):
    """账本错误基类（消息带原因码）。"""


class DuplicateTradeError(LedgerError):
    """同一 order_id 已写入过成交（幂等保护）。"""


class DuplicateClientOrderIdError(LedgerError):
    """client_order_id 已被其它订单占用。"""


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


class Ledger:
    """单账户虚拟账本（P0：一个插件实例一个库文件）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        #: 旧版（v1）库被备份到的路径；没有发生迁移时为 ``None``。
        self.backup_path: Path | None = self._backup_legacy_database()
        self._conn: sqlite3.Connection | None = sqlite3.connect(str(self._path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------- 旧库备份（v1 → v2） ----------

    def _detect_legacy(self) -> bool:
        """只读探测现有库是否旧版（v1 日元列 / schema_version < 2）；无库/不可读返回 False。

        只有**明确**读到 v1 的日元列（或缺少 v2 的 ``balance`` 列、meta 版本 < 2）才算旧库；
        打不开/被占用/WAL 读不到元数据时**不迁移**（避免把正在使用的库误改名），
        交给正常打开流程报错或继续使用。
        """

        if not self._path.exists() or self._path.stat().st_size == 0:
            return False
        conn: sqlite3.Connection | None = None
        try:
            # 用普通连接（不是 mode=ro）：WAL 库的元数据需要 -shm，只读连接可能读不到
            conn = sqlite3.connect(str(self._path), timeout=2.0)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='account'"
            ).fetchone()
            if row is None:
                return False
            columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(account)")}
            if any(name in columns for name in LEGACY_ACCOUNT_COLUMNS):
                return True
            if "balance" not in columns:
                return True
            meta = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if meta is not None:
                try:
                    return int(str(meta[0])) < SCHEMA_VERSION
                except (TypeError, ValueError):
                    return False
            return False
        except sqlite3.DatabaseError:
            # 打不开（被占用）或损坏：不按旧库处理，避免误删/误改名正在使用的库
            return False
        finally:
            if conn is not None:
                conn.close()

    def _backup_legacy_database(self) -> Path | None:
        """旧版库 → 同目录 ``<库名>.v1.bak-<YYYYmmddTHHMMSS>``（含 -wal/-shm）。

        备份后删除原文件（连同 -wal/-shm），由调用方重建空库；返回备份路径。
        不是旧库时不做任何事，返回 ``None``。
        """

        if not self._detect_legacy():
            return None
        stamp = utc_now().strftime("%Y%m%dT%H%M%S")
        backup = self._path.with_name(f"{self._path.name}.v1.bak-{stamp}")
        suffix = 1
        while backup.exists():
            backup = self._path.with_name(f"{self._path.name}.v1.bak-{stamp}-{suffix}")
            suffix += 1
        try:
            self._path.replace(backup)
        except OSError as exc:
            raise LedgerError(
                f"LEGACY_SCHEMA_BACKUP_FAILED：旧版库无法备份（{exc}），已停止启动；"
                "请先关闭占用该文件的进程，或手动把库文件改名后再启动"
            ) from exc
        for extra in ("-wal", "-shm"):
            side = self._path.with_name(self._path.name + extra)
            if side.exists():
                try:
                    side.replace(backup.with_name(backup.name + extra))
                except OSError:
                    # 旧 WAL/SHM 无法改名时直接删除：它们属于已备份的旧库，
                    # 留着会被新库误当成自己的日志（宁可丢弃未提交尾巴，也不能冒损坏风险）。
                    try:
                        side.unlink()
                    except OSError:
                        pass
        return backup

    # ---------- 生命周期 ----------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise LedgerError("LEDGER_CLOSED：账本已关闭")
        return self._conn

    def init(self, initial_balance: int = DEFAULT_INITIAL_BALANCE) -> None:
        """建表后的初始化：确保 account 行存在（不存在则按 ``initial_balance`` 开户）。

        可重复调用；已开户时**不会**覆盖余额（改本金请用 ``reset()``）。
        """

        if not isinstance(initial_balance, int) or initial_balance < 0:
            raise LedgerError(f"INVALID_INITIAL_BALANCE：起始资金必须是 >=0 的整数元，收到 {initial_balance!r}")
        with self._tx() as conn:
            row = conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone()
            if row is None:
                self._insert_default_account(conn, int(initial_balance))
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _insert_default_account(self, conn: sqlite3.Connection, initial_balance: int) -> None:
        conn.execute(
            "INSERT INTO account (id, initial_balance, balance, realized_pnl,"
            " fees_paid, swap_paid, day, trades_today, day_start_equity,"
            " peak_equity, halted, loss_cut_pending, loss_cut_cooldown_until, schema_version)"
            " VALUES (1, ?, ?, 0, 0, 0, '', 0, ?, ?, 0, 0, NULL, ?)",
            (int(initial_balance), int(initial_balance), int(initial_balance), int(initial_balance), SCHEMA_VERSION),
        )

    def close(self) -> None:
        """关闭连接（可重复调用）。"""

        if self._conn is not None:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """单事务上下文：正常提交，异常回滚（事务内不要嵌套另一层 ``with self._conn``）。"""

        conn = self.conn
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def quick_check(self) -> list[str]:
        """``PRAGMA quick_check``；返回问题列表（空=数据库结构正常）。"""

        rows = self.conn.execute("PRAGMA quick_check").fetchall()
        results = [str(row[0]) for row in rows]
        return [] if results == ["ok"] else results

    # ---------- 账户 ----------

    def _ensure_account(self, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone() is None:
            self._insert_default_account(conn, DEFAULT_INITIAL_BALANCE)

    def load_account(self) -> AccountState:
        """读账户；未开户时用默认 10 万元自动开户（避免调度层空库崩溃）。"""

        row = self.conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            with self._tx() as conn:
                self._ensure_account(conn)
            row = self.conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        assert row is not None
        return AccountState(
            balance=int(row["balance"]),
            initial_balance=int(row["initial_balance"]),
            realized_pnl=int(row["realized_pnl"]),
            fees_paid=int(row["fees_paid"]),
            swap_paid=int(row["swap_paid"]),
            day=str(row["day"] or ""),
            trades_today=int(row["trades_today"]),
            day_start_equity=int(row["day_start_equity"]),
            peak_equity=int(row["peak_equity"]),
            halted=bool(row["halted"]),
            loss_cut_pending=bool(row["loss_cut_pending"]),
            loss_cut_cooldown_until=text_to_dt(row["loss_cut_cooldown_until"]),
            schema_version=int(row["schema_version"]),
        )

    def _save_account_row(self, conn: sqlite3.Connection, acc: AccountState) -> None:
        self._ensure_account(conn)
        conn.execute(
            "UPDATE account SET initial_balance=?, balance=?, realized_pnl=?,"
            " fees_paid=?, swap_paid=?, day=?, trades_today=?, day_start_equity=?,"
            " peak_equity=?, halted=?, loss_cut_pending=?, loss_cut_cooldown_until=?,"
            " schema_version=? WHERE id = 1",
            (
                int(acc.initial_balance),
                int(acc.balance),
                int(acc.realized_pnl),
                int(acc.fees_paid),
                int(acc.swap_paid),
                str(acc.day or ""),
                int(acc.trades_today),
                int(acc.day_start_equity),
                int(acc.peak_equity),
                1 if acc.halted else 0,
                1 if acc.loss_cut_pending else 0,
                dt_to_text(acc.loss_cut_cooldown_until),
                int(acc.schema_version or SCHEMA_VERSION),
            ),
        )

    def save_account(self, acc: AccountState) -> None:
        """以内存账户对象为准全量覆盖 account 行（日切 / 命令改本金后调用）。"""

        with self._tx() as conn:
            self._save_account_row(conn, acc)

    # ---------- 持仓 ----------

    @staticmethod
    def _position_from_row(row: sqlite3.Row) -> Position:
        return Position(
            position_id=str(row["position_id"]),
            pair=str(row["pair"]),
            side=str(row["side"]),
            units=int(row["units"]),
            entry_price=Decimal(str(row["entry_price"])),
            opened_at=text_to_dt(row["opened_at"]) or utc_now(),
            margin=int(row["margin"]),
            swap_accrued=int(row["swap_accrued"]),
            stop_price=text_to_dec(row["stop_price"]),
            take_price=text_to_dec(row["take_price"]),
        )

    def load_positions(self) -> list[Position]:
        """全部持仓（按 position_id 升序，保证确定性）。"""

        rows = self.conn.execute("SELECT * FROM positions ORDER BY position_id").fetchall()
        return [self._position_from_row(row) for row in rows]

    def load_position(self, position_id: str) -> Position | None:
        row = self.conn.execute("SELECT * FROM positions WHERE position_id = ?", (str(position_id),)).fetchone()
        return None if row is None else self._position_from_row(row)

    def _upsert_position_row(self, conn: sqlite3.Connection, pos: Position) -> None:
        if not isinstance(pos.units, int) or pos.units <= 0:
            raise LedgerError(f"INVALID_UNITS：持仓数量必须是正整数，收到 {pos.units!r}")
        if pos.side not in (SIDE_BUY, SIDE_SELL):
            raise LedgerError(f"INVALID_SIDE：持仓方向非法 {pos.side!r}")
        conn.execute(
            "INSERT INTO positions (position_id, pair, side, units, entry_price, opened_at,"
            " margin, swap_accrued, stop_price, take_price)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(position_id) DO UPDATE SET pair=excluded.pair, side=excluded.side,"
            " units=excluded.units, entry_price=excluded.entry_price, opened_at=excluded.opened_at,"
            " margin=excluded.margin, swap_accrued=excluded.swap_accrued,"
            " stop_price=excluded.stop_price, take_price=excluded.take_price",
            (
                str(pos.position_id),
                str(pos.pair),
                str(pos.side),
                int(pos.units),
                dec_to_text(pos.entry_price),
                dt_to_text(pos.opened_at),
                int(pos.margin),
                int(pos.swap_accrued),
                dec_to_text(pos.stop_price),
                dec_to_text(pos.take_price),
            ),
        )

    def upsert_position(self, pos: Position) -> None:
        """新增或更新一条持仓。"""

        with self._tx() as conn:
            self._upsert_position_row(conn, pos)

    def _delete_position_row(self, conn: sqlite3.Connection, position_id: str) -> None:
        conn.execute("DELETE FROM positions WHERE position_id = ?", (str(position_id),))

    def delete_position(self, position_id: str) -> None:
        """删除一条持仓（部分平仓后 units 归零时）。"""

        with self._tx() as conn:
            self._delete_position_row(conn, position_id)

    # ---------- 成交 ----------

    def _insert_trade_row(self, conn: sqlite3.Connection, fill: Fill) -> str:
        """写一条成交，并在同一事务内按恒等式增量更新账户。返回 trade_id（=order_id）。"""

        if not isinstance(fill.units, int) or fill.units <= 0:
            raise LedgerError(f"INVALID_UNITS：成交数量必须是正整数，收到 {fill.units!r}")
        if fill.price is None or Decimal(fill.price) <= 0:
            raise LedgerError(f"INVALID_FILL：成交价必须是正数，收到 {fill.price!r}")
        if fill.position_type not in (POS_OPEN, POS_CLOSE):
            raise LedgerError(f"INVALID_FILL：position_type 非法 {fill.position_type!r}")
        if int(fill.fee) < 0:
            raise LedgerError(f"INVALID_FILL：手续费不能为负，收到 {fill.fee!r}")
        order_id = str(fill.order_id or "").strip()
        if not order_id:
            raise LedgerError("INVALID_FILL：成交缺少 order_id")
        existing = conn.execute("SELECT trade_id FROM trades WHERE order_id = ?", (order_id,)).fetchone()
        if existing is not None:
            raise DuplicateTradeError(f"DUPLICATE_TRADE：order_id={order_id} 已入账（trade_id={existing[0]}）")

        realized = int(fill.realized_pnl)
        fee = int(fill.fee)
        trade_id = order_id
        self._ensure_account(conn)
        conn.execute(
            "INSERT INTO trades (trade_id, order_id, pair, side, position_type, units, price,"
            " slippage_pips, fee, realized_pnl, position_id, decision_id, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id,
                order_id,
                str(fill.pair),
                str(fill.side),
                str(fill.position_type),
                int(fill.units),
                dec_to_text(Decimal(fill.price)),
                dec_to_text(fill.slippage_pips),
                fee,
                realized,
                fill.position_id,
                fill.decision_id,
                dt_to_text(fill.ts) or dt_to_text(utc_now()),
            ),
        )
        # 恒等式增量：balance += realized - fee；realized += realized；fees += fee
        conn.execute(
            "UPDATE account SET balance = balance + ? - ?,"
            " realized_pnl = realized_pnl + ?,"
            " fees_paid = fees_paid + ? WHERE id = 1",
            (realized, fee, realized, fee),
        )
        return trade_id

    def insert_trade(self, fill: Fill) -> str:
        """写入一条成交（返回 trade_id = order_id；重复 order_id 抛 ``DuplicateTradeError``）。"""

        with self._tx() as conn:
            return self._insert_trade_row(conn, fill)

    def list_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近成交（新→旧）。

        返回的 dict 字段：``trade_id/order_id/pair/side/position_type/units/price(Decimal)/
        slippage_pips(Decimal)/fee(int)/realized_pnl(int)/position_id/decision_id/
        ts(datetime)``，直接可喂给 ``report.record_list`` 与 ``decision.build_prompt``。
        """

        safe_limit = max(1, min(1000, int(limit)))
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC, trade_id DESC LIMIT ?", (safe_limit,)
        ).fetchall()
        return [
            {
                "trade_id": str(row["trade_id"]),
                "order_id": str(row["order_id"]),
                "pair": str(row["pair"]),
                "side": str(row["side"]),
                "position_type": str(row["position_type"]),
                "units": int(row["units"]),
                "price": Decimal(str(row["price"])),
                "slippage_pips": text_to_dec(row["slippage_pips"]) or Decimal("0"),
                "fee": int(row["fee"]),
                "realized_pnl": int(row["realized_pnl"]),
                "position_id": row["position_id"],
                "decision_id": row["decision_id"],
                "ts": text_to_dt(row["ts"]),
            }
            for row in rows
        ]

    # ---------- 订单 ----------

    @staticmethod
    def _request_to_dict(request: OrderRequest) -> dict[str, Any]:
        return {
            "pair": request.pair,
            "side": request.side,
            "position_type": request.position_type,
            "order_type": request.order_type,
            "units": int(request.units),
            "client_order_id": request.client_order_id,
            "limit_price": dec_to_text(request.limit_price),
            "trigger_price": dec_to_text(request.trigger_price),
            "time_in_force": request.time_in_force,
            "expires_at": dt_to_text(request.expires_at),
            "position_id": request.position_id,
            "oco_group_id": request.oco_group_id,
            "decision_id": request.decision_id,
            "created_by": request.created_by,
            "reason": request.reason,
        }

    @staticmethod
    def _request_from_dict(data: dict[str, Any]) -> OrderRequest:
        return OrderRequest(
            pair=str(data.get("pair", "")),
            side=str(data.get("side", SIDE_BUY)),
            position_type=str(data.get("position_type", POS_OPEN)),
            order_type=str(data.get("order_type", ORDER_MARKET)),
            units=int(data.get("units", 0) or 0),
            client_order_id=str(data.get("client_order_id") or ""),
            limit_price=text_to_dec(data.get("limit_price")),
            trigger_price=text_to_dec(data.get("trigger_price")),
            time_in_force=str(data.get("time_in_force", "GTC")),
            expires_at=text_to_dt(data.get("expires_at")),
            position_id=data.get("position_id"),
            oco_group_id=data.get("oco_group_id"),
            decision_id=data.get("decision_id"),
            created_by=str(data.get("created_by") or ""),
            reason=str(data.get("reason") or ""),
        )

    @classmethod
    def _order_to_payload(cls, order: Order) -> str:
        payload = {
            "order": {
                "order_id": order.order_id,
                "request": cls._request_to_dict(order.request),
                "status": order.status,
                "filled_price": dec_to_text(order.filled_price),
                "filled_units": int(order.filled_units),
                "slippage_pips": dec_to_text(order.slippage_pips),
                "gap_pips": dec_to_text(order.gap_pips),
                "reject_reason": order.reject_reason,
                "suspend_reason": order.suspend_reason,
                "version": int(order.version),
                "created_at": dt_to_text(order.created_at),
                "updated_at": dt_to_text(order.updated_at),
                "trade_id": order.trade_id,
            }
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _order_from_row(cls, row: sqlite3.Row) -> Order:
        try:
            data = json.loads(str(row["payload_json"]))
            raw = data.get("order") or data
        except (json.JSONDecodeError, AttributeError) as exc:  # pragma: no cover - 防御
            raise LedgerError(f"INVALID_ORDER_PAYLOAD：order_id={row['order_id']} 无法解析（{exc}）") from exc
        order = Order(
            order_id=str(row["order_id"]),
            request=cls._request_from_dict(raw.get("request") or {}),
            status=str(row["status"]),
            filled_price=text_to_dec(raw.get("filled_price")),
            filled_units=int(raw.get("filled_units") or 0),
            slippage_pips=text_to_dec(raw.get("slippage_pips")) or Decimal("0"),
            gap_pips=text_to_dec(raw.get("gap_pips")) or Decimal("0"),
            reject_reason=str(raw.get("reject_reason") or ""),
            suspend_reason=str(raw.get("suspend_reason") or ""),
            version=int(raw.get("version") or row["version"] or 1),
            created_at=text_to_dt(raw.get("created_at")) or text_to_dt(row["created_at"]),
            updated_at=text_to_dt(raw.get("updated_at")) or text_to_dt(row["updated_at"]),
            trade_id=raw.get("trade_id"),
        )
        return order

    def _save_order_row(self, conn: sqlite3.Connection, order: Order) -> None:
        if not str(order.order_id or "").strip():
            raise LedgerError("INVALID_ORDER：订单缺少 order_id")
        client_id = str(order.request.client_order_id or "").strip() or None
        if client_id is not None:
            clash = conn.execute(
                "SELECT order_id FROM orders WHERE client_order_id = ? AND order_id <> ?",
                (client_id, str(order.order_id)),
            ).fetchone()
            if clash is not None:
                raise DuplicateClientOrderIdError(
                    f"DUPLICATE_CLIENT_ORDER_ID：client_order_id={client_id} 已被订单 {clash[0]} 占用"
                )
        created = dt_to_text(order.created_at) or dt_to_text(utc_now())
        updated = dt_to_text(order.updated_at) or created
        conn.execute(
            "INSERT INTO orders (order_id, client_order_id, pair, side, position_type, order_type,"
            " units, status, payload_json, created_at, updated_at, version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(order_id) DO UPDATE SET client_order_id=excluded.client_order_id,"
            " pair=excluded.pair, side=excluded.side, position_type=excluded.position_type,"
            " order_type=excluded.order_type, units=excluded.units, status=excluded.status,"
            " payload_json=excluded.payload_json, created_at=excluded.created_at,"
            " updated_at=excluded.updated_at, version=excluded.version",
            (
                str(order.order_id),
                client_id,
                str(order.request.pair),
                str(order.request.side),
                str(order.request.position_type),
                str(order.request.order_type),
                int(order.request.units),
                str(order.status),
                self._order_to_payload(order),
                created,
                updated,
                int(order.version),
            ),
        )

    def save_order(self, order: Order) -> None:
        """新增/更新订单（``client_order_id`` 与别的订单冲突时抛 ``DuplicateClientOrderIdError``）。"""

        with self._tx() as conn:
            self._save_order_row(conn, order)

    def load_orders_pending(self) -> list[Order]:
        """重启恢复用：状态为 created / pending 的订单（按 created_at、order_id 升序）。"""

        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY created_at, order_id",
            PENDING_STATUSES,
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def load_order(self, order_id: str) -> Order | None:
        row = self.conn.execute("SELECT * FROM orders WHERE order_id = ?", (str(order_id),)).fetchone()
        return None if row is None else self._order_from_row(row)

    def find_order_by_client_id(self, client_order_id: str) -> Order | None:
        """按 QQ 侧幂等键查订单（重复提交时返回原单）。"""

        row = self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (str(client_order_id),)
        ).fetchone()
        return None if row is None else self._order_from_row(row)

    def load_orders(self, limit: int = 20) -> list[Order]:
        """最近订单（含终态，按 updated_at 新→旧）。"""

        safe_limit = max(1, min(200, int(limit)))
        rows = self.conn.execute(
            "SELECT * FROM orders ORDER BY updated_at DESC, order_id DESC LIMIT ?", (safe_limit,)
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    # ---------- 订单事件（append-only 审计） ----------

    def _append_event_row(self, conn: sqlite3.Connection, order_id: str, event: str, detail: str) -> int:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM order_events WHERE order_id = ?", (str(order_id),)
        ).fetchone()
        seq = int(seq_row[0]) if seq_row is not None else 1
        conn.execute(
            "INSERT INTO order_events (order_id, seq, event_type, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(order_id), seq, str(event), str(detail or "")[:1000], dt_to_text(utc_now())),
        )
        return seq

    def append_order_event(self, order_id: str, event: str, detail: str = "") -> int:
        """追加一条订单事件（同一订单内 seq 从 1 递增；返回 seq）。"""

        if not str(order_id or "").strip():
            raise LedgerError("INVALID_EVENT：事件缺少 order_id")
        if not str(event or "").strip():
            raise LedgerError("INVALID_EVENT：事件缺少 event 类型")
        with self._tx() as conn:
            return self._append_event_row(conn, order_id, event, detail)

    def load_order_events(self, order_id: str) -> list[dict[str, Any]]:
        """某订单的事件流水（seq 升序）。"""

        rows = self.conn.execute(
            "SELECT seq, event_type, detail, created_at FROM order_events WHERE order_id = ? ORDER BY seq",
            (str(order_id),),
        ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "event_type": str(row["event_type"]),
                "detail": str(row["detail"]),
                "created_at": text_to_dt(row["created_at"]),
            }
            for row in rows
        ]

    # ---------- 隔夜利息 ----------

    def insert_swap(
        self,
        rollover_date: str,
        pair: str,
        side: str,
        units: int,
        amount: int,
        days: int,
    ) -> bool:
        """记一笔日切隔夜利息并同步更新账户余额（正=收取，负=支付）。

        - 同一 (日切日, 品种, 方向, 数量, 金额, 天数) 重复调用**不会重复入账**（返回 False）；
        - 入账后 ``balance += amount``、``swap_paid += amount``，
          与恒等式中的 ``+ Σ swaps.amount`` 一致。
        """

        if amount != int(amount):
            raise LedgerError(f"INVALID_SWAP：金额必须是整数元，收到 {amount!r}")
        if int(days) <= 0:
            raise LedgerError(f"INVALID_SWAP：天数必须为正，收到 {days!r}")
        if int(units) <= 0:
            raise LedgerError(f"INVALID_SWAP：数量必须为正，收到 {units!r}")
        with self._tx() as conn:
            self._ensure_account(conn)
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO swaps (rollover_date, pair, side, units, amount, days, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(rollover_date), str(pair), str(side), int(units), int(amount), int(days), dt_to_text(utc_now())),
            )
            if conn.total_changes == before:  # 幂等命中：不重复改余额
                return False
            conn.execute(
                "UPDATE account SET balance = balance + ?, swap_paid = swap_paid + ? WHERE id = 1",
                (int(amount), int(amount)),
            )
            return True

    def list_swaps(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近隔夜利息明细（新→旧）。"""

        safe_limit = max(1, min(500, int(limit)))
        rows = self.conn.execute(
            "SELECT * FROM swaps ORDER BY swap_id DESC LIMIT ?", (safe_limit,)
        ).fetchall()
        return [
            {
                "rollover_date": str(row["rollover_date"]),
                "pair": str(row["pair"]),
                "side": str(row["side"]),
                "units": int(row["units"]),
                "amount": int(row["amount"]),
                "days": int(row["days"]),
                "created_at": text_to_dt(row["created_at"]),
            }
            for row in rows
        ]

    def swaps_total(self) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(amount), 0) FROM swaps").fetchone()
        return int(row[0]) if row is not None else 0

    # ---------- 权益快照 ----------

    def save_equity(self, ts: datetime, equity: int) -> None:
        """写一条权益快照（同一时间戳覆盖，幂等）。"""

        if not isinstance(equity, int):
            raise LedgerError(f"INVALID_EQUITY：权益必须是整数元，收到 {equity!r}")
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity (ts, equity) VALUES (?, ?)",
                (dt_to_text(ts) or dt_to_text(utc_now()), int(equity)),
            )

    def list_equity(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(5000, int(limit)))
        rows = self.conn.execute(
            "SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (safe_limit,)
        ).fetchall()
        return [{"ts": text_to_dt(row["ts"]), "equity": int(row["equity"])} for row in rows]

    # ---------- 决策 ----------

    def save_decision(self, decision_id: str, source: str, payload: str, outcome: str) -> None:
        """记录一次决策（同 decision_id 覆盖；payload 为原始 LLM 文本摘要）。"""

        if not str(decision_id or "").strip():
            raise LedgerError("INVALID_DECISION：缺少 decision_id")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO decisions (decision_id, source, payload, outcome, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(decision_id) DO UPDATE SET source=excluded.source,"
                " payload=excluded.payload, outcome=excluded.outcome, created_at=excluded.created_at",
                (
                    str(decision_id),
                    str(source or "")[:64],
                    str(payload or "")[:8000],
                    str(outcome or "")[:200],
                    dt_to_text(utc_now()),
                ),
            )

    def load_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (str(decision_id),)
        ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": str(row["decision_id"]),
            "source": str(row["source"]),
            "payload": str(row["payload"]),
            "outcome": str(row["outcome"]),
            "created_at": text_to_dt(row["created_at"]),
        }

    # ---------- meta ----------

    def load_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
        return None if row is None else str(row["value"])

    def save_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (str(key), str(value)))

    def delete_meta(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (str(key),))

    def meta_keys(self, prefix: str = "") -> list[str]:
        rows = self.conn.execute(
            "SELECT key FROM meta WHERE key LIKE ? ORDER BY key", (str(prefix) + "%",)
        ).fetchall()
        return [str(row["key"]) for row in rows]

    # ---------- 复合事务：成交一条龙 ----------

    def commit_fill(
        self,
        fill: Fill,
        *,
        position: Position | None = None,
        delete_position_id: str | None = None,
        order: Order | None = None,
        event: str = "filled",
        event_detail: str = "",
        account: AccountState | None = None,
    ) -> str:
        """**同一事务**提交一次成交：成交 + 持仓增删 + 订单状态 + 订单事件（可选账户覆盖）。

        推荐给调度层使用：任一步失败整体回滚，不会出现「有成交没持仓」的半截状态。
        返回 trade_id（=order_id）。
        """

        with self._tx() as conn:
            trade_id = self._insert_trade_row(conn, fill)
            if position is not None:
                self._upsert_position_row(conn, position)
            if delete_position_id is not None:
                self._delete_position_row(conn, delete_position_id)
            if order is not None:
                self._save_order_row(conn, order)
                self._append_event_row(conn, order.order_id, event, event_detail)
            if account is not None:
                self._save_account_row(conn, account)
            return trade_id

    # ---------- 校验 ----------

    def check_accounting(
        self,
        account: AccountState | None = None,
        positions: list[Position] | None = None,
        pairs: Any = None,
        quotes: Any = None,
        rates: Any = None,
    ) -> list[str]:
        """账本一致性检查；返回不一致项列表（空列表 = 一致）。

        检查项（失败信息都以原因码开头）：
        1. ``ACCOUNT_IDENTITY_MISMATCH``：账户余额 ≠ 初始 + Σ已实现 − Σ手续费 + Σ隔夜利息；
        2. ``ACCOUNT_FIELD_MISMATCH``：内存账户的 realized/fees/swap 与明细汇总不符
           （或与 DB account 行不符）；
        3. ``POSITION_ROW_MISMATCH``：传入的持仓与 DB 持仓不一致（数量/价格/集合）；
        4. ``POSITION_INVALID``：持仓行本身非法（units<=0/非 1000 的整数倍/价格非正）；
        5. ``TRADE_INVALID``：成交行非法（手续费为负等）。

        说明：浮动盈亏/维持率属于 ``accounting.py`` 的估值口径（含 JPY/USD → 元换算），
        不在账本层重复实现，因此 ``pairs/quotes/rates`` 目前只用于签名兼容与将来扩展。
        """

        del pairs, quotes, rates
        issues: list[str] = []
        db_account = self.load_account()
        trade_row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl,"
            " COALESCE(SUM(fee), 0) AS fee, COUNT(*) AS cnt FROM trades"
        ).fetchone()
        assert trade_row is not None
        sum_realized = int(trade_row["pnl"])
        sum_fees = int(trade_row["fee"])
        trade_count = int(trade_row["cnt"])
        sum_swaps = self.swaps_total()

        expected_balance = int(db_account.initial_balance) + sum_realized - sum_fees + sum_swaps
        if int(db_account.balance) != expected_balance:
            issues.append(
                "ACCOUNT_IDENTITY_MISMATCH：DB 余额 {} ≠ 初始 {} + 已实现 {} − 手续费 {} + 隔夜利息 {} = {}".format(
                    db_account.balance,
                    db_account.initial_balance,
                    sum_realized,
                    sum_fees,
                    sum_swaps,
                    expected_balance,
                )
            )
        if int(db_account.realized_pnl) != sum_realized:
            issues.append(
                f"ACCOUNT_FIELD_MISMATCH：realized_pnl={db_account.realized_pnl} ≠ Σtrades={sum_realized}"
            )
        if int(db_account.fees_paid) != sum_fees:
            issues.append(f"ACCOUNT_FIELD_MISMATCH：fees_paid={db_account.fees_paid} ≠ Σfees={sum_fees}")
        if int(db_account.swap_paid) != sum_swaps:
            issues.append(f"ACCOUNT_FIELD_MISMATCH：swap_paid={db_account.swap_paid} ≠ Σswaps={sum_swaps}")

        if account is not None:
            if int(account.balance) != expected_balance:
                issues.append(
                    f"ACCOUNT_IDENTITY_MISMATCH：内存余额 {account.balance} ≠ 恒等式 {expected_balance}"
                )
            if int(account.realized_pnl) != sum_realized:
                issues.append(
                    f"ACCOUNT_FIELD_MISMATCH：内存 realized_pnl={account.realized_pnl} ≠ Σtrades={sum_realized}"
                )
            if int(account.fees_paid) != sum_fees:
                issues.append(
                    f"ACCOUNT_FIELD_MISMATCH：内存 fees_paid={account.fees_paid} ≠ Σfees={sum_fees}"
                )
            if int(account.swap_paid) != sum_swaps:
                issues.append(
                    f"ACCOUNT_FIELD_MISMATCH：内存 swap_paid={account.swap_paid} ≠ Σswaps={sum_swaps}"
                )

        db_positions = {pos.position_id: pos for pos in self.load_positions()}
        for pos in db_positions.values():
            if not isinstance(pos.units, int) or pos.units <= 0:
                issues.append(f"POSITION_INVALID：{pos.position_id} units={pos.units}")
            elif pos.units % 1_000 != 0:
                issues.append(f"POSITION_INVALID：{pos.position_id} units={pos.units} 不是 1,000 的整数倍")
            if pos.entry_price <= 0:
                issues.append(f"POSITION_INVALID：{pos.position_id} entry_price={pos.entry_price}")
            if pos.side not in (SIDE_BUY, SIDE_SELL):
                issues.append(f"POSITION_INVALID：{pos.position_id} side={pos.side!r}")
        if trade_count >= 0:
            bad_fee = self.conn.execute("SELECT COUNT(*) FROM trades WHERE fee < 0").fetchone()
            if bad_fee is not None and int(bad_fee[0]) > 0:
                issues.append(f"TRADE_INVALID：存在 {int(bad_fee[0])} 条负手续费成交")
            bad_units = self.conn.execute("SELECT COUNT(*) FROM trades WHERE units <= 0").fetchone()
            if bad_units is not None and int(bad_units[0]) > 0:
                issues.append(f"TRADE_INVALID：存在 {int(bad_units[0])} 条非正数量成交")

        if positions is not None:
            passed = {pos.position_id: pos for pos in positions}
            for position_id in sorted(set(db_positions) | set(passed)):
                db_pos = db_positions.get(position_id)
                mem_pos = passed.get(position_id)
                if db_pos is None or mem_pos is None:
                    issues.append(f"POSITION_ROW_MISMATCH：{position_id} 仅存在于{'DB' if db_pos else '内存'}")
                    continue
                if db_pos.units != mem_pos.units or db_pos.pair != mem_pos.pair or db_pos.side != mem_pos.side:
                    issues.append(
                        f"POSITION_ROW_MISMATCH：{position_id} DB({db_pos.pair},{db_pos.side},{db_pos.units})"
                        f" ≠ 内存({mem_pos.pair},{mem_pos.side},{mem_pos.units})"
                    )
                elif db_pos.entry_price != mem_pos.entry_price:
                    issues.append(
                        f"POSITION_ROW_MISMATCH：{position_id} 开仓价 DB={db_pos.entry_price} ≠ 内存={mem_pos.entry_price}"
                    )
        return issues

    # ---------- 重置 ----------

    def reset(self, initial_balance: int) -> None:
        """清空账本并重新开户（管理员「重置本金」用；旧成交/持仓/挂单/事件全部作废）。"""

        if not isinstance(initial_balance, int) or initial_balance < 0:
            raise LedgerError(f"INVALID_INITIAL_BALANCE：起始资金必须是 >=0 的整数元，收到 {initial_balance!r}")
        with self._tx() as conn:
            for table in ("positions", "trades", "orders", "order_events", "swaps", "equity", "decisions", "meta"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM account")
            self._insert_default_account(conn, int(initial_balance))
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
