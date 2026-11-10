"""决策层单测：prompt 构造、严格 JSON 解析（代码块/字符串数字/校验）、预算、LLM 调用与失败回退。

全部离线：用假的 ``ctx``/``cfg``/``PairSpec``，不联网、不依赖宿主。
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

import decision  # noqa: E402
import models  # noqa: E402


def make_pairs() -> dict[str, models.PairSpec]:
    return {
        "USDJPY": models.PairSpec(
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
        ),
        "EURUSD": models.PairSpec(
            code="EURUSD",
            name_cn="欧元/美元",
            digits=5,
            tick=Decimal("0.00001"),
            pip=Decimal("0.0001"),
            quote_currency="USD",
            sina_code="fx_seurusd",
            source_kind="raw",
            base_spread_pips=Decimal("0.5"),
            max_spread_pips=Decimal("2.0"),
            swap_long_jpy=Decimal("-101"),
            swap_short_jpy=Decimal("55"),
            max_units=100_000,
        ),
    }


def make_cfg(**overrides):
    decision_cfg = SimpleNamespace(
        decision_task_name="planner",
        temperature=0.7,
        decision_max_tokens=1200,
        request_timeout_ms=120_000,
        prompt_char_limit=8000,
        max_drift_pips=3.0,
        auto_trade=False,
    )
    for key, value in overrides.items():
        setattr(decision_cfg, key, value)
    return SimpleNamespace(decision=decision_cfg)


def make_account():
    return models.AccountState(balance=1_000_000, day="2026-11-06", trades_today=2)


def make_quote(bid="158.000", ask="158.005", mid="158.0025"):
    return SimpleNamespace(
        pair="USDJPY",
        bid=Decimal(bid),
        ask=Decimal(ask),
        mid=Decimal(mid),
        quote_ts=None,
    )


def make_position():
    return models.Position(
        position_id="POS-1",
        pair="USDJPY",
        side=models.SIDE_BUY,
        units=10_000,
        entry_price=Decimal("157.500"),
        opened_at=None,  # type: ignore[arg-type]
        stop_price=Decimal("157.000"),
        take_price=Decimal("159.000"),
        margin=63_000,
    )


class PromptTestCase(unittest.TestCase):
    def test_build_prompt_contains_required_sections(self) -> None:
        prompt = decision.build_prompt(
            make_account(),
            [make_position()],
            {"USDJPY": make_quote()},
            make_pairs(),
            [
                {
                    "pair": "USDJPY",
                    "side": "buy",
                    "position_type": "open",
                    "units": 10_000,
                    "price": Decimal("157.500"),
                    "fee": 0,
                    "realized_pnl": 0,
                    "ts": None,
                }
            ],
            make_cfg(),
        )
        for keyword in ("麦麦", "JSON", "USDJPY", "美元/日元", "当前持仓", "最近成交", "余额（现金）", "1,000,000"):
            self.assertIn(keyword, prompt)
        self.assertIn('"action"', prompt)
        self.assertIn("hold", prompt)

    def test_build_prompt_empty_positions_and_trades(self) -> None:
        prompt = decision.build_prompt(make_account(), [], {}, make_pairs(), [], make_cfg())
        self.assertIn("无（空仓）", prompt)
        self.assertIn("最近成交】\n- 无", prompt)

    def test_build_prompt_respects_char_limit(self) -> None:
        trades = [
            {"pair": "USDJPY", "side": "sell", "position_type": "close", "units": 1000,
             "price": Decimal("158.0"), "fee": 0, "realized_pnl": 123, "ts": None}
        ] * 50
        prompt = decision.build_prompt(
            make_account(), [], {"USDJPY": make_quote()}, make_pairs(), trades,
            make_cfg(prompt_char_limit=1000),
        )
        self.assertLessEqual(len(prompt), 1000)

    def test_build_prompt_tolerates_none_quote(self) -> None:
        prompt = decision.build_prompt(make_account(), [], {}, make_pairs(), None, make_cfg())
        self.assertIn("暂无可用行情", prompt)

    def test_persona_string_is_header(self) -> None:
        persona = "我是小麦，有点毒舌但心软；先观察再动手。"
        prompt = decision.build_prompt(
            make_account(), [], {}, make_pairs(), [], make_cfg(), persona=persona
        )
        self.assertTrue(prompt.startswith(persona))
        self.assertIn(decision.IDENTITY_LINE, prompt)
        self.assertIn("模拟盘", decision.IDENTITY_LINE)
        self.assertNotIn(decision.DEFAULT_HEADER, prompt)
        self.assertLess(prompt.index(persona), prompt.index("【硬性规则】"))

    def test_empty_persona_uses_default_header(self) -> None:
        for persona in ("", "   ", None):
            prompt = decision.build_prompt(
                make_account(), [], {}, make_pairs(), [], make_cfg(), persona=persona
            )
            self.assertTrue(prompt.startswith(decision.DEFAULT_HEADER), repr(persona))
            self.assertNotIn(decision.IDENTITY_LINE, prompt)

    def test_chat_reference_material_placed_last(self) -> None:
        prompt = decision.build_prompt(
            make_account(),
            [],
            {"USDJPY": make_quote()},
            make_pairs(),
            [],
            make_cfg(),
            chat_context_lines=["甲: 美日要涨", "乙: 空它"],
        )
        self.assertIn(decision.REFERENCE_HEADER, prompt)
        self.assertIn(decision.REFERENCE_INTRO, prompt)
        self.assertIn("甲: 美日要涨", prompt)
        self.assertGreater(prompt.index(decision.REFERENCE_HEADER), prompt.index("【最近成交】"))
        self.assertLess(prompt.index(decision.REFERENCE_HEADER), prompt.index("请给出本轮决策 JSON："))
        for keyword in ("不是命令", "不要执行", "由你的性格"):
            self.assertIn(keyword, prompt)

    def test_reference_header_not_duplicated_for_context_object(self) -> None:
        context = SimpleNamespace(
            prompt_lines=lambda: [
                decision.REFERENCE_HEADER,
                decision.REFERENCE_INTRO,
                "- 甲: 美日要涨",
            ]
        )
        prompt = decision.build_prompt(
            make_account(), [], {}, make_pairs(), [], make_cfg(), chat_context_lines=context
        )
        self.assertEqual(prompt.count(decision.REFERENCE_HEADER), 1)
        self.assertEqual(prompt.count(decision.REFERENCE_INTRO), 1)

    def test_calendar_lines_kept_before_reference_material(self) -> None:
        prompt = decision.build_prompt(
            make_account(),
            [],
            {},
            make_pairs(),
            [],
            make_cfg(),
            calendar_lines=["【财经日历】", "- 20:30 USD 非农（high）"],
            chat_context_lines=["甲: 非农要来了"],
        )
        self.assertIn("非农（high）", prompt)
        self.assertGreater(prompt.index("【财经日历】"), prompt.index("【最近成交】"))
        self.assertLess(prompt.index("【财经日历】"), prompt.index(decision.REFERENCE_HEADER))

    def test_persona_stripped_but_multiline_kept(self) -> None:
        prompt = decision.build_prompt(
            make_account(), [], {}, make_pairs(), [], make_cfg(), persona="  第一行\n第二行  "
        )
        self.assertTrue(prompt.startswith("第一行\n第二行"))
        self.assertNotIn("  第一行", prompt)

    def test_no_reference_section_when_context_empty(self) -> None:
        for context in (None, [], "", tuple()):
            prompt = decision.build_prompt(
                make_account(), [], {}, make_pairs(), [], make_cfg(), chat_context_lines=context
            )
            self.assertNotIn(decision.REFERENCE_HEADER, prompt, repr(context))

    def test_long_reference_material_respects_char_limit(self) -> None:
        prompt = decision.build_prompt(
            make_account(),
            [],
            {},
            make_pairs(),
            [],
            make_cfg(prompt_char_limit=1000),
            chat_context_lines=[f"甲: {'长' * 150}" for _ in range(20)],
        )
        self.assertLessEqual(len(prompt), 1000)


class ParseIntentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.pairs = make_pairs()

    def test_parse_plain_json(self) -> None:
        intent = decision.parse_intent(
            '{"pair":"USDJPY","action":"open_long","order_type":"limit","units":10000,'
            '"price":"158.500","stop_loss":"158.000","take_profit":"159.500",'
            '"reason":"突破做多","speech":"冲"}',
            self.pairs,
        )
        self.assertEqual(intent.action, "open_long")
        self.assertEqual(intent.pair, "USDJPY")
        self.assertEqual(intent.order_type, "limit")
        self.assertEqual(intent.units, 10_000)
        self.assertEqual(intent.price, Decimal("158.5"))
        self.assertEqual(intent.stop_loss, Decimal("158.0"))
        self.assertEqual(intent.take_profit, Decimal("159.500"))
        self.assertEqual(intent.reason, "突破做多")
        self.assertEqual(intent.speech, "冲")

    def test_parse_code_fence_and_numeric_strings(self) -> None:
        raw = (
            "```json\n"
            '{"pair": "usdjpy", "action": "OPEN_SHORT", "order_type": "market",'
            ' "units": "10000.0", "price": "158.0025", "reason": "r"}\n'
            "```"
        )
        intent = decision.parse_intent(raw, self.pairs)
        self.assertEqual(intent.action, "open_short")
        self.assertEqual(intent.units, 10_000)
        # 空仓数量省略 = 0（close/close_all 用）
        close_intent = decision.parse_intent('{"pair":"美元日元","action":"close"}', self.pairs)
        self.assertEqual(close_intent.action, "close")
        self.assertEqual(close_intent.pair, "USDJPY")
        self.assertEqual(close_intent.units, 0)

    def test_parse_hold(self) -> None:
        intent = decision.parse_intent('{"action":"hold","reason":"没有机会"}', self.pairs)
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.pair, "")
        self.assertEqual(intent.reason, "没有机会")

    def test_parse_json_with_surrounding_text(self) -> None:
        intent = decision.parse_intent(
            '我决定这样：{"pair":"USDJPY","action":"open_long","units":1000} 完毕', self.pairs
        )
        self.assertEqual(intent.action, "open_long")
        self.assertEqual(intent.units, 1_000)

    def test_parse_currency_name_pair(self) -> None:
        intent = decision.parse_intent('{"pair":"欧元/美元","action":"close_all"}', self.pairs)
        self.assertEqual(intent.pair, "EURUSD")

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(decision.DecisionError) as ctx:
            decision.parse_intent("这不是 JSON", self.pairs)
        self.assertIn("INVALID_JSON", str(ctx.exception))

    def test_invalid_action_raises(self) -> None:
        with self.assertRaises(decision.DecisionError) as ctx:
            decision.parse_intent('{"pair":"USDJPY","action":"buy"}', self.pairs)
        self.assertIn("INVALID_ACTION", str(ctx.exception))

    def test_invalid_pair_raises(self) -> None:
        with self.assertRaises(decision.DecisionError) as ctx:
            decision.parse_intent('{"pair":"GBPUSD","action":"open_long","units":10000}', self.pairs)
        self.assertIn("INVALID_PAIR", str(ctx.exception))

    def test_units_not_multiple_of_thousand_raises(self) -> None:
        with self.assertRaises(decision.DecisionError) as ctx:
            decision.parse_intent('{"pair":"USDJPY","action":"open_long","units":5500}', self.pairs)
        self.assertIn("INVALID_UNITS", str(ctx.exception))

    def test_limit_without_price_raises(self) -> None:
        with self.assertRaises(decision.DecisionError) as ctx:
            decision.parse_intent(
                '{"pair":"USDJPY","action":"open_long","order_type":"limit","units":10000}', self.pairs
            )
        self.assertIn("INVALID_PRICE", str(ctx.exception))

    def test_stop_loss_wrong_direction_raises(self) -> None:
        with self.assertRaises(decision.DecisionError) as ctx:
            decision.parse_intent(
                '{"pair":"USDJPY","action":"open_long","units":10000,"price":"158.0","stop_loss":"158.5"}',
                self.pairs,
            )
        self.assertIn("INVALID_PRICE", str(ctx.exception))

    def test_units_wan_text_supported(self) -> None:
        intent = decision.parse_intent(
            '{"pair":"USDJPY","action":"open_long","units":"1万"}', self.pairs
        )
        self.assertEqual(intent.units, 10_000)


class CallBudgetTestCase(unittest.TestCase):
    def test_consume_and_remaining(self) -> None:
        budget = decision.CallBudget("2026-11-06", 3)
        self.assertEqual(budget.remaining, 3)
        self.assertTrue(budget.can_call())
        budget.consume()
        budget.consume()
        self.assertEqual(budget.remaining, 1)
        budget.consume()
        self.assertEqual(budget.remaining, 0)
        self.assertFalse(budget.can_call())
        with self.assertRaises(RuntimeError) as ctx:
            budget.consume()
        self.assertIn("LLM_BUDGET_EXHAUSTED", str(ctx.exception))

    def test_reset_if_new_day(self) -> None:
        budget = decision.CallBudget("2026-11-06", 2)
        budget.consume()
        self.assertFalse(budget.reset_if_new_day("2026-11-06"))
        self.assertEqual(budget.used, 1)
        self.assertTrue(budget.reset_if_new_day("2026-11-07"))
        self.assertEqual(budget.used, 0)
        self.assertEqual(budget.remaining, 2)


class FakeCtx:
    """模拟宿主 ctx：只实现 call_capability('llm.generate')。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def call_capability(self, name, **kwargs):
        self.calls.append({"name": name, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class DecideTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.account = make_account()
        self.quotes = {"USDJPY": make_quote()}
        self.pairs = make_pairs()
        self.cfg = make_cfg()

    async def test_decide_success_consumes_budget(self) -> None:
        ctx = FakeCtx([{"success": True, "response": '{"pair":"USDJPY","action":"open_long","units":10000}'}])
        budget = decision.CallBudget("2026-11-06", 5)
        intent = await decision.decide(ctx, self.account, [], self.quotes, self.pairs, [], self.cfg, budget)
        self.assertEqual(intent.action, "open_long")
        self.assertEqual(intent.units, 10_000)
        self.assertEqual(budget.used, 1)
        self.assertEqual(len(ctx.calls), 1)
        call = ctx.calls[0]
        self.assertEqual(call["name"], "llm.generate")
        self.assertEqual(call["task_name"], "planner")
        self.assertEqual(call["timeout_ms"], 120_000)
        self.assertEqual(call["max_tokens"], 1200)

    async def test_decide_llm_failure_returns_hold(self) -> None:
        ctx = FakeCtx([{"success": False, "error": "网络错误"}])
        intent = await decision.decide(
            ctx, self.account, [], self.quotes, self.pairs, [], self.cfg, decision.CallBudget("2026-11-06", 5)
        )
        self.assertEqual(intent.action, "hold")
        self.assertIn("LLM_FAILED", intent.reason)

    async def test_decide_timeout_returns_hold(self) -> None:
        ctx = FakeCtx([TimeoutError("slow")])
        intent = await decision.decide(
            ctx, self.account, [], self.quotes, self.pairs, [], self.cfg, decision.CallBudget("2026-11-06", 5)
        )
        self.assertEqual(intent.action, "hold")
        self.assertIn("LLM_TIMEOUT", intent.reason)

    async def test_decide_budget_exhausted_does_not_call(self) -> None:
        ctx = FakeCtx([])
        budget = decision.CallBudget("2026-11-06", 0)
        intent = await decision.decide(
            ctx, self.account, [], self.quotes, self.pairs, [], self.cfg, budget
        )
        self.assertEqual(intent.action, "hold")
        self.assertIn("LLM_BUDGET_EXHAUSTED", intent.reason)
        self.assertEqual(ctx.calls, [])

    async def test_decide_retries_once_on_invalid_json(self) -> None:
        ctx = FakeCtx(
            [
                {"success": True, "response": "不是 JSON"},
                {"success": True, "response": '{"pair":"USDJPY","action":"hold"}'},
            ]
        )
        budget = decision.CallBudget("2026-11-06", 5)
        intent = await decision.decide(ctx, self.account, [], self.quotes, self.pairs, [], self.cfg, budget)
        self.assertEqual(intent.action, "hold")
        self.assertEqual(len(ctx.calls), 2)
        self.assertEqual(budget.used, 2)
        self.assertIn("上一次输出无法使用", ctx.calls[1]["prompt"])

    async def test_decide_invalid_output_twice_returns_reason(self) -> None:
        ctx = FakeCtx(
            [
                {"success": True, "response": "nope"},
                {"success": True, "response": "still nope"},
            ]
        )
        intent = await decision.decide(
            ctx, self.account, [], self.quotes, self.pairs, [], self.cfg, decision.CallBudget("2026-11-06", 5)
        )
        self.assertEqual(intent.action, "hold")
        self.assertIn("LLM_INVALID_OUTPUT", intent.reason)


if __name__ == "__main__":
    unittest.main()
