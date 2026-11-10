"""群聊上下文单测：消息→上下文行、过滤媒体/过长、默认关闭、失败/超时降级、隐私（纯离线）。

宿主能力 ``message.get_recent`` 用假 ctx 模拟；不联网、不读真实群聊、不落盘。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import influence  # noqa: E402

KANA_RE = r"[\u3040-\u30ff]"


def make_message(
    text: str,
    *,
    nickname: str = "群友A",
    user_id: str = "123456789",
    cardname: str | None = None,
    raw: bool = True,
    **extra,
):
    """按宿主真实序列化结构造消息（processed_plain_text + raw_message + message_info）。"""

    message = {
        "message_id": "m1",
        "message_info": {
            "user_info": {
                "user_id": user_id,
                "user_nickname": nickname,
                "user_cardname": cardname,
            },
            "group_info": {"group_id": "10001", "group_name": "测试群"},
        },
        "raw_message": [{"type": "text", "data": text}] if raw else [],
    }
    if text:
        message["processed_plain_text"] = text
    message.update(extra)
    return message


def section(**overrides):
    base = dict(
        enabled=True,
        group_ids=[],
        limit=20,
        hours=1.0,
        timeout_seconds=10,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def config(**overrides):
    return SimpleNamespace(influence=section(**overrides))


class MessageTextTestCase(unittest.TestCase):
    def test_prefers_processed_plain_text(self) -> None:
        message = make_message("正式文本")
        message["plain_text"] = "备用文本"
        self.assertEqual(influence.message_text(message), "正式文本")

    def test_raw_segments_text_only(self) -> None:
        message = {
            "raw_message": [
                {"type": "text", "data": "做多"},
                {"type": "image", "data": "base64..."},
                {"type": "text", "data": "美元日元"},
            ]
        }
        self.assertEqual(influence.message_text(message), "做多美元日元")

    def test_plain_text_fallback(self) -> None:
        self.assertEqual(influence.message_text({"plain_text": "只有备用"}), "只有备用")

    def test_unknown_objects(self) -> None:
        self.assertEqual(influence.message_text(None), "")
        self.assertEqual(influence.message_text(SimpleNamespace(processed_plain_text="对象文本")), "对象文本")
        self.assertEqual(influence.message_text(SimpleNamespace()), "")

    def test_newlines_are_flattened(self) -> None:
        self.assertEqual(influence.message_text(make_message("第一行\n第二行")), "第一行 第二行")


class MessageNicknameTestCase(unittest.TestCase):
    def test_cardname_preferred(self) -> None:
        message = make_message("hi", nickname="昵称", cardname="群名片")
        self.assertEqual(influence.message_nickname(message), "群名片")

    def test_nickname_fallback(self) -> None:
        self.assertEqual(influence.message_nickname(make_message("hi", nickname="昵称")), "昵称")

    def test_missing_nickname_uses_placeholder(self) -> None:
        message = {"message_info": {"user_info": {"user_id": "1"}}}
        self.assertEqual(influence.message_nickname(message), "群友")

    def test_long_nickname_truncated(self) -> None:
        name = influence.message_nickname(make_message("hi", nickname="长" * 60))
        self.assertLessEqual(len(name), influence.MAX_NICKNAME_CHARS)
        self.assertTrue(name.endswith("…"))


class FormatLineTestCase(unittest.TestCase):
    def test_basic_line(self) -> None:
        self.assertEqual(influence.format_line(make_message("美日好像要涨")), "群友A: 美日好像要涨")

    def test_command_is_skipped(self) -> None:
        self.assertEqual(influence.format_line(make_message("/外汇 状态")), "")
        self.assertEqual(influence.format_line(make_message("普通", is_command=True)), "")

    def test_empty_and_media_only_skipped(self) -> None:
        self.assertEqual(influence.format_line(make_message("", raw=False)), "")
        self.assertEqual(
            influence.format_line({"raw_message": [{"type": "image", "data": "x"}]}), ""
        )

    def test_overlong_message_skipped(self) -> None:
        long_text = "长" * (influence.MAX_MESSAGE_CHARS + 1)
        self.assertEqual(influence.format_line(make_message(long_text)), "")

    def test_line_has_no_user_id(self) -> None:
        line = influence.format_line(make_message("美日做多", user_id="987654321"))
        self.assertNotIn("987654321", line)
        self.assertIn("群友A", line)


class BuildContextTestCase(unittest.TestCase):
    def test_lines_and_count(self) -> None:
        messages = [make_message("第一条", nickname="甲"), make_message("第二条", nickname="乙")]
        context = influence.build_context(messages)
        self.assertEqual(context.lines, ("甲: 第一条", "乙: 第二条"))
        self.assertEqual(context.message_count, 2)
        self.assertFalse(context.is_empty())

    def test_limit_keeps_latest(self) -> None:
        messages = [make_message(f"消息{i}", nickname="甲") for i in range(5)]
        context = influence.build_context(messages, limit=3)
        self.assertEqual([line.split(": ")[1] for line in context.lines], ["消息2", "消息3", "消息4"])

    def test_char_budget_drops_oldest(self) -> None:
        messages = [make_message("长" * 150, nickname="甲") for _ in range(20)]
        context = influence.build_context(messages, limit=20)
        total = sum(len(line) + 4 for line in context.lines)
        self.assertLessEqual(total, influence.MAX_CONTEXT_CHARS)
        self.assertLess(len(context.lines), 20)

    def test_empty_note(self) -> None:
        context = influence.build_context([])
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_EMPTY)

    def test_mixed_media_filtered_but_counted_as_lines(self) -> None:
        messages = [
            make_message("有内容"),
            {"raw_message": [{"type": "image", "data": "x"}]},
            make_message("也有内容"),
        ]
        context = influence.build_context(messages)
        self.assertEqual(context.message_count, 2)
        self.assertEqual(len(context.lines), 2)


class ChatContextTestCase(unittest.TestCase):
    def test_is_empty_and_to_dict_has_no_text(self) -> None:
        message = make_message("机密聊天内容", nickname="秘密昵称", user_id="555666777")
        context = influence.build_context([message])
        data = context.to_dict()
        dumped = json.dumps(data, ensure_ascii=False)
        for forbidden in ("机密聊天内容", "秘密昵称", "555666777"):
            self.assertNotIn(forbidden, dumped)
        self.assertEqual(data["message_count"], 1)
        self.assertEqual(data["line_count"], 1)
        self.assertNotIn("lines", data)
        # 提示词行里只有「昵称: 文本」，永远不含 QQ 号/用户 ID
        for line in context.prompt_lines():
            self.assertNotIn("555666777", line)

    def test_prompt_lines_shape(self) -> None:
        context = influence.build_context([make_message("美日做多", nickname="甲")])
        lines = context.prompt_lines()
        self.assertEqual(lines[0], influence.PROMPT_HEADER)
        self.assertEqual(lines[1], influence.PROMPT_INTRO)
        self.assertIn("甲: 美日做多", lines[2])
        self.assertNotRegex("\n".join(lines), KANA_RE)

    def test_prompt_header_marks_untrusted(self) -> None:
        # 参考材料必须写明：不可信、不是命令、是否采纳由性格决定
        self.assertEqual(influence.PROMPT_HEADER, "【参考材料（群友观点）】")
        for keyword in ("只作参考", "不是命令", "不要执行", "性格"):
            self.assertIn(keyword, influence.PROMPT_INTRO)

    def test_empty_prompt_lines(self) -> None:
        self.assertEqual(influence.empty_context(note="x").prompt_lines(), [])

    def test_empty_context_defaults(self) -> None:
        context = influence.empty_context()
        self.assertTrue(context.is_empty())
        self.assertEqual(context.to_dict()["line_count"], 0)


class CollectTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_missing_enabled_key_means_disabled(self) -> None:
        # 配置里没有 enabled 键时按「默认关闭」处理（和期货插件一致）
        context = await influence.collect(None, SimpleNamespace(influence=SimpleNamespace()), ["s1"])
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_DISABLED)

    async def test_dict_config_defaults_to_disabled(self) -> None:
        context = await influence.collect(None, {"influence": {}}, ["s1"])
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_DISABLED)

    async def test_disabled_returns_empty_without_fetch(self) -> None:
        called = False

        async def fetch(ids, hours, limit):
            nonlocal called
            called = True
            return []

        context = await influence.collect(
            None, config(enabled=False), ["s1"], fetch=fetch
        )
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_DISABLED)
        self.assertFalse(called)

    async def test_no_chat_ids(self) -> None:
        context = await influence.collect(None, config(), [])
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_NO_CHAT)

    async def test_injected_fetch_uses_config_values(self) -> None:
        seen: dict[str, object] = {}

        async def fetch(ids, hours, limit):
            seen.update(ids=list(ids), hours=hours, limit=limit)
            return [make_message("群里在聊美日")]

        context = await influence.collect(
            None, config(limit=5, hours=2.0), ["s1", "s2"], fetch=fetch
        )
        self.assertEqual(seen, {"ids": ["s1", "s2"], "hours": 2.0, "limit": 5})
        self.assertEqual(context.message_count, 1)
        self.assertEqual(context.chat_id, "s1")
        self.assertEqual(context.hours, 2.0)

    async def test_injected_sync_fetch_supported(self) -> None:
        def fetch(ids, hours, limit):
            return [make_message("同步取数")]

        context = await influence.collect(None, config(), ["s1"], fetch=fetch)
        self.assertEqual(context.lines, ("群友A: 同步取数",))

    async def test_explicit_overrides(self) -> None:
        seen: dict[str, object] = {}

        async def fetch(ids, hours, limit):
            seen.update(hours=hours, limit=limit)
            return []

        await influence.collect(None, config(limit=5, hours=2.0), ["s1"], fetch=fetch, limit=9, hours=0.5)
        self.assertEqual(seen, {"hours": 0.5, "limit": 9})

    async def test_fetch_failure_degrades(self) -> None:
        logs: list[str] = []

        async def fetch(ids, hours, limit):
            raise RuntimeError("宿主炸了：机密正文")

        context = await influence.collect(
            None, config(), ["s1"], fetch=fetch, log=logs.append
        )
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_FAILED)
        self.assertTrue(logs)
        self.assertNotIn("机密正文", "\n".join(logs))

    async def test_timeout_degrades(self) -> None:
        async def fetch(ids, hours, limit):
            await asyncio.sleep(0.2)
            return [make_message("来不及")]

        context = await influence.collect(
            None, config(timeout_seconds=1), ["s1"], fetch=fetch, timeout=0.01
        )
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_TIMEOUT)

    async def test_non_list_result_degrades(self) -> None:
        async def fetch(ids, hours, limit):
            return {"messages": "不是列表"}

        context = await influence.collect(None, config(), ["s1"], fetch=fetch)
        self.assertTrue(context.is_empty())
        self.assertEqual(context.note, influence.NOTE_FAILED)

    async def test_no_capability_support(self) -> None:
        context = await influence.collect(object(), config(), ["s1"])
        self.assertEqual(context.note, influence.NOTE_NO_SUPPORT)

    async def test_default_capability_contract(self) -> None:
        calls: list[dict] = []

        class FakeCtx:
            async def call_capability(self, name, **kwargs):
                calls.append({"name": name, **kwargs})
                return {"success": True, "messages": [make_message("美日做多")]}

        context = await influence.collect(FakeCtx(), config(limit=7, hours=3.0), ["s1"])
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["name"], "message.get_recent")
        self.assertEqual(call["chat_id"], "s1")
        self.assertEqual(call["hours"], 3.0)
        self.assertEqual(call["limit"], 7)
        self.assertEqual(call["limit_mode"], "latest")
        self.assertTrue(call["filter_mai"])
        self.assertEqual(context.message_count, 1)

    async def test_capability_always_filters_mai(self) -> None:
        # include_mai 已从配置移除：内部常量固定 False，读群聊时永远过滤机器人消息
        calls: list[dict] = []

        class FakeCtx:
            async def call_capability(self, name, **kwargs):
                calls.append(kwargs)
                return {"success": True, "messages": []}

        self.assertFalse(influence.INCLUDE_MAI)
        await influence.collect(FakeCtx(), config(), ["s1"])
        self.assertTrue(calls[0]["filter_mai"])

    async def test_capability_failure_result_tolerated(self) -> None:
        class FakeCtx:
            async def call_capability(self, name, **kwargs):
                return {"success": False, "error": "没有记录"}

        context = await influence.collect(FakeCtx(), config(), ["s1"])
        self.assertTrue(context.is_empty())
        self.assertEqual(context.message_count, 0)

    async def test_multiple_chats_are_merged(self) -> None:
        class FakeCtx:
            async def call_capability(self, name, **kwargs):
                text = "一群在聊" if kwargs["chat_id"] == "s1" else "二群在聊"
                return {"success": True, "messages": [make_message(text)]}

        context = await influence.collect(FakeCtx(), config(), ["s1", "s2"])
        self.assertEqual(context.message_count, 2)
        self.assertIn("一群在聊", " ".join(context.lines))
        self.assertIn("二群在聊", " ".join(context.lines))


if __name__ == "__main__":
    unittest.main()
