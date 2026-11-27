"""机器人称呼（宿主机 ``bot.nickname``）注入测试。

每个麦麦可能叫不同名字，播报与决策提示词里的自称必须跟着宿主昵称走，
读不到时回退「麦麦」，不能写死。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import decision
import report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "_maibot_plugin_github_nethielchou_mai_fx_sim_bot_name"


def load_plugin_module():
    """按宿主 plugin_loader 的方式把 plugin.py 作为包加载（相对导入可用）。"""

    sys.modules.pop(MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PROJECT_ROOT / "plugin.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建插件模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def _pairs() -> dict:
    return {
        "USDJPY": SimpleNamespace(
            code="USDJPY",
            name_cn="美元/日元",
            digits=3,
            pip=Decimal("0.01"),
            quote_currency="JPY",
            base_spread_pips=Decimal("1.0"),
        )
    }


def _quote() -> SimpleNamespace:
    return SimpleNamespace(
        pair="USDJPY",
        bid=Decimal("158.000"),
        ask=Decimal("158.010"),
        mid=Decimal("158.005"),
        quote_ts=NOW,
        recv_ts=NOW,
    )


def _account() -> SimpleNamespace:
    return SimpleNamespace(
        balance=100_000,
        initial_balance=100_000,
        realized_pnl=0,
        fees_paid=0,
        swap_paid=0,
        day_start_equity=100_000,
        peak_equity=100_000,
        trades_today=0,
        halted=False,
        loss_cut_pending=False,
        day="2026-11-27",
    )


def _intent() -> SimpleNamespace:
    return SimpleNamespace(
        action="open_long",
        pair="USDJPY",
        order_type="market",
        units=10_000,
        limit_price=None,
        trigger_price=None,
        stop_loss=Decimal("157.000"),
        take_profit=None,
        reason="趋势向上",
        speech="做多一波",
    )


class BotNameTestCase(unittest.TestCase):
    def setUp(self) -> None:
        report.set_bot_name("麦麦")

    def tearDown(self) -> None:
        report.set_bot_name("麦麦")

    def test_default_name(self) -> None:
        self.assertEqual(report.bot_name(), "麦麦")

    def test_set_name(self) -> None:
        report.set_bot_name("小夏")
        self.assertEqual(report.bot_name(), "小夏")

    def test_blank_name_falls_back(self) -> None:
        report.set_bot_name("   ")
        self.assertEqual(report.bot_name(), "麦麦")
        report.set_bot_name("")
        self.assertEqual(report.bot_name(), "麦麦")
        report.set_bot_name(None)  # type: ignore[arg-type]
        self.assertEqual(report.bot_name(), "麦麦")

    def test_name_is_stripped(self) -> None:
        report.set_bot_name("  小夏  ")
        self.assertEqual(report.bot_name(), "小夏")

    def test_decision_report_uses_name(self) -> None:
        report.set_bot_name("小夏")
        text = report.decision_report(_intent())
        self.assertIn("小夏的决定", text)
        self.assertIn("小夏说：", text)
        self.assertNotIn("麦麦的决定", text)

    def test_status_uses_name(self) -> None:
        report.set_bot_name("小夏")
        text = report.status(
            _account(), [], {}, _pairs(), auto_trade=False, paused=False
        )
        self.assertIn("小夏", text)
        self.assertIn("外汇账户概览", text)


class BuildPromptNameTestCase(unittest.TestCase):
    def _build(self, bot_name: str) -> str:
        return decision.build_prompt(
            _account(),
            [],
            {"USDJPY": _quote()},
            _pairs(),
            [],
            None,
            bot_name=bot_name,
        )

    def test_default_prompt_uses_bot_name(self) -> None:
        text = self._build("小夏")
        self.assertIn("你是「小夏」", text)
        self.assertNotIn("你是「麦麦」", text)

    def test_persona_branch_uses_bot_name(self) -> None:
        text = decision.build_prompt(
            _account(),
            [],
            {"USDJPY": _quote()},
            _pairs(),
            [],
            None,
            persona="我是小夏，性格谨慎。",
            bot_name="小夏",
        )
        self.assertIn("我是小夏", text)
        self.assertIn("你是小夏，同时在用虚拟账户炒外汇", text)

    def test_default_name_when_omitted(self) -> None:
        text = decision.build_prompt(
            _account(), [], {"USDJPY": _quote()}, _pairs(), [], None
        )
        self.assertIn("你是「麦麦」", text)

    def test_blank_name_falls_back(self) -> None:
        text = decision.build_prompt(
            _account(), [], {"USDJPY": _quote()}, _pairs(), [], None, bot_name="  "
        )
        self.assertIn("你是「麦麦」", text)


class PluginLoadBotNameTestCase(unittest.IsolatedAsyncioTestCase):
    """``MaiFxSimPlugin._load_bot_name`` 的读取与降级。"""

    async def _load(self, response=None, *, raises: Exception | None = None) -> str:
        module = load_plugin_module()
        MaiFxSimPlugin = module.MaiFxSimPlugin

        class _Ctx:
            class logger:
                @staticmethod
                def warning(*_args, **_kwargs):
                    return None

            async def call_capability(self, capability, **kwargs):
                if raises is not None:
                    raise raises
                return response

        plugin = MaiFxSimPlugin.__new__(MaiFxSimPlugin)
        plugin._ctx = _Ctx()
        return await plugin._load_bot_name()

    async def test_string_response(self) -> None:
        self.assertEqual(await self._load("小夏"), "小夏")

    async def test_dict_response(self) -> None:
        self.assertEqual(
            await self._load({"success": True, "value": "小夏"}), "小夏"
        )

    async def test_dict_success_false(self) -> None:
        self.assertEqual(
            await self._load({"success": False, "value": "小夏"}), "麦麦"
        )

    async def test_empty_response_falls_back(self) -> None:
        self.assertEqual(await self._load(""), "麦麦")
        self.assertEqual(await self._load("   "), "麦麦")
        self.assertEqual(await self._load(None), "麦麦")

    async def test_exception_falls_back(self) -> None:
        self.assertEqual(
            await self._load(raises=RuntimeError("capability unavailable")), "麦麦"
        )

    async def test_name_is_stripped(self) -> None:
        self.assertEqual(await self._load("  小夏  "), "小夏")


if __name__ == "__main__":
    unittest.main()