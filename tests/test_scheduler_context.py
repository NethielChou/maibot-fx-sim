"""编排器上下文单测：宿主人设字符串、群聊参考材料降级、事件窗拦截、reset（纯离线）。

用临时 SQLite 账本 + 假行情源，不联网、不部署；宿主能力通过注入的 fetcher 模拟。

口径（v0.2.1，与期货插件一致）：宿主人设是**一段字符串**，插件 on_load 读一次后原样传给
决策层；编排器不再保存 ``HostPersona`` 对象，也没有人设刷新/视图接口。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from fxconv import FxRates
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PKG = "_mai_fx_scheduler_test_pkg"


def load_package():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(
        PKG, str(PROJECT_ROOT / "plugin.py"), submodule_search_locations=[str(PROJECT_ROOT)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件包")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


def submodule(name: str):
    load_package()
    return importlib.import_module(f"{PKG}.{name}")


NOW = datetime(2026, 11, 7, 4, 30, tzinfo=timezone.utc)  # 北京时间 12:30
RATES = FxRates(usdjpy_mid=Decimal("158.000"), usdcny_mid=Decimal("6.67"))
HOST_PERSONA = "我是小麦，有点毒舌但心软；交易时先观察再动手。"


class FakeSource:
    async def fetch(self, codes, now, seq):
        return {}


def chat_message(text: str, nickname: str = "群友A", user_id: str = "123456789"):
    return {
        "processed_plain_text": text,
        "message_info": {"user_info": {"user_id": user_id, "user_nickname": nickname}},
        "raw_message": [{"type": "text", "data": text}],
    }


class EngineTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config_mod = submodule("config")
        self.ledger_mod = submodule("ledger")
        self.influence = submodule("influence")
        self.calendar_mod = submodule("econ_calendar")
        self.decision = submodule("decision")
        self.models = submodule("models")
        self.scheduler = submodule("scheduler")
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = self.ledger_mod.Ledger(Path(self.tmp.name) / "ledger.sqlite3")
        self.config = self.config_mod.MaiFxConfig()
        # 群聊参考材料默认关闭；下面需要读取群聊的用例会显式打开
        self.config.influence.enabled = True
        self.ledger.init(int(self.config.account.initial_balance))

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def make_engine(self, *, persona: str = HOST_PERSONA, fetcher=None):
        return self.scheduler.TradingEngine(
            ctx=SimpleNamespace(),
            config=self.config,
            quote_source=FakeSource(),
            ledger=self.ledger,
            persona=persona,
            chat_context_fetcher=fetcher,
        )

    def test_persona_is_plain_string(self) -> None:
        engine = self.make_engine()
        self.assertEqual(engine.persona, HOST_PERSONA)
        self.assertIsInstance(engine.persona, str)

    def test_persona_normalized_and_optional(self) -> None:
        self.assertEqual(self.make_engine(persona="  临时人设  ").persona, "临时人设")
        self.assertEqual(self.make_engine(persona="").persona, "")
        self.assertEqual(self.make_engine(persona=None).persona, "")

    def test_persona_reaches_decision_prompt(self) -> None:
        engine = self.make_engine()
        prompt = self.decision.build_prompt(
            engine.account, [], {}, engine.pairs, [], self.config, persona=engine.persona
        )
        self.assertTrue(prompt.startswith(HOST_PERSONA))
        self.assertIn(self.decision.IDENTITY_LINE, prompt)
        self.assertLess(prompt.index(HOST_PERSONA), prompt.index("【硬性规则】"))

    def test_no_persona_refresh_or_view_api(self) -> None:
        engine = self.make_engine()
        for name in ("persona_view", "set_host_persona", "host_persona", "refresh_persona"):
            self.assertFalse(hasattr(engine, name), name)

    async def test_collect_context_returns_chat_and_calendar_only(self) -> None:
        engine = self.make_engine()
        result = await engine.collect_context(NOW)
        self.assertEqual(len(result), 2)
        chat, calendar = result
        self.assertEqual(chat, [])
        self.assertEqual(calendar, [])

    async def test_chat_context_lines_included(self) -> None:
        async def fetcher(now=None):
            return self.influence.build_context(
                [chat_message("美日做多", "甲", "999888777")], limit=20, hours=1.0, chat_id="s1"
            )

        engine = self.make_engine(fetcher=fetcher)
        chat, _calendar = await engine.collect_context(NOW)
        self.assertEqual(chat[0], self.influence.PROMPT_HEADER)
        self.assertIn("甲: 美日做多", "\n".join(chat))
        self.assertNotIn("999888777", "\n".join(chat))

    async def test_chat_failure_degrades_silently(self) -> None:
        async def boom(now=None):
            raise RuntimeError("宿主炸了")

        engine = self.make_engine(fetcher=boom)
        chat, _calendar = await engine.collect_context(NOW)
        self.assertEqual(chat, [])
        self.assertTrue(engine.last_chat_context.is_empty())

    async def test_influence_disabled_skips_fetcher(self) -> None:
        called = False

        async def fetcher(now=None):
            nonlocal called
            called = True
            return self.influence.empty_context()

        self.config.influence.enabled = False
        engine = self.make_engine(fetcher=fetcher)
        chat, _calendar = await engine.collect_context(NOW)
        self.assertEqual(chat, [])
        self.assertFalse(called)
        self.assertEqual(engine.last_chat_context.note, self.influence.NOTE_DISABLED)

    async def test_calendar_lines_when_enabled(self) -> None:
        self.config.calendar.enabled = True
        self.config.calendar.source = "static"  # 离线：不触发真实数据源
        self.config.calendar.events = ["2026-11-07T12:40:00|USD|非农|high"]
        engine = self.make_engine()
        _chat, calendar = await engine.collect_context(NOW)
        self.assertIn("【财经日历】", "\n".join(calendar))
        self.assertIn("非农", "\n".join(calendar))

    async def test_event_window_blocks_new_position(self) -> None:
        self.config.calendar.enabled = True
        self.config.calendar.source = "static"
        self.config.calendar.events = ["2026-11-07T12:30:00|USD|非农|high"]
        engine = self.make_engine()
        engine.calendar = self.calendar_mod.EconomicCalendar.from_config(self.config)
        reason = engine._open_block_reason(NOW)
        self.assertIn(self.calendar_mod.EVENT_WINDOW, reason)
        self.assertIn("非农", reason)

    async def test_calendar_disabled_does_not_block(self) -> None:
        engine = self.make_engine()
        self.assertEqual(engine._open_block_reason(NOW), "")

    async def test_influence_default_is_disabled(self) -> None:
        # 默认配置（未改任何开关）不读群聊：和期货插件一致
        self.assertFalse(self.config_mod.InfluenceConfig().enabled)
        self.config.influence.enabled = self.config_mod.InfluenceConfig().enabled
        engine = self.make_engine(fetcher=None)
        chat, _calendar = await engine.collect_context(NOW)
        self.assertEqual(chat, [])
        self.assertEqual(engine.last_chat_context.note, self.influence.NOTE_DISABLED)

    async def test_maybe_decide_passes_persona_and_reference_material(self) -> None:
        self.config.decision.auto_trade = True
        engine = self.make_engine()
        captured: dict[str, object] = {}

        async def fake_decide(*args, **kwargs):
            captured.update(kwargs)
            captured["positional"] = args
            return self.models.DecisionIntent(pair="", action="hold", units=0, reason="TEST")

        original = self.decision.decide
        self.decision.decide = fake_decide
        try:
            await engine._maybe_decide(NOW, RATES)
        finally:
            self.decision.decide = original
        self.assertEqual(captured.get("persona"), HOST_PERSONA)
        self.assertIn("chat_context_lines", captured)
        self.assertIn("calendar_lines", captured)
        self.assertNotIn("host_persona_lines", captured)

    async def test_reset_clears_chat_context(self) -> None:
        async def fetcher(now=None):
            return self.influence.build_context([chat_message("在聊")])

        engine = self.make_engine(fetcher=fetcher)
        await engine.collect_context(NOW)
        self.assertFalse(engine.last_chat_context.is_empty())
        engine.reset()
        self.assertTrue(engine.last_chat_context.is_empty())


if __name__ == "__main__":
    unittest.main()
