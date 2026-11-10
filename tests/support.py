"""测试辅助：按宿主方式加载插件包、构造行情 fixture、临时账本。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_maibot_plugin_mai_fx_test_pkg"
CST = ZoneInfo("Asia/Shanghai")


def load_package() -> object:
    """按宿主 plugin_loader 的方式加载 plugin.py 为包，并注册到 sys.modules。"""

    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        str(PROJECT_ROOT / "plugin.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建插件模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def submodule(name: str):
    load_package()
    return importlib.import_module(f"{PACKAGE_NAME}.{name}")


def make_quote(
    pair: str = "USDJPY",
    *,
    bid: str = "158.180",
    ask: str = "158.190",
    age_seconds: float = 1.0,
    now: datetime | None = None,
    seq: int = 1,
):
    """构造 QuoteSnapshot（默认 USDJPY 158.18/158.19）。"""

    module = submodule("models")
    base = now or datetime(2026, 11, 6, 9, 0, 0, tzinfo=CST)
    quote_ts = base - timedelta(seconds=age_seconds)
    bid_d = Decimal(bid)
    ask_d = Decimal(ask)
    return module.QuoteSnapshot(
        pair=pair,
        bid=bid_d,
        ask=ask_d,
        mid=(bid_d + ask_d) / 2,
        quote_ts=quote_ts,
        recv_ts=base,
        seq=seq,
    )


def quotes_for(*pairs: str, now: datetime | None = None) -> dict:
    """构造常用 8 个货币对的行情字典（测试固定价）。"""

    defaults = {
        "USDJPY": ("158.180", "158.190"),
        "EURJPY": ("176.830", "176.920"),
        "GBPJPY": ("211.700", "211.820"),
        "AUDJPY": ("108.700", "108.730"),
        "TRYJPY": ("3.1550", "3.1570"),
        "MXNJPY": ("8.4650", "8.4700"),
        "ZARJPY": ("9.5730", "9.5770"),
        "EURUSD": ("1.11770", "1.11780"),
    }
    result = {}
    for pair in pairs or tuple(defaults):
        bid, ask = defaults[pair]
        result[pair] = make_quote(pair, bid=bid, ask=ask, now=now)
    return result
