"""播报层单测：免责声明、金额格式、卡片式排版、命令/事件播报（纯离线）。"""

from __future__ import annotations

import re
import sys
import unicodedata
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fxconv import FxRates  # noqa: E402
import models  # noqa: E402
import report  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
RATES = FxRates(usdjpy_mid=Decimal("158.0025"), usdcny_mid=Decimal("6.67"))
TITLE_STAMP_RE = re.compile(r"^.+ · \d{2}-\d{2} \d{2}:\d{2}$")
KANA_RE = re.compile(r"[\u3040-\u30ff]")


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
        )
    }


def make_quote(bid="158.000", ask="158.005", mid="158.0025", pair="USDJPY") -> models.QuoteSnapshot:
    return models.QuoteSnapshot(
        pair=pair,
        bid=Decimal(bid),
        ask=Decimal(ask),
        mid=Decimal(mid),
        quote_ts=NOW,
        recv_ts=NOW,
    )


def make_reference_quote(pair: str, bid: str, ask: str) -> models.QuoteSnapshot:
    """人民币参考汇率快照（只展示、不可交易）。"""

    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return models.QuoteSnapshot(
        pair=pair,
        bid=bid_d,
        ask=ask_d,
        mid=(bid_d + ask_d) / 2,
        quote_ts=NOW,
        recv_ts=NOW,
    )


def reference_quotes() -> dict[str, models.QuoteSnapshot]:
    """8 个人民币参考对（价格照 2026-11-10 实测样例，日元为 ×100 前的原始价）。"""

    raw = {
        "USDCNY": ("6.6708", "6.6709"),
        "EURCNY": ("7.4847", "7.4870"),
        "JPYCNY": ("0.042210", "0.042210"),
        "GBPCNY": ("8.7400", "8.7420"),
        "AUDCNY": ("4.7950", "4.7980"),
        "HKDCNY": ("0.85700", "0.85720"),
        "CADCNY": ("5.1350", "5.1370"),
        "NZDCNY": ("4.4380", "4.4410"),
    }
    return {code: make_reference_quote(code, bid, ask) for code, (bid, ask) in raw.items()}


def make_position(*, side="buy", units=10_000, entry="158.000", margin=2_668, pid="POS-1"):
    return models.Position(
        position_id=pid,
        pair="USDJPY",
        side=side,
        units=units,
        entry_price=Decimal(entry),
        opened_at=NOW,
        margin=margin,
        stop_price=Decimal("157.000"),
        take_price=None,
    )


def make_order(order_id="ORD-1", *, status="pending", units=10_000) -> models.Order:
    request = models.OrderRequest(
        pair="USDJPY",
        side="buy",
        position_type="open",
        order_type="limit",
        units=units,
        limit_price=Decimal("158.000"),
    )
    return models.Order(order_id=order_id, request=request, status=status, created_at=NOW, updated_at=NOW)


def make_fill(order_id="ORD-1", *, position_type="open", side="buy", realized=0, fee=0) -> models.Fill:
    return models.Fill(
        order_id=order_id,
        pair="USDJPY",
        side=side,
        position_type=position_type,
        units=10_000,
        price=Decimal("158.005"),
        slippage_pips=Decimal("0.2"),
        fee=fee,
        realized_pnl=realized,
        position_id="POS-1",
        ts=NOW,
    )


