"""P0 交付前预检（只读）：文件齐全 / manifest 基本字段 / 版本口径 / 模块可编译。

用法（项目根目录）：
  <botpython> scripts/preflight.py
退出码：0=通过；1=失败。

版本口径：
- **代码版本**以 ``plugin.py`` 的 ``PLUGIN_VERSION`` 为准（本版必须为 0.3.4）；
- ``_manifest.json`` 的 ``version`` 由发布流程单独维护（本脚本只校验它是合法三段式，
  不要求与代码版本一致，避免本地改版本被误判为发布）。
"""

from __future__ import annotations

import json
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PLUGIN_VERSION = "0.3.4"
REQUIRED = [
    "plugin.py", "_manifest.json", "config.py", "config_ui.py", "models.py", "pairs.py",
    "market_clock.py", "quotes.py", "execution_cost.py", "orders.py", "accounting.py",
    "risk.py", "ledger.py", "decision.py", "report.py", "commands.py", "scheduler.py",
    "influence.py", "econ_calendar.py", "calendar_source.py", "fxconv.py",
    "README.md", "CHANGELOG.md", "config.example.toml", "LICENSE", ".gitignore",
]
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
PLUGIN_VERSION_RE = re.compile(r'^PLUGIN_VERSION\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def main() -> int:
    errors: list[str] = []
    for name in REQUIRED:
        if not (ROOT / name).exists():
            errors.append(f"缺少文件：{name}")
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    for key in ("manifest_version", "id", "version", "name", "author", "license", "urls", "host_application", "sdk", "capabilities", "i18n"):
        if key not in manifest:
            errors.append(f"manifest 缺少字段：{key}")
    if not SEMVER_RE.match(str(manifest.get("version") or "")):
        errors.append(f"manifest 版本不是三段式版本号：{manifest.get('version')!r}")

    plugin_version = ""
    plugin_path = ROOT / "plugin.py"
    if plugin_path.exists():
        match = PLUGIN_VERSION_RE.search(plugin_path.read_text(encoding="utf-8"))
        if match is None:
            errors.append("plugin.py 里找不到 PLUGIN_VERSION")
        else:
            plugin_version = match.group(1)
            if plugin_version != EXPECTED_PLUGIN_VERSION:
                errors.append(
                    f"PLUGIN_VERSION 不是 {EXPECTED_PLUGIN_VERSION}：{plugin_version}"
                )

    for file in ROOT.glob("*.py"):
        try:
            py_compile.compile(str(file), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"编译失败 {file.name}: {exc}")
    for file in (ROOT / "tests").glob("*.py"):
        try:
            py_compile.compile(str(file), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"编译失败 tests/{file.name}: {exc}")
    if errors:
        print("PREFLIGHT FAILED")
        for item in errors:
            print(" -", item)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "files": len(REQUIRED),
                "plugin_version": plugin_version,
                "manifest_version": manifest["version"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
