"""宿主人设读取单测：``plugin._load_persona`` 的 str/dict/异常/空回退（纯离线）。

口径（v0.2.1，和期货插件一致）：插件只在 on_load 时读一次
``config.get(key="personality.personality")``，返回值可能是字符串，也可能是
``{"success": True, "value": ...}`` 字典；读不到 / 宿主异常 / 字段为空 → 空串，
不阻塞加载、不伪造性格，也不往日志里写人设正文。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "_maibot_plugin_github_nethielchou_mai_fx_sim_persona_load"


def load_plugin_module():
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


class FakeLogger:
    """记录日志调用（插件只应记录字数，不应记录人设正文）。"""

    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message, *args) -> None:
        self.info_messages.append(str(message) % args if args else str(message))

    def warning(self, message, *args) -> None:
        self.warning_messages.append(str(message) % args if args else str(message))

    def debug(self, message, *args) -> None:  # pragma: no cover - 插件未使用
        pass


class FakeCtx:
    """假宿主 ctx：只实现 ``call_capability`` 与 ``logger``。"""

    def __init__(self, result=None, *, raises: Exception | None = None) -> None:
        self.logger = FakeLogger()
        self.calls: list[dict] = []
        self._result = result
        self._raises = raises

    async def call_capability(self, name: str, **kwargs):
        self.calls.append({"name": name, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._result


def make_plugin(ctx) -> tuple[object, object]:
    plugin = _MODULE.create_plugin()
    plugin._ctx = ctx
    return _MODULE, plugin


# 在导入期（事件循环之外）加载一次插件包，避免把宿主 SDK 的导入耗时算进异步用例。
_MODULE = load_plugin_module()


class LoadPersonaTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_reads_plain_string(self) -> None:
        ctx = FakeCtx("  我是小麦，有点毒舌。  ")
        _module, plugin = make_plugin(ctx)
        self.assertEqual(await plugin._load_persona(), "我是小麦，有点毒舌。")

    async def test_reads_dict_value(self) -> None:
        ctx = FakeCtx({"success": True, "value": "  冷静，先观察  "})
        _module, plugin = make_plugin(ctx)
        self.assertEqual(await plugin._load_persona(), "冷静，先观察")

    async def test_failed_dict_returns_empty(self) -> None:
        ctx = FakeCtx({"success": False, "error": "没有权限", "value": "不该用"})
        _module, plugin = make_plugin(ctx)
        self.assertEqual(await plugin._load_persona(), "")

    async def test_dict_without_value_returns_empty(self) -> None:
        _module, plugin = make_plugin(FakeCtx({}))
        self.assertEqual(await plugin._load_persona(), "")

    async def test_unexpected_type_returns_empty(self) -> None:
        for result in (None, [], 42):
            _module, plugin = make_plugin(FakeCtx(result))
            self.assertEqual(await plugin._load_persona(), "", repr(result))

    async def test_empty_and_whitespace_return_empty(self) -> None:
        for result in ("", "   ", "\n\t"):
            _module, plugin = make_plugin(FakeCtx(result))
            self.assertEqual(await plugin._load_persona(), "", repr(result))

    async def test_exception_does_not_raise(self) -> None:
        ctx = FakeCtx(raises=RuntimeError("宿主炸了"))
        _module, plugin = make_plugin(ctx)
        self.assertEqual(await plugin._load_persona(), "")
        self.assertTrue(ctx.logger.warning_messages)
        self.assertIn("读取宿主人设失败", ctx.logger.warning_messages[-1])

    async def test_timeout_does_not_raise(self) -> None:
        ctx = FakeCtx(raises=asyncio.TimeoutError())
        _module, plugin = make_plugin(ctx)
        self.assertEqual(await plugin._load_persona(), "")

    async def test_multiline_persona_stripped_only_at_edges(self) -> None:
        ctx = FakeCtx("\n第一行\n第二行\n")
        _module, plugin = make_plugin(ctx)
        self.assertEqual(await plugin._load_persona(), "第一行\n第二行")

    async def test_capability_contract(self) -> None:
        ctx = FakeCtx("人设")
        _module, plugin = make_plugin(ctx)
        await plugin._load_persona()
        self.assertEqual(len(ctx.calls), 1)
        call = ctx.calls[0]
        self.assertEqual(call["name"], "config.get")
        self.assertEqual(call["key"], "personality.personality")
        self.assertEqual(call["default"], "")

    async def test_logs_length_but_not_text(self) -> None:
        secret = "机密性格设定：嘴硬心软"
        ctx = FakeCtx(secret)
        _module, plugin = make_plugin(ctx)
        await plugin._load_persona()
        joined = "\n".join(ctx.logger.info_messages + ctx.logger.warning_messages)
        self.assertIn(f"已读取宿主人设（{len(secret)} 字）", joined)
        self.assertNotIn(secret, joined)

    def test_plugin_version_is_034(self) -> None:
        self.assertEqual(_MODULE.PLUGIN_VERSION, "0.3.4")


if __name__ == "__main__":
    unittest.main()