def all_cards() -> dict[str, str]:
    """所有用户可见播报（用于卡片排版一致性检查）。"""

    pairs = make_pairs()
    quotes = {"USDJPY": make_quote(), **reference_quotes()}
    account = models.AccountState(balance=1_000_000, realized_pnl=1_000, fees_paid=100, swap_paid=-210)
    position = make_position()
    order = make_order()
    trade = {
        "pair": "USDJPY",
        "position_type": "close",
        "side": "sell",
        "units": 10_000,
        "price": Decimal("158.005"),
        "fee": 0,
        "realized_pnl": 1_000,
        "ts": NOW,
    }
    event = SimpleNamespace(
        currency="USD", name="非农", impact="high", impact_text="高",
        at=datetime(2026, 11, 7, 20, 30, tzinfo=timezone.utc),
    )
    return {
        "行情": report.quotes_overview(quotes, pairs),
        "行情无参考": report.quotes_overview(quotes, pairs, cny_reference=False),
        "行情空": report.quotes_overview({}, pairs),
        "状态": report.status(
            account, [position], quotes, pairs, RATES,
            pending=[order], market_open=True, auto_trade=False, paused=False,
        ),
        "状态空": report.status(account, [], {}, pairs, None, market_open=False, paused=True),
        "持仓": report.positions_list([position], quotes, pairs, RATES),
        "持仓空": report.positions_list([], {}, pairs, None),
        "挂单": report.pending_list([order]),
        "挂单空": report.pending_list([]),
        "保证金": report.margin_overview(account, [position], quotes, pairs, RATES),
        "保证金空": report.margin_overview(account, [], quotes, pairs, RATES),
        "隔夜": report.swap_list(
            [{"rollover_date": "2026-11-07", "pair": "USDJPY", "side": "buy",
              "units": 10_000, "amount": 160, "days": 1}],
            pairs,
        ),
        "隔夜空": report.swap_list([], pairs),
        "记录": report.record_list([trade]),
        "记录空": report.record_list([]),
        "帮助": report.help_text(),
        "成交开": report.fill_report(make_fill(fee=100), pairs["USDJPY"]),
        "成交平": report.fill_report(
            make_fill(position_type="close", side="sell", realized=1_000), pairs["USDJPY"]
        ),
        "受理": report.order_submitted(order),
        "拒单": report.order_rejected("ORD-2", "INSUFFICIENT_MARGIN"),
        "挂起": report.order_suspended("ORD-3", "QUOTE_STALE"),
        "决策开多": report.decision_report(
            models.DecisionIntent(
                pair="USDJPY", action="open_long", order_type="market", units=10_000,
                stop_loss=Decimal("157.700"), take_profit=Decimal("158.700"),
                reason="USD/JPY 当前呈现明确上涨趋势，价格站稳短期均线，动能仍在放大。",
                speech="美元兑日元趋势向上，果断做多一波！",
            )
        ),
        "决策观望": report.decision_report(
            models.DecisionIntent(pair="", action="hold", reason="没有符合条件的机会", speech="再等等")
        ),
        "强平": report.loss_cut_report(
            [make_fill(position_type="close", side="sell", realized=-5_000)], account, 900_000, []
        ),
        "隔夜入账": report.swap_report(160, 1),
        "汇率不可用": report.rate_unavailable_text(),
        "日历": report.event_soon_text(event),
        "通知": report.card_notice(
            "✅ 已提交买入委托",
            ["品种　美元日元", "数量　10,000 货币", "委托　限价 158.5"],
            footer="📝 成交后会单独发成交播报",
        ),
    }


class FormatTestCase(unittest.TestCase):
    def test_disclaimer_constant_and_append(self) -> None:
        self.assertEqual(report.DISCLAIMER, "【纯本地虚拟游戏】模拟盘，不构成投资建议")
        text = report.append_disclaimer("正文")
        self.assertTrue(text.endswith(report.DISCLAIMER))
        self.assertEqual(report.append_disclaimer(text), text, "已含声明时不重复追加")
        self.assertEqual(report.append_disclaimer(""), report.DISCLAIMER)

    def test_fmt_money(self) -> None:
        self.assertEqual(report.fmt_money(1_000_000), "1,000,000 元")
        self.assertEqual(report.fmt_money(0), "0 元")
        self.assertEqual(report.fmt_money(-123_456), "-123,456 元")
        self.assertEqual(report.fmt_signed_money(1_000), "+1,000 元")
        self.assertEqual(report.fmt_signed_money(-210), "-210 元")
        self.assertEqual(report.fmt_price(Decimal("158.005")), "158.005")
        self.assertEqual(report.fmt_price(None), "-")

    def test_all_public_outputs_end_with_disclaimer(self) -> None:
        for name, output in all_cards().items():
            self.assertIsInstance(output, str, name)
            self.assertTrue(output.endswith(report.DISCLAIMER), name)
            self.assertEqual(output.count(report.DISCLAIMER), 1, name)


