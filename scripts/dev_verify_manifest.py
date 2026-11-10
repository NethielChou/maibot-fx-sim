"""用真实宿主 ManifestValidator 校验 _manifest.json（开发/交付证据脚本）。

前置：设置环境变量 MAIBOT_INSTANCE_DIR 指向当前实例根（只影响本进程，不改机器人）。

用法：
  设置 MAIBOT_INSTANCE_DIR=<实例根>，再用 <generation 目录下的 python.exe> 运行
  scripts/dev_verify_manifest.py

退出码：0=通过；1=校验失败；2=缺少环境变量。
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    instance_dir = os.environ.get("MAIBOT_INSTANCE_DIR", "").strip()
    if not instance_dir:
        print("缺少 MAIBOT_INSTANCE_DIR（指向实例根目录）", file=sys.stderr)
        return 2

    from src.plugin_runtime.runner.manifest_validator import ManifestValidator
    raw = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    validator = ManifestValidator(
        host_version="1.3.19",
        sdk_version="2.21.0",
        project_root=PROJECT_ROOT,
    )
    schema_ok = validator.validate(raw)
    loaded = validator.load_from_plugin_path(PROJECT_ROOT, require_entrypoint=True)
    result = {
        "instance_dir": instance_dir,
        "schema_ok": schema_ok,
        "load_ok": loaded is not None,
        "errors": validator.errors,
        "warnings": validator.warnings,
        "plugin_id": getattr(loaded, "id", ""),
        "version": getattr(loaded, "version", ""),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if schema_ok and loaded is not None and not validator.errors else 1


if __name__ == "__main__":
    sys.exit(main())
