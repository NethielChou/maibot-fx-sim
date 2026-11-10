"""配置页单测：中文页签/分组、字段标题与提示、隐藏/只读标记、输入不被修改（纯离线）。

用与 ``config.py`` 同构的假 Schema（不 import maibot_sdk / config.py），
保证单测在任何环境都能跑。
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config_ui  # noqa: E402

FIELDS: dict[str, list[str]] = {
    "plugin": ["enabled", "config_version"],
    "account": [
        "currency", "initial_balance", "leverage", "unit_contracts", "min_units",
        "commission_per_10k", "allow_hedging", "loss_cut_ratio", "margin_call_ratio",
        "liquidation_mode", "swap_enabled",
    ],
    "market": [
        "source", "pairs", "quote_poll_seconds", "quote_timeout_seconds",
        "stale_quote_seconds", "quote_jitter_ratio", "secondary_enabled", "weekend_valuation",
        "usdcny_enabled", "cny_reference_enabled",
    ],
    "spread": [
        "max_multiplier", "rollover_multiplier", "monday_open_multiplier",
        "news_core_multiplier", "news_edge_multiplier", "market_max_slip_pips",
    ],
    "orders": [
        "max_pending", "default_tif", "allow_limit", "allow_stop",
        "allow_trailing", "allow_ifd", "quote_stale_fill_seconds",
    ],
    "risk": [
        "risk_per_trade_ratio", "margin_per_order_ratio", "max_positions",
        "max_units_per_pair", "daily_trade_limit", "daily_loss_limit_ratio",
        "max_drawdown_ratio", "forbid_add_to_loser", "loss_cut_cooldown_minutes",
        "post_loss_cut_resume_ratio",
    ],
    "decision": [
        "auto_trade", "llm_enabled", "interval_minutes", "decision_windows",
        "max_llm_calls_per_day", "prompt_char_limit", "decision_max_tokens",
        "report_max_tokens", "temperature", "decision_task_name", "report_task_name",
        "request_timeout_ms", "max_drift_pips",
    ],
    "swap": ["rollover_time", "timezone", "triple_weekday", "triple_enabled"],
    "notify": [
        "platform", "group_ids", "private_enabled", "event_reports",
        "merge_messages", "merge_window_ms",
    ],
    "admin": ["users"],
    "influence": [
        "enabled", "group_ids", "limit", "hours", "timeout_seconds",
    ],
    "calendar": [
        "enabled", "source", "refresh_minutes", "blackout_minutes",
        "block_high_impact_only", "announce_minutes", "events",
    ],
    "storage": ["history_limit"],
}


def make_schema(extra_fields: dict[str, dict] | None = None) -> dict:
    sections: dict[str, dict] = {}
    for source, names in FIELDS.items():
        sections[source] = {
            "name": source,
            "title": f"{source} 原始标题",
            "description": f"{source} 原始描述",
            "icon": "circle",
            "collapsed": False,
            "order": 0,
            "fields": {
                name: {
                    "name": name,
                    "type": "string",
                    "default": "",
                    "label": f"label-{name}",
                    "description": f"desc-{name}",
                    "hint": f"hint-{name}",
                    "hidden": False,
                    "disabled": False,
                    "order": 0,
                }
                for name in names
            },
        }
    if extra_fields:
        for source, fields in extra_fields.items():
            sections.setdefault(source, {"name": source, "title": source, "description": "", "fields": {}})[
                "fields"
            ].update(fields)
    return {
        "plugin_id": "github.nethielchou.mai-fx-sim",
        "plugin_info": {"name": "麦麦炒外汇", "version": "0.1.0"},
        "sections": sections,
        "layout": {"type": "grid"},
    }


def iter_fields(schema: dict):
    for section_id, section in schema["sections"].items():
        source = section.get("name")
        for key, field in section["fields"].items():
            path = key if "." in key else f"{source}.{key}"
            yield section_id, path, field


def find_field(schema: dict, path: str) -> dict | None:
    for _section_id, current, field in iter_fields(schema):
        if current == path:
            return field
    return None


class BuildConfigUiSchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.input_schema = make_schema()
        self.input_copy = copy.deepcopy(self.input_schema)
        self.output = config_ui.build_config_ui_schema(self.input_schema)

    def test_tabs_are_chinese(self) -> None:
        layout = self.output["layout"]
        self.assertEqual(layout["type"], "tabs")
        titles = [tab["title"] for tab in layout["tabs"]]
        self.assertEqual(titles, ["常用", "交易", "高级"])
        self.assertEqual([tab["id"] for tab in layout["tabs"]], ["basic", "trading", "advanced"])

    def test_layout_sections_exist(self) -> None:
        section_ids = set(self.output["sections"])
        for tab in self.output["layout"]["tabs"]:
            self.assertTrue(tab["sections"])
            for section_id in tab["sections"]:
                self.assertIn(section_id, section_ids, f"{tab['id']} → {section_id}")

    def test_tab_membership(self) -> None:
        by_id = {tab["id"]: tab["sections"] for tab in self.output["layout"]["tabs"]}
        self.assertIn("account_core", by_id["basic"])
        self.assertIn("admin", by_id["basic"])
        self.assertIn("swap", by_id["trading"])
        self.assertIn("decision_core", by_id["trading"])
        self.assertIn("plugin", by_id["advanced"])

    def test_labels_and_hints_chinese(self) -> None:
        field = find_field(self.output, "account.initial_balance")
        self.assertIsNotNone(field)
        self.assertEqual(field["label"], "初始资金（元）")
        self.assertIn("重置", field["hint"])
        self.assertEqual(field["description"], field["hint"])
        # 中文标签不能是原始英文键名
        self.assertNotEqual(field["label"], "initial_balance")

    def test_all_fields_covered(self) -> None:
        covered: set[str] = set()
        for _section_id, path, _field in iter_fields(self.output):
            covered.add(path)
        expected: set[str] = set()
        for source, names in FIELDS.items():
            expected.update(f"{source}.{name}" for name in names)
        missing = expected - covered
        self.assertEqual(missing, set(), f"这些字段在配置页里丢了：{sorted(missing)}")

    def test_hidden_developer_fields(self) -> None:
        for path in (
            "plugin.config_version",
            "market.source",
            "market.secondary_enabled",
            "orders.allow_trailing",
            "orders.allow_ifd",
            "storage.history_limit",
            "swap.timezone",
        ):
            field = find_field(self.output, path)
            self.assertIsNotNone(field, path)
            self.assertTrue(field.get("hidden"), f"{path} 应被隐藏")

    def test_disabled_readonly_fields(self) -> None:
        for path in ("account.currency", "plugin.config_version"):
            field = find_field(self.output, path)
            self.assertIsNotNone(field, path)
            self.assertTrue(field.get("disabled"), f"{path} 应只读")

    def test_visible_fields_not_hidden(self) -> None:
        for path in ("account.initial_balance", "decision.auto_trade", "admin.users", "notify.group_ids"):
            field = find_field(self.output, path)
            self.assertIsNotNone(field, path)
            self.assertFalse(field.get("hidden"), f"{path} 不应被隐藏")

    def test_field_order_sequential(self) -> None:
        for section_id, section in self.output["sections"].items():
            orders = [field["order"] for field in section["fields"].values()]
            self.assertEqual(orders, list(range(len(orders))), section_id)

    def test_section_titles_chinese(self) -> None:
        titles = {section_id: section["title"] for section_id, section in self.output["sections"].items()}
        self.assertEqual(titles["account_core"], "虚拟账户")
        self.assertEqual(titles["risk_core"], "风控（常用）")
        self.assertEqual(titles["swap"], "隔夜利息")
        for section_id, title in titles.items():
            self.assertNotIn(section_id, title, f"{section_id} 标题没有中文化")

    def test_section_meta_kept(self) -> None:
        section = self.output["sections"]["account_core"]
        self.assertEqual(section["name"], "account")
        self.assertTrue(section["description"])
        self.assertEqual(section["collapsed"], False)
        self.assertTrue(self.output["sections"]["account_advanced"]["collapsed"])

    def test_input_not_mutated(self) -> None:
        self.assertEqual(self.input_schema, self.input_copy, "build_config_ui_schema 不应原地修改输入")

    def test_extra_unknown_field_kept_in_fallback_section(self) -> None:
        schema = make_schema(
            extra_fields={"account": {"future_field": {"name": "future_field", "type": "string", "default": "x"}}}
        )
        output = config_ui.build_config_ui_schema(schema)
        field = find_field(output, "account.future_field")
        self.assertIsNotNone(field, "新版本新增字段不应丢失")
        section = output["sections"]["other_settings"]
        self.assertEqual(section["title"], "其他设置")
        self.assertIn("other_settings", output["layout"]["tabs"][2]["sections"])

    def test_empty_schema(self) -> None:
        output = config_ui.build_config_ui_schema({})
        self.assertEqual(output["sections"], {})
        self.assertEqual(output["layout"], {"type": "tabs", "tabs": []})

    def test_non_dict_schema_passthrough(self) -> None:
        self.assertIsNone(config_ui.build_config_ui_schema(None))
        self.assertEqual(config_ui.build_config_ui_schema([]), [])

    def test_partial_schema_only_account(self) -> None:
        schema = make_schema()
        schema["sections"] = {"account": schema["sections"]["account"]}
        output = config_ui.build_config_ui_schema(schema)
        section_ids = set(output["sections"])
        self.assertIn("account_core", section_ids)
        self.assertNotIn("admin", section_ids)  # 输入里没有的配置节不硬造
        self.assertIn("account.initial_balance", {path for _sid, path, _f in iter_fields(output)})

    def test_labels_not_english_for_known_fields(self) -> None:
        for path in ("account.leverage", "risk.max_positions", "decision.auto_trade", "swap.rollover_time"):
            field = find_field(self.output, path)
            self.assertIsNotNone(field, path)
            self.assertRegex(field["label"], r"[\u4e00-\u9fff]", f"{path} 标签应为中文")

    def test_influence_fields_chinese(self) -> None:
        for path in (
            "influence.enabled",
            "influence.group_ids",
            "influence.limit",
            "influence.hours",
            "influence.timeout_seconds",
        ):
            field = find_field(self.output, path)
            self.assertIsNotNone(field, path)
            self.assertRegex(field["label"], r"[\u4e00-\u9fff]", f"{path} 标签应为中文")
            self.assertNotIn(path.split(".")[-1], field["label"], f"{path} 标签不应是英文键名")

    def test_cny_reference_field_chinese_and_visible(self) -> None:
        # v0.3.4：行情页新增「显示人民币参考汇率」开关（默认开，普通用户可见）
        field = find_field(self.output, "market.cny_reference_enabled")
        self.assertIsNotNone(field)
        self.assertRegex(field["label"], r"[\u4e00-\u9fff]", "标签应为中文")
        self.assertNotIn("cny_reference_enabled", field["label"], "标签不应是英文键名")
        self.assertIn("参考", field["label"])
        self.assertIn("参考", field["hint"])
        self.assertIn("不可交易", field["hint"])
        self.assertFalse(field.get("hidden"), "参考汇率开关应可见")
        self.assertFalse(field.get("disabled"), "参考汇率开关应可改")

    def test_no_persona_section_or_fields(self) -> None:
        # 人设设置已整体移除：设置页不能再出现任何 persona.* 配置项
        self.assertNotIn("persona_core", self.output["sections"])
        self.assertNotIn("persona_advanced", self.output["sections"])
        self.assertEqual(
            [path for _sid, path, _field in iter_fields(self.output) if path.startswith("persona.")],
            [],
        )
        influence_fields = [
            path for _sid, path, _field in iter_fields(self.output) if path.startswith("influence.")
        ]
        self.assertEqual(len(influence_fields), len(set(influence_fields)))


if __name__ == "__main__":
    unittest.main()