class CardLayoutTestCase(unittest.TestCase):
    """卡片式排版硬约束：标题行/空行/分隔线/行宽/emoji/纯文本。"""

    def test_divider_constant(self) -> None:
        self.assertEqual(report.CARD_DIVIDER, "─" * 13)
        self.assertEqual(len(report.CARD_DIVIDER), 13)

    def test_title_line_and_blank_second_line(self) -> None:
        for name, text in all_cards().items():
            lines = text.splitlines()
            first = lines[0]
            self.assertRegex(first, TITLE_STAMP_RE, name)
            self.assertEqual(unicodedata.category(first[0])[0], "S", f"{name} 标题行需要 emoji：{first!r}")
            self.assertEqual(lines[1], "", f"{name} 标题下需要空一行")

    def test_divider_separates_sections(self) -> None:
        for name, text in all_cards().items():
            if name.startswith("决策"):
                continue  # 决策卡片按参考样例用空行分区（末尾空行 + 声明）
            self.assertIn(report.CARD_DIVIDER, text, name)
            divider_lines = [line for line in text.splitlines() if line == report.CARD_DIVIDER]
            self.assertTrue(divider_lines, name)
            for line in divider_lines:
                self.assertEqual(line, report.CARD_DIVIDER)

    def test_every_line_within_card_width(self) -> None:
        for name, text in all_cards().items():
            for line in text.splitlines():
                width = report.display_width(line)
                self.assertLessEqual(width, report.CARD_WIDTH, f"{name}: {width} 列：{line!r}")

    def test_no_markdown_table_or_code_block(self) -> None:
        for name, text in all_cards().items():
            self.assertNotIn("```", text, name)
            self.assertNotIn("|", text, name)
            for line in text.splitlines():
                self.assertEqual(line, line.rstrip(), f"{name} 行尾不能有空白：{line!r}")

    def test_no_japanese_kana(self) -> None:
        for name, text in all_cards().items():
            self.assertIsNone(KANA_RE.search(text), f"{name} 出现日文假名：{text}")

    def test_empty_states_use_empty_box_emoji(self) -> None:
        for name in ("持仓空", "挂单空", "记录空", "隔夜空", "行情空"):
            self.assertIn("📭", all_cards()[name], name)
        self.assertIn("📭", report.margin_overview(models.AccountState(), [], {}, make_pairs(), None))

    def test_decision_market_does_not_repeat_price_text(self) -> None:
        market = report.decision_report(
            models.DecisionIntent(
                pair="USDJPY", action="open_long", order_type="market", units=10_000,
                stop_loss=Decimal("157.000"), take_profit=Decimal("158.000"),
                reason="突破", speech="上车",
            )
        )
        self.assertEqual(market.count("市价"), 1, market)
        self.assertIn("数量  10,000 单位｜市价", market)
        self.assertIn("止损  157.000｜止盈  158.000", market)
        limit = report.decision_report(
            models.DecisionIntent(
                pair="USDJPY", action="open_long", order_type="limit", units=10_000,
                price=Decimal("157.500"), reason="回踩",
            )
        )
        self.assertNotIn("市价", limit)
        self.assertIn("限价 157.500", limit)

    def test_display_width(self) -> None:
        self.assertEqual(report.display_width("abc"), 3)
        self.assertEqual(report.display_width("中文"), 4)
        self.assertEqual(report.display_width("中文abc"), 7)


