"""命令层单测：解析、权限、白名单、查询/控制子命令、补给/重置（全离线，不联网）。

命令处理只依赖 duck-type 的 services/actions，因此用假回调即可验证行为。
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import commands  # noqa: E402
import models  # noqa: E402
import report  # noqa: E402


class FakeLedger:
    """内存版账本替身（只实现命令层用到的读方法 + save_account）。"""

    def __init__(self) -> None:
        self.saved_account = None
        self.pending: list = []
        self.trades: list = []
        self.swaps: list = []

    def save_account(self, account) -> None:
        self.saved_account = account

    def load_orders_pending(self) -> list:
        return list(self.pending)

    def list_trades(self, limit: int = 50) -> list:
        return list(self.trades)[:limit]

    def list_swaps(self, limit: int = 50) -> list:
        return list(self.swaps)[:limit]


class FakeEngine:
    def __init__(self) -> None:
        self.paused = False
        self.last_chat_context = None
        self.calendar = None

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


def make_services(
    *,
    group_ids=("10001",),
    admins=("qq:20002",),
    private_enabled=False,
    account=None,
    positions=None,
    actions=None,
    engine=None,
    ledger=None,
) -> commands.CommandServices:
    cfg = SimpleNamespace(
        notify=SimpleNamespace(platform="qq", group_ids=list(group_ids), private_enabled=private_enabled),
        admin=SimpleNamespace(users=list(admins)),
        account=SimpleNamespace(initial_balance=1_000_000),
        decision=SimpleNamespace(auto_trade=False),
    )
    return commands.CommandServices(
        cfg=cfg,
        account=account if account is not None else models.AccountState(balance=1_000_000),
        positions=lambda: list(positions or []),
        quotes=lambda: {},
        pairs={},
        ledger=ledger if ledger is not None else FakeLedger(),
        engine=engine if engine is not None else FakeEngine(),
        actions=dict(actions or {}),
    )


class ParseTestCase(unittest.TestCase):
    def test_non_command_returns_none(self) -> None:
        self.assertIsNone(commands.parse_command("你好"))
        self.assertIsNone(commands.parse_command("/期货 状态"))
        self.assertIsNone(commands.parse_command(""))

    def test_default_status_when_no_args(self) -> None:
        self.assertEqual(commands.parse_command("/外汇"), ("状态", []))
        self.assertEqual(commands.parse_command("/外汇 状态"), ("状态", []))
        self.assertEqual(commands.parse_command(" /fx status "), ("状态", []))

    def test_parse_buy_tokens(self) -> None:
        parsed = commands.parse_command("/外汇 买 美元日元 1万 限价 158.5 止损 158.0")
        self.assertIsNotNone(parsed)
        sub, args = parsed
        self.assertEqual(sub, "买")
        self.assertEqual(args, ["美元日元", "1万", "限价", "158.5", "止损", "158.0"])

    def test_parse_aliases(self) -> None:
        cases = {
            "/fx 平 美元日元": ("平", ["美元日元"]),
            "/外汇 撤单 ORD-1": ("撤单", ["ORD-1"]),
            "/外汇 补给 10万": ("补给", ["10万"]),
            "/外汇 全平": ("全平", []),
            "/外汇 重置 确认": ("重置", ["确认"]),
            "/外汇 帮助": ("帮助", []),
            "/外汇 隔夜 5": ("隔夜", ["5"]),
        }
        for text, expected in cases.items():
            self.assertEqual(commands.parse_command(text), expected, text)

    def test_unknown_subcommand_passthrough(self) -> None:
        self.assertEqual(commands.parse_command("/外汇 飞行"), ("飞行", []))

    def test_persona_command_removed(self) -> None:
        # 人设命令已删除（和期货插件一致）：不再映射成「麦麦/人设」，按未知子命令处理
        for text in ("/外汇 麦麦", "/外汇 人设", "/外汇 人设 刷新", "/fx 性格"):
            parsed = commands.parse_command(text)
            self.assertIsNotNone(parsed, text)
            sub, _args = parsed
            self.assertNotIn(sub, commands.QUERY_SUBS, text)
            self.assertNotIn(sub, commands.CONTROL_SUBS, text)
        self.assertNotIn("麦麦", commands.SUB_ALIASES)
        self.assertNotIn("人设", commands.SUB_ALIASES)
        self.assertNotIn("persona", commands.SUB_ALIASES)

    def test_refresh_persona_fallback_removed(self) -> None:
        # 就算引擎上还挂着同名方法，命令层也不再调用（没有 refresh_persona 回退）
        engine = SimpleNamespace(refresh_persona=lambda: "不应被调用")
        services = make_services(engine=engine)
        self.assertIsNone(commands._action(services, "refresh_persona"))

    def test_parse_units(self) -> None:
        self.assertEqual(commands.parse_units("1万"), 10_000)
        self.assertEqual(commands.parse_units("1万单位"), 10_000)
        self.assertEqual(commands.parse_units("0.5万"), 5_000)
        self.assertEqual(commands.parse_units("10000"), 10_000)
        self.assertEqual(commands.parse_units("5,000"), 5_000)
        with self.assertRaises(commands.CommandError) as ctx:
            commands.parse_units("5500")
        self.assertIn("INVALID_UNITS", str(ctx.exception))
        with self.assertRaises(commands.CommandError):
            commands.parse_units("abc")

    def test_parse_cny_amount(self) -> None:
        self.assertEqual(commands.parse_cny_amount("10万"), 100_000)
        self.assertEqual(commands.parse_cny_amount("10万元"), 100_000)
        self.assertEqual(commands.parse_cny_amount("100000元"), 100_000)
        self.assertEqual(commands.parse_cny_amount("100000"), 100_000)
        self.assertEqual(commands.parse_cny_amount("10万人民币"), 100_000)
        with self.assertRaises(commands.CommandError) as ctx:
            commands.parse_cny_amount("0")
        self.assertIn("INVALID_AMOUNT", str(ctx.exception))


class AdminTestCase(unittest.TestCase):
    def test_admin_platform_format(self) -> None:
        self.assertTrue(commands.is_admin("20002", "qq", ["qq:20002"]))
        self.assertFalse(commands.is_admin("20002", "qq", ["qq:99999"]))
        self.assertFalse(commands.is_admin("20002", "telegram", ["qq:20002"]))
        self.assertFalse(commands.is_admin("", "qq", ["qq:20002"]))

    def test_admin_pure_digits_entry(self) -> None:
        self.assertTrue(commands.is_admin("20002", "qq", ["20002"]))
        self.assertFalse(commands.is_admin("20003", "qq", ["20002"]))

    def test_admin_user_id_with_platform(self) -> None:
        self.assertTrue(commands.is_admin("qq:20002", "qq", ["qq:20002"]))
        self.assertTrue(commands.is_admin("QQ：20002", "", ["qq:20002"]))

    def test_admin_empty_list(self) -> None:
        self.assertFalse(commands.is_admin("20002", "qq", []))
        self.assertFalse(commands.is_admin("20002", "qq", None))


class HandleTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_non_command_returns_none(self) -> None:
        result = await commands.handle(
            make_services(), platform="qq", user_id="1", group_id="10001", text="闲聊"
        )
        self.assertIsNone(result)

    async def test_group_whitelist(self) -> None:
        services = make_services()
        allowed = await commands.handle(
            services, platform="qq", user_id="1", group_id="10001", text="/外汇 状态"
        )
        self.assertIsNotNone(allowed)
        blocked = await commands.handle(
            services, platform="qq", user_id="1", group_id="99999", text="/外汇 状态"
        )
        self.assertIsNone(blocked)
        wrong_platform = await commands.handle(
            services, platform="telegram", user_id="1", group_id="10001", text="/外汇 状态"
        )
        self.assertIsNone(wrong_platform)
        private_blocked = await commands.handle(
            services, platform="qq", user_id="1", group_id=None, text="/外汇 状态"
        )
        self.assertIsNone(private_blocked)

    async def test_private_enabled(self) -> None:
        services = make_services(private_enabled=True)
        result = await commands.handle(
            services, platform="qq", user_id="1", group_id=None, text="/外汇 状态"
        )
        self.assertIsNotNone(result)

    async def test_query_available_to_everyone(self) -> None:
        services = make_services(positions=[], account=models.AccountState(balance=1_000_000))
        for query in ("/外汇 状态", "/外汇 持仓", "/外汇 挂单", "/外汇 行情", "/外汇 记录",
                      "/外汇 保证金", "/外汇 隔夜", "/外汇 帮助"):
            result = await commands.handle(
                services, platform="qq", user_id="100", group_id="10001", text=query
            )
            self.assertIsNotNone(result, query)
            self.assertIn(report.DISCLAIMER, result, query)
            self.assertIn(report.CARD_DIVIDER, result, query)

    async def test_margin_and_swap_cards(self) -> None:
        ledger = FakeLedger()
        ledger.swaps = [{"rollover_date": "2026-11-07", "pair": "USDJPY", "side": "buy",
                         "units": 10_000, "amount": 160, "days": 1}]
        services = make_services(ledger=ledger)
        margin = await commands.handle(
            services, platform="qq", user_id="1", group_id="10001", text="/外汇 保证金"
        )
        self.assertIn("保证金", margin)
        self.assertIn("强平规则", margin)
        self.assertIn("空仓", margin)
        self.assertTrue(margin.endswith(report.DISCLAIMER))
        swaps = await commands.handle(
            services, platform="qq", user_id="1", group_id="10001", text="/外汇 隔夜"
        )
        self.assertIn("隔夜利息", swaps)
        self.assertIn("+160 元", swaps)
        self.assertIn("合计", swaps)

    async def test_control_denied_for_non_admin(self) -> None:
        calls: list = []
        services = make_services(actions={"manual": lambda **kwargs: calls.append(kwargs) or (True, "ORD-1")})
        result = await commands.handle(
            services, platform="qq", user_id="100", group_id="10001",
            text="/外汇 买 美元日元 1万 限价 158.5",
        )
        self.assertIn("只有管理员", result)
        self.assertEqual(calls, [])

    async def test_buy_as_admin_submits_limit_order(self) -> None:
        calls: list = []

        def fake_manual(**kwargs):
            calls.append(kwargs)
            return True, f"ORD-{len(calls)}"

        services = make_services(
            actions={"manual": fake_manual}, account=models.AccountState(balance=1_000_000)
        )
        result = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001",
            text="/外汇 买 美元日元 1万 限价 158.5 止损 158.0",
        )
        self.assertIsNotNone(result)
        self.assertIn("已提交买入委托", result)
        self.assertIn("🛡 保护单", result)
        self.assertEqual(len(calls), 2, calls)
        entry = calls[0]
        self.assertEqual(entry["pair_text"], "美元日元")
        self.assertEqual(entry["side"], "buy")
        self.assertEqual(entry["units"], 10_000)
        self.assertEqual(entry["order_type"], "limit")
        self.assertEqual(entry["price"], Decimal("158.5"))
        protect = calls[1]
        self.assertEqual(protect["position_type"], "close")
        self.assertEqual(protect["order_type"], "stop")
        self.assertEqual(protect["price"], Decimal("158.0"))
        self.assertEqual(protect["side"], "sell")

    async def test_sell_market_order(self) -> None:
        calls: list = []
        services = make_services(actions={"manual": lambda **kwargs: calls.append(kwargs) or (True, "ORD-1")})
        result = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 卖 美元日元 1万 市价"
        )
        self.assertEqual(calls[0]["side"], "sell")
        self.assertEqual(calls[0]["order_type"], "market")
        self.assertIsNone(calls[0]["price"])
        self.assertEqual(result.count("市价"), 1, result)
        self.assertIn("已提交卖出委托", result)

    async def test_manual_failure_reported(self) -> None:
        services = make_services(actions={"manual": lambda **kwargs: (False, "INSUFFICIENT_MARGIN")})
        result = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 买 美元日元 1万"
        )
        self.assertIn("失败", result)
        self.assertIn("INSUFFICIENT_MARGIN", result)

    async def test_close_and_cancel_actions(self) -> None:
        events: list = []
        services = make_services(
            actions={
                "close": lambda pair_text: events.append(("close", pair_text)) or (True, "已平"),
                "cancel": lambda order_id: events.append(("cancel", order_id)) or (True, "已撤"),
            }
        )
        close_text = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 平 美元日元"
        )
        self.assertIn("已提交平仓委托", close_text)
        cancel_text = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 撤单 ORD-7"
        )
        self.assertIn("已撤单", cancel_text)
        self.assertEqual(events, [("close", "美元日元"), ("cancel", "ORD-7")])

    async def test_close_all_iterates_position_pairs(self) -> None:
        positions = [
            models.Position("POS-1", "USDJPY", "buy", 10_000, Decimal("158"), None),  # type: ignore[arg-type]
            models.Position("POS-2", "EURJPY", "sell", 10_000, Decimal("170"), None),  # type: ignore[arg-type]
        ]
        closed: list = []
        services = make_services(
            positions=positions,
            actions={"close": lambda pair_text: closed.append(pair_text) or (True, "ok")},
        )
        result = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 全平"
        )
        self.assertIn("全平处理完成", result)
        self.assertEqual(sorted(closed), ["EURJPY", "USDJPY"])

    async def test_pause_resume(self) -> None:
        engine = FakeEngine()
        services = make_services(engine=engine)
        await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 暂停"
        )
        self.assertTrue(engine.paused)
        await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 恢复"
        )
        self.assertFalse(engine.paused)

    async def test_reset_requires_confirm(self) -> None:
        resets: list = []
        services = make_services(actions={"reset": lambda: resets.append(1)})
        soft = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 重置"
        )
        self.assertIn("确认", soft)
        self.assertEqual(resets, [])
        hard = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 重置 确认"
        )
        self.assertIn("已重置本金", hard)
        self.assertEqual(resets, [1])

    async def test_deposit_updates_account_and_ledger(self) -> None:
        ledger = FakeLedger()
        account = models.AccountState(balance=1_000_000, initial_balance=1_000_000)
        services = make_services(account=account, ledger=ledger)
        denied = await commands.handle(
            services, platform="qq", user_id="100", group_id="10001", text="/外汇 补给 10万"
        )
        self.assertIn("只有管理员", denied)
        result = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 补给 10万"
        )
        self.assertIn("已补给虚拟资金", result)
        self.assertEqual(account.balance, 1_100_000)
        self.assertEqual(account.initial_balance, 1_100_000)
        self.assertIs(ledger.saved_account, account)
        self.assertNotIn("入金", result)
        self.assertNotIn("出金", result)

    async def test_deposit_bad_amount(self) -> None:
        services = make_services()
        result = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 补给 很多钱"
        )
        self.assertIn("参数有问题", result)
        self.assertIn("INVALID_AMOUNT", result)

    async def test_records_limit_and_bad_limit(self) -> None:
        ledger = FakeLedger()
        ledger.trades = [{"pair": "USDJPY", "position_type": "open", "side": "buy", "units": 10_000,
                          "price": Decimal("158.0"), "fee": 0, "realized_pnl": 0, "ts": None}]
        services = make_services(ledger=ledger)
        ok = await commands.handle(
            services, platform="qq", user_id="1", group_id="10001", text="/外汇 记录 1"
        )
        self.assertIn("最近成交", ok)
        bad = await commands.handle(
            services, platform="qq", user_id="1", group_id="10001", text="/外汇 记录 abc"
        )
        self.assertIn("INVALID_ARGS", bad)

    async def test_unknown_subcommand_shows_help(self) -> None:
        services = make_services()
        result = await commands.handle(
            services, platform="qq", user_id="1", group_id="10001", text="/外汇 飞行"
        )
        self.assertIn("未知子命令", result)
        self.assertIn("命令", result)

    async def test_action_exception_does_not_propagate(self) -> None:
        def boom(**kwargs):
            raise RuntimeError("引擎炸了")

        services = make_services(actions={"manual": boom})
        result = await commands.handle(
            services, platform="qq", user_id="20002", group_id="10001", text="/外汇 买 美元日元 1万"
        )
        self.assertIsNotNone(result)
        self.assertIn("命令执行出错", result)
        self.assertIn("引擎炸了", result)

    async def test_persona_commands_fall_back_to_unknown(self) -> None:
        for text in ("/外汇 麦麦", "/外汇 人设", "/外汇 人设 刷新"):
            result = await commands.handle(
                make_services(), platform="qq", user_id="20002", group_id="10001", text=text
            )
            self.assertIn("未知子命令", result, text)
            self.assertIn(report.DISCLAIMER, result, text)

    async def test_help_text_via_command(self) -> None:
        services = make_services()
        result = await commands.handle(
            services, platform="qq", user_id="1", group_id="10001", text="/外汇 帮助"
        )
        self.assertIn("补给", result)
        self.assertIn("重置", result)
        self.assertNotIn("入金", result)
        self.assertNotIn("出金", result)
        self.assertNotIn("人设", result)


if __name__ == "__main__":
    unittest.main()
