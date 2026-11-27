"""Maisaka 上下文注入回归测试：Planner 事实 / 机器人称呼 / 不否认功能。

背景（用户实测）：
- 群友问"有没有外汇功能"，麦麦答"暂时没有外汇功能哦"——因为 Planner 不知道插件存在。
  修复方式：在 ``maisaka.planner.before_request`` 注入「外汇插件事实」。
- 实测踩坑：``items`` 是 Host 快照，必须返回 ``modified_kwargs``；元素结构必须是
  ``{"item_type": "SystemMessageItem", "parts": [{"type": "text", ...}]}``，写错会静默忽略。

本文件覆盖纯函数注入逻辑 + 称呼联动，全部离线。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE = "_maibot_plugin_github_nethielchou_mai_fx_sim_maisaka_glue"

import maisaka_glue as glue  # noqa: E402


def _system_items(extra: list | None = None) -> list:
    items = [
        {"item_type": "SystemMessageItem", "meta": {"id": 1}, "parts": [{"type": "text", "text": "系统提示"}]},
        {"item_type": "UserMessageItem", "meta": {"id": 2}, "parts": [{"type": "text", "text": "你好"}]},
    ]
    if extra:
        items.extend(extra)
    return items


class FactBlockTestCase(unittest.TestCase):
    def test_marker_present(self) -> None:
        block = glue.build_fact_block()
        self.assertIn(glue.INJECT_MARKER, block)

    def test_block_mentions_plugin_and_no_real_trading(self) -> None:
        block = glue.build_fact_block(plugin_name="麦麦外汇战士")
        self.assertIn("麦麦外汇战士", block)
        self.assertIn("虚拟", block)
        self.assertIn("不接任何真实交易账户", block)

    def test_block_lists_commands(self) -> None:
        block = glue.build_fact_block()
        for command in ("/外汇 帮助", "/外汇 状态", "/外汇 行情", "/外汇 保证金"):
            self.assertIn(command, block)

    def test_block_carries_account_facts(self) -> None:
        block = glue.build_fact_block(account_yuan=100_000, leverage=10, pair_count=8)
        self.assertIn("100,000 元", block)
        self.assertIn("10 倍杠杆", block)
        self.assertIn("8 个货币对", block)

    def test_block_omits_missing_facts(self) -> None:
        block = glue.build_fact_block()
        self.assertNotIn("倍杠杆", block)
        self.assertNotIn("个货币对", block)

    def test_block_auto_trade_state(self) -> None:
        self.assertIn("关闭", glue.build_fact_block(auto_trade=False))
        self.assertIn("已开启", glue.build_fact_block(auto_trade=True))

    def test_block_forbids_denial_and_real_broker_claims(self) -> None:
        block = glue.build_fact_block()
        self.assertIn("不要否认", block)
        self.assertIn("不要声称能连接真实券商", block)
        self.assertIn("不提供投资建议" if "不提供投资建议" in block else "投资建议", block)

    def test_block_uses_bot_name(self) -> None:
        block = glue.build_fact_block(bot_name="小夏")
        self.assertIn("小夏", block)
        self.assertNotIn("（你是麦麦）", block)

    def test_block_default_name_has_no_parenthetical(self) -> None:
        block = glue.build_fact_block(bot_name="麦麦")
        self.assertNotIn("（你是", block)

    def test_block_respects_max_chars(self) -> None:
        block = glue.build_fact_block(max_chars=120)
        self.assertLessEqual(len(block), 120)
        self.assertTrue(block.endswith("…"))

    def test_block_custom_plugin_name(self) -> None:
        block = glue.build_fact_block(plugin_name="自定义外汇")
        self.assertIn("自定义外汇", block)


class InjectFactTestCase(unittest.TestCase):
    def test_inject_appends_to_system_item(self) -> None:
        block = glue.build_fact_block()
        result = glue.inject_fact({"items": _system_items()}, block)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "continue")
        items = result["modified_kwargs"]["items"]
        system_parts = items[0]["parts"]
        self.assertEqual(len(system_parts), 2)
        self.assertEqual(system_parts[-1]["type"], "text")
        self.assertIn(glue.INJECT_MARKER, system_parts[-1]["text"])

    def test_inject_keeps_other_items_untouched(self) -> None:
        block = glue.build_fact_block()
        original = _system_items()
        result = glue.inject_fact({"items": original}, block)
        self.assertIsNotNone(result)
        items = result["modified_kwargs"]["items"]
        self.assertEqual(items[1], original[1])
        self.assertEqual(len(items), len(original))

    def test_inject_does_not_mutate_input(self) -> None:
        block = glue.build_fact_block()
        original = _system_items()
        before = len(original[0]["parts"])
        glue.inject_fact({"items": original}, block)
        self.assertEqual(len(original[0]["parts"]), before)

    def test_inject_is_idempotent(self) -> None:
        block = glue.build_fact_block()
        first = glue.inject_fact({"items": _system_items()}, block)
        self.assertIsNotNone(first)
        second = glue.inject_fact({"items": first["modified_kwargs"]["items"]}, block)
        self.assertIsNone(second)

    def test_inject_preserves_other_kwargs(self) -> None:
        block = glue.build_fact_block()
        result = glue.inject_fact({"items": _system_items(), "session_id": "s1"}, block)
        self.assertIsNotNone(result)
        self.assertEqual(result["modified_kwargs"]["session_id"], "s1")

    def test_inject_returns_none_without_items(self) -> None:
        block = glue.build_fact_block()
        self.assertIsNone(glue.inject_fact({}, block))
        self.assertIsNone(glue.inject_fact({"items": []}, block))
        self.assertIsNone(glue.inject_fact({"items": "not-a-list"}, block))
        self.assertIsNone(glue.inject_fact(None, block))

    def test_inject_returns_none_without_system_item(self) -> None:
        block = glue.build_fact_block()
        items = [{"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "hi"}]}]
        self.assertIsNone(glue.inject_fact({"items": items}, block))

    def test_inject_returns_none_for_bad_parts(self) -> None:
        block = glue.build_fact_block()
        items = [{"item_type": "SystemMessageItem", "parts": "bad"}]
        self.assertIsNone(glue.inject_fact({"items": items}, block))

    def test_inject_returns_none_for_empty_block(self) -> None:
        self.assertIsNone(glue.inject_fact({"items": _system_items()}, ""))
        self.assertIsNone(glue.inject_fact({"items": _system_items()}, "没有标记的文本"))

    def test_has_marker(self) -> None:
        block = glue.build_fact_block()
        self.assertFalse(glue.has_marker(_system_items()))
        result = glue.inject_fact({"items": _system_items()}, block)
        self.assertTrue(glue.has_marker(result["modified_kwargs"]["items"]))
        self.assertFalse(glue.has_marker("not-a-list"))
        self.assertFalse(glue.has_marker([{"item_type": "X", "parts": [{"type": "image"}]}]))


class PluginHookRegistrationTestCase(unittest.TestCase):
    """插件类上必须真实注册两个 Hook（Planner / replyer）。"""

    @classmethod
    def setUpClass(cls) -> None:
        sys.modules.pop(MODULE, None)
        spec = importlib.util.spec_from_file_location(
            MODULE,
            str(PROJECT_ROOT / "plugin.py"),
            submodule_search_locations=[str(PROJECT_ROOT)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("无法创建插件模块 spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[MODULE] = module
        spec.loader.exec_module(module)
        cls.module = module

    def test_hooks_registered(self) -> None:
        from maibot_sdk.components import collect_components

        plugin = self.module.create_plugin()
        components = collect_components(plugin)
        hooks = {
            item["metadata"].get("hook"): item["metadata"]
            for item in components
            if item.get("type") == "HOOK_HANDLER"
        }
        self.assertIn("maisaka.planner.before_request", hooks)
        self.assertIn("maisaka.replyer.before_request", hooks)
        for metadata in hooks.values():
            self.assertEqual(metadata.get("mode"), "blocking")
            self.assertEqual(metadata.get("error_policy"), "skip")

    def test_inject_fact_survives_missing_context(self) -> None:
        """上下文/配置未就绪时必须静默返回 None（不影响宿主请求）。"""

        plugin = self.module.create_plugin()
        self.assertIsNone(plugin._inject_fact({"items": _system_items()}))


if __name__ == "__main__":
    unittest.main()