class OverviewTestCase(unittest.TestCase):
    def test_status_computes_equity_margin_ratio(self) -> None:
        pairs = make_pairs()
        account = models.AccountState(balance=1_000_000)
        position = make_position(entry="158.000")
        text = report.status(account, [position], {"USDJPY": make_quote(bid="158.000")}, pairs, RATES)
        self.assertIn("账户净值", text)
        self.assertIn("1,000,000 元", text)
        self.assertIn("现金余额", text)
        self.assertIn("占用保证金", text)
        self.assertIn("2,668 元", text)
        self.assertIn("37481.26%", text)
        self.assertIn("持仓 1 笔", text)
        self.assertIn("📌 持仓明细", text)

    def test_status_empty_positions(self) -> None:
        text = report.status(
            models.AccountState(balance=1_000_000), [], {}, make_pairs(), RATES, market_open=False, paused=True
        )
        self.assertIn("持仓 0 笔", text)
        self.assertIn("市场休市", text)
        self.assertIn("已暂停", text)
        self.assertIn("保证金维持率　—", text)

    def test_status_without_rates_warns(self) -> None:
        text = report.status(models.AccountState(balance=1_000_000), [], {}, make_pairs(), None)
        self.assertIn("汇率不可用", text)
        self.assertIn("绝不用假汇率", text)

    def test_positions_list_long_uses_bid(self) -> None:
        text = report.positions_list(
            [make_position(entry="158.000")], {"USDJPY": make_quote(bid="158.100")}, make_pairs(), RATES
        )
        self.assertIn("美元/日元", text)
        self.assertIn("多", text)
        self.assertIn("1万单位", text)
        self.assertIn("浮动 +42 元", text)
        self.assertIn("开仓 158.000", text)
        self.assertIn("保证金 2,668 元", text)

    def test_positions_list_short_uses_ask(self) -> None:
        position = make_position(side="sell", entry="158.000")
        text = report.positions_list(
            [position], {"USDJPY": make_quote(ask="157.900")}, make_pairs(), RATES
        )
        self.assertIn("空", text)
        self.assertIn("浮动 +42 元", text)

    def test_positions_list_empty(self) -> None:
        self.assertIn("空仓", report.positions_list([], {}, make_pairs(), None))

    def test_positions_list_missing_quote(self) -> None:
        text = report.positions_list([make_position()], {}, make_pairs(), RATES)
        self.assertIn("行情不可用", text)
        no_rate = report.positions_list([make_position()], {"USDJPY": make_quote()}, make_pairs(), None)
        self.assertIn("汇率不可用", no_rate)

    def test_pending_list(self) -> None:
        text = report.pending_list([make_order("ORD-7")])
        self.assertIn("ORD-7", text)
        self.assertIn("挂单中", text)
        self.assertIn("158.000", text)
        self.assertIn("没有在途挂单", report.pending_list([]))

    def test_quotes_overview_one_line_per_pair(self) -> None:
        text = report.quotes_overview({"USDJPY": make_quote()}, make_pairs())
        self.assertIn("🇺🇸🇯🇵 美元/日元", text)
        self.assertIn("158.000 / 158.005", text)
        self.assertIn("点差 0.5", text)
        self.assertNotIn("中 158", text, "卡片不再逐项重复中间价")
        self.assertIn("暂无行情", report.quotes_overview({}, make_pairs()))
        self.assertIn("点差", text)
        # 计算对的长尾价格按品种位数收敛（默认 3 位）
        long_quote = make_quote(bid="212.4508100142", ask="212.4508100142")
        rounded = report.quotes_overview({"GBPJPY": long_quote}, make_pairs())
        self.assertIn("212.451", rounded)
        self.assertNotIn("212.4508100142", rounded)

    def test_quotes_overview_subtitle_and_sections(self) -> None:
        import pairs as pairs_mod

        specs = pairs_mod.pairs_from_config([])
        quotes = {code: make_quote(pair=code) for code in pairs_mod.DEFAULT_PAIR_CODES}
        quotes.update(reference_quotes())
        text = report.quotes_overview(quotes, specs)
        self.assertIn("可交易 8 个品种 + 人民币参考汇率", text)
        self.assertEqual(text.count("【可交易】"), 1)
        self.assertEqual(text.count("【人民币参考汇率】"), 1)
        self.assertIn("（仅参考，暂不可交易）", text)
        self.assertIn(report.CARD_DIVIDER, text)

    def test_quotes_overview_cny_reference_rows(self) -> None:
        quotes = {"USDJPY": make_quote(), **reference_quotes()}
        text = report.quotes_overview(quotes, make_pairs())
        self.assertIn("🇺🇸🇨🇳 美元/人民币", text)
        self.assertIn("6.6708 / 6.6709", text, "参考汇率保留 4 位小数")
        self.assertIn("点差 0.1", text)
        self.assertIn("🇪🇺🇨🇳 欧元/人民币", text)
        self.assertIn("7.4847 / 7.4870", text)
        self.assertIn("点差 2.3", text)
        self.assertIn("🇭🇰🇨🇳 港元/人民币", text)
        self.assertIn("0.8570 / 0.8572", text)
        self.assertIn("🇳🇿🇨🇳 新西兰元/人民币", text)
        self.assertIn("4 位小数", text)

    def test_quotes_overview_jpy_reference_uses_100_yen(self) -> None:
        quotes = reference_quotes()
        text = report.quotes_overview(quotes, make_pairs())
        self.assertIn("🇯🇵🇨🇳 100日元 = 4.2210 / 4.2210 元", text)
        self.assertNotIn("0.0422", text, "日元不能显示成 0.0422 这种难读数字")
        self.assertNotIn("日元/人民币", text, "日元按 100 日元显示，不再用原始报价")

    def test_quotes_overview_reference_spread_hidden_when_zero(self) -> None:
        # 日元样例买卖价相等 → 点差 0，不显示「点差 0.0」噪音
        text = report.quotes_overview(reference_quotes(), make_pairs())
        self.assertNotIn("点差 0.0", text)
        noisy = reference_quotes()
        noisy["JPYCNY"] = make_reference_quote("JPYCNY", "0.042210", "0.042220")
        spread_text = report.quotes_overview(noisy, make_pairs())
        self.assertIn("100日元 = 4.2210 / 4.2220 元", spread_text)
        self.assertIn("点差 1.0", spread_text)

    def test_quotes_overview_reference_not_in_tradable_section(self) -> None:
        """参考对只在「人民币参考汇率」栏；可交易栏里一个都不能出现。"""
        quotes = {"USDJPY": make_quote(), **reference_quotes()}
        text = report.quotes_overview(quotes, make_pairs())
        tradable_part = text.split("【人民币参考汇率】")[0]
        for name in ("美元/人民币", "欧元/人民币", "100日元", "港元/人民币", "新西兰元/人民币"):
            self.assertNotIn(name, tradable_part, name)
        self.assertNotIn("USDCNY", text, "卡片里只显示中文名，不暴露代码")
        # USDCNY 是换算锚：出现在参考栏、不在可交易栏
        reference_part = text.split("【人民币参考汇率】")[1]
        self.assertIn("美元/人民币", reference_part)

    def test_quotes_overview_hides_reference_when_disabled(self) -> None:
        quotes = {"USDJPY": make_quote(), **reference_quotes()}
        text = report.quotes_overview(quotes, make_pairs(), cny_reference=False)
        self.assertNotIn("人民币参考汇率", text)
        self.assertNotIn("美元/人民币", text)
        self.assertNotIn("100日元", text)
        self.assertIn("🇺🇸🇯🇵 美元/日元", text, "关闭参考栏后仍显示可交易品种")
        self.assertIn("可交易 1 个品种", text)
        self.assertNotIn("+ 人民币参考汇率", text)

    def test_quotes_overview_howto_explains_spread(self) -> None:
        text = report.quotes_overview({"USDJPY": make_quote()}, make_pairs())
        self.assertIn("📖 怎么看", text)
        for line in report.HOWTO_LINES:
            self.assertIn(line, text)
        self.assertIn("买入价 = 你买入要付的价格", text)
        self.assertIn("卖出价 = 你卖出能拿到的价格", text)
        self.assertIn("点差 = 买入价与卖出价的差，越小越便宜", text)
        self.assertIn("例：美元/日元 158.030 / 158.040", text)
        self.assertIn("点差单位 pip", text)
        # 旧的歧义文案已清掉
        self.assertNotIn("买价", text)
        self.assertNotIn("卖价", text)

    def test_quotes_overview_no_duplicate_rows(self) -> None:
        quotes = {"USDJPY": make_quote(), **reference_quotes()}
        text = report.quotes_overview(quotes, make_pairs())
        lines = [
            line for line in text.splitlines() if line.strip() and line != report.CARD_DIVIDER
        ]
        self.assertEqual(len(lines), len(set(lines)), "卡片里不能有重复行")
        self.assertEqual(text.count("📖 怎么看"), 1)
        self.assertEqual(text.count("美元/人民币"), 1)
        self.assertEqual(text.count("100日元"), 1)
        self.assertEqual(text.count(report.CARD_DIVIDER), 2, "参考栏与说明栏各一条分隔线")

    def test_quotes_overview_reference_only_card(self) -> None:
        text = report.quotes_overview(reference_quotes(), make_pairs())
        self.assertIn("可交易暂无行情", text)
        self.assertIn("📭", text)
        self.assertIn("美元/人民币", text)

    def test_record_list(self) -> None:
        trades = [
            {
                "pair": "USDJPY",
                "position_type": "close",
                "side": "sell",
                "units": 10_000,
                "price": Decimal("158.005"),
                "fee": 0,
                "realized_pnl": 1_000,
                "ts": NOW,
            }
        ]
        text = report.record_list(trades)
        self.assertIn("平仓卖", text)
        self.assertIn("+1,000 元", text)
        self.assertIn("共 1 笔", text)
        self.assertIn("还没有成交记录", report.record_list([]))

    def test_margin_overview_positions_and_empty(self) -> None:
        pairs = make_pairs()
        account = models.AccountState(balance=1_000_000)
        text = report.margin_overview(
            account, [make_position()], {"USDJPY": make_quote()}, pairs, RATES, leverage=10
        )
        self.assertIn("保证金", text)
        self.assertIn("账户净值", text)
        self.assertIn("占用保证金", text)
        self.assertIn("持仓占用", text)
        self.assertIn("所需保证金 2,668 元", text)
        self.assertIn("强平规则", text)
        empty = report.margin_overview(account, [], {}, pairs, None)
        self.assertIn("空仓", empty)
        self.assertIn("无保证金占用", empty)

    def test_swap_list(self) -> None:
        text = report.swap_list(
            [{"rollover_date": "2026-11-07", "pair": "USDJPY", "side": "buy",
              "units": 10_000, "amount": 160, "days": 1}],
            make_pairs(),
        )
        self.assertIn("2026-11-07", text)
        self.assertIn("美元/日元", text)
        self.assertIn("+160 元", text)
        self.assertIn("1 日分", text)
        self.assertIn("合计", text)
        self.assertIn("还没有隔夜利息记录", report.swap_list([], make_pairs()))

    def test_help_text_has_subcommands_without_deposit_words(self) -> None:
        text = report.help_text()
        for keyword in ("状态", "持仓", "挂单", "行情", "记录", "保证金", "隔夜", "买", "卖", "平", "撤单", "补给", "重置", "帮助"):
            self.assertIn(keyword, text)
        self.assertNotIn("入金", text)
        self.assertNotIn("出金", text)
        # 人设命令已删除：帮助里不再出现「人设」相关条目
        self.assertNotIn("人设", text)
        self.assertNotIn("/外汇 麦麦", text)

    def test_help_text_grouped(self) -> None:
        text = report.help_text()
        for group in ("查询", "交易", "管理", "仅管理员"):
            self.assertIn(group, text)
        self.assertGreaterEqual(text.count(report.CARD_DIVIDER), 3, "各分组之间要有分隔线")


