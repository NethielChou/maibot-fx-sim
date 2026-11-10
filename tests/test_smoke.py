"""P0 骨架契约测试（Bot Python 下运行）。

覆盖：宿主方式加载包、create_plugin、生命周期覆写、默认配置、组件注册、
manifest 静态字段、config.example.toml 与默认值一致。
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MODULE_NAME = "_maibot_plugin_github_nethielchou_mai_fx_sim_smoke"


def load_plugin_module():
    sys.modules.pop(PLUGIN_MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(
        PLUGIN_MODULE_NAME,
        str(PROJECT_ROOT / "plugin.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建插件模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class PluginContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin_module()
        cls.plugin = cls.module.create_plugin()

    def test_create_plugin(self):
        from maibot_sdk.plugin import MaiBotPlugin

        self.assertIsInstance(self.plugin, MaiBotPlugin)
        self.assertTrue(callable(self.module.create_plugin))
        self.assertEqual(self.module.PLUGIN_VERSION, "0.3.4")

    def test_lifecycle_overridden(self):
        from maibot_sdk.plugin import MaiBotPlugin

        plugin_type = type(self.plugin)
        self.assertIsNot(plugin_type.on_load, MaiBotPlugin.on_load)
        self.assertIsNot(plugin_type.on_unload, MaiBotPlugin.on_unload)
        self.assertIsNot(plugin_type.on_config_update, MaiBotPlugin.on_config_update)

    def test_default_config(self):
        config = type(self.plugin).build_default_config()
        self.assertEqual(config["plugin"]["config_version"], "0.3.4")
        for section in (
            "plugin", "account", "market", "spread", "orders", "risk",
            "influence", "decision", "swap", "notify", "admin", "calendar", "storage",
        ):
            self.assertIn(section, config)
        # 人设设置已整体移除：插件配置里不再有 [persona] 段
        self.assertNotIn("persona", config)
        self.assertEqual(config["account"]["currency"], "CNY")
        self.assertEqual(config["account"]["leverage"], 10)
        self.assertEqual(config["market"]["pairs"], [])
        # v0.3.4：行情卡默认显示「人民币参考汇率」栏（只展示、不可交易）
        self.assertTrue(config["market"]["cny_reference_enabled"])
        self.assertTrue(config["market"]["usdcny_enabled"])
        self.assertFalse(config["decision"]["auto_trade"])
        self.assertEqual(config["notify"]["group_ids"], [])
        self.assertEqual(config["admin"]["users"], [])
        # 群聊参考材料默认关闭；键精简为 5 个
        self.assertEqual(
            set(config["influence"]), {"enabled", "group_ids", "limit", "hours", "timeout_seconds"}
        )
        self.assertFalse(config["influence"]["enabled"])
        self.assertEqual(config["influence"]["limit"], 20)

    def test_no_stdlib_shadowing_calendar_module(self):
        # 财经日历已改名 econ_calendar.py；项目根不能再有 calendar.py
        self.assertFalse((PROJECT_ROOT / "calendar.py").exists())
        self.assertTrue((PROJECT_ROOT / "econ_calendar.py").exists())

    def test_config_schema(self):
        schema = type(self.plugin).build_config_schema(
            plugin_id="github.nethielchou.mai-fx-sim",
            plugin_name="麦麦炒外汇",
            plugin_version="0.1.0",
        )
        self.assertIsInstance(schema, dict)
        self.assertTrue(schema)
        # 人设设置已整体移除：配置页 Schema 里也没有 persona 配置节
        self.assertNotIn("persona", schema.get("sections", {}))

    def test_components_registered(self):
        components = self.plugin.get_components()
        self.assertIsInstance(components, list)
        dump = json.dumps(components, ensure_ascii=False, default=str)
        self.assertIn("外汇", dump)
        # 命令/帮助里不再有人设相关条目
        self.assertNotIn("人设", dump)

    def test_manifest_static_checks(self):
        raw = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))
        required = (
            "manifest_version", "id", "version", "name", "description", "author",
            "license", "urls", "host_application", "sdk", "capabilities", "i18n",
        )
        for field in required:
            self.assertIn(field, raw)
        self.assertEqual(raw["manifest_version"], 2)
        self.assertEqual(raw["id"], "github.nethielchou.mai-fx-sim")
        self.assertRegex(raw["version"], r"^\d+\.\d+\.\d+$")
        for key in ("host_application", "sdk"):
            self.assertRegex(raw[key]["min_version"], r"^\d+\.\d+\.\d+$")
            self.assertRegex(raw[key]["max_version"], r"^\d+\.\d+\.\d+$")
        self.assertIn("send.forward", raw["capabilities"])
        self.assertIn("send.text", raw["capabilities"])
        self.assertEqual(raw["i18n"]["default_locale"], "zh-CN")

    def test_config_example_matches_defaults(self):
        import tomllib

        example = tomllib.loads((PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8"))
        defaults = type(self.plugin).build_default_config()
        # 逐键一致：示例文件不能多键、少键或值不同
        self.assertEqual(example, defaults)

    def test_persona_module_removed(self):
        # 人设读取逻辑已并入 plugin._load_persona（和期货插件一致）
        self.assertFalse((PROJECT_ROOT / "persona.py").exists())
        self.assertFalse((PROJECT_ROOT / "tests" / "test_persona.py").exists())


if __name__ == "__main__":
    unittest.main()