class EventReportTestCase(unittest.TestCase):
    def test_fill_report_open_and_close(self) -> None:
        open_text = report.fill_report(make_fill(fee=100), make_pairs()["USDJPY"])
        self.assertIn("新规买", open_text)
        self.assertIn("成交 1万单位 @ 158.005", open_text)
        self.assertIn("手续费 100 元", open_text)
        close_text = report.fill_report(
            make_fill(position_type="close", side="sell", realized=1_000), make_pairs()["USDJPY"]
        )
        self.assertIn("平仓卖", close_text)
        self.assertIn("已实现盈亏 +1,000 元", close_text)

    def test_swap_report_sign(self) -> None:
        positive = report.swap_report(160, 1)
        self.assertIn("+160 元", positive)
        self.assertIn("收取", positive)
        negative = report.swap_report(-210, 3)
        self.assertIn("-210 元", negative)
        self.assertIn("支付", negative)
        self.assertIn("3 个日切", negative)

    def test_loss_cut_report(self) -> None:
        fills = [make_fill("LIQ-1", position_type="close", side="sell", realized=-5_000)]
        text = report.loss_cut_report(fills, models.AccountState(balance=500_000), 500_000, [])
        self.assertIn("强制平仓", text)
        self.assertIn("-5,000 元", text)
        self.assertIn("强平后账户净值 500,000 元", text)
        self.assertIn("剩余持仓 0 笔", text)

    def test_event_soon_text(self) -> None:
        # 财经日历播报保留（人设播报已删除）
        event = SimpleNamespace(
            currency="USD", name="非农", impact="high", impact_text="高",
            at=datetime(2026, 11, 7, 20, 30, tzinfo=timezone.utc),
        )
        text = report.event_soon_text(event)
        self.assertIn("即将公布", text)
        self.assertIn("USD 非农", text)
        self.assertIn("高影响", text)
        self.assertIn("11-07 20:30", text)
        self.assertIn("事件窗内麦麦不新开仓", text)
        self.assertTrue(text.endswith(report.DISCLAIMER))

    def test_decision_report_hold_and_open(self) -> None:
        hold = report.decision_report(
            models.DecisionIntent(pair="", action="hold", reason="没有机会", speech="再等等")
        )
        self.assertIn("观望", hold)
        self.assertIn("没有机会", hold)
        self.assertIn("✨ 麦麦说：再等等", hold)
        opened = report.decision_report(
            models.DecisionIntent(
                pair="USDJPY",
                action="open_long",
                order_type="limit",
                units=10_000,
                price=Decimal("158.500"),
                stop_loss=Decimal("158.000"),
                take_profit=Decimal("159.500"),
                reason="突破",
                speech="上车",
            )
        )
        self.assertIn("开多", opened)
        self.assertIn("美元/日元", opened)
        self.assertIn("10,000 单位", opened)
        self.assertIn("限价 158.500", opened)
        self.assertIn("止损  158.000", opened)
        self.assertIn("止盈  159.500", opened)
        self.assertIn("💬 理由", opened)

    def test_order_submitted_and_rejected_reasons(self) -> None:
        submitted = report.order_submitted(make_order("ORD-1"))
        self.assertIn("订单已受理", submitted)
        self.assertIn("ORD-1", submitted)
        self.assertIn("挂单中", submitted)
        self.assertIn("美元/日元", submitted)
        rejected = report.order_rejected("ORD-2", "INSUFFICIENT_MARGIN：需要 63,202 日元")
        self.assertIn("保证金不足", rejected)
        self.assertIn("ORD-2", rejected)
        self.assertIn("💬 原因", rejected)
        suspended = report.order_suspended("", "QUOTE_STALE")
        self.assertIn("行情过期", suspended)
        self.assertIn("挂起", suspended)
        unknown = report.order_rejected("ORD-3", "SOME_NEW_CODE")
        self.assertIn("SOME_NEW_CODE", unknown)

    def test_card_notice_layout(self) -> None:
        text = report.card_notice("✅ 已提交买入委托", ["品种　美元日元"], footer="📝 备注")
        lines = text.splitlines()
        self.assertRegex(lines[0], TITLE_STAMP_RE)
        self.assertEqual(lines[1], "")
        self.assertIn("品种　美元日元", lines)
        self.assertIn(report.CARD_DIVIDER, lines)
        self.assertIn("📝 备注", lines)
        self.assertEqual(lines[-1], report.DISCLAIMER)
        no_stamp = report.card_notice("🔒 仅管理员可用", ["这个命令只有管理员能用"], stamp=False)
        self.assertTrue(no_stamp.startswith("🔒 仅管理员可用\n\n"), no_stamp)


if __name__ == "__main__":
    unittest.main()
