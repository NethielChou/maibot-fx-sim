"""P0 真实链路冒烟（联网 + 临时账本，不碰机器人、不部署）。

用法（项目根目录）：
  <botpython> scripts/smoke_live.py
退出码 0=通过。验证：新浪行情解析 → tick → 手动市价开仓 → 平仓 → 账本一致性。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PKG = "_mai_fx_smoke_pkg"


def load_package():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(
        PKG, str(PROJECT_ROOT / "plugin.py"), submodule_search_locations=[str(PROJECT_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    load_package()
    models = importlib.import_module(f"{PKG}.models")
    pairs = importlib.import_module(f"{PKG}.pairs")
    quotes = importlib.import_module(f"{PKG}.quotes")
    ledger_mod = importlib.import_module(f"{PKG}.ledger")
    sched_mod = importlib.import_module(f"{PKG}.scheduler")
    config_mod = importlib.import_module(f"{PKG}.config")

    cfg = config_mod.MaiFxConfig()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ledger = ledger_mod.Ledger(Path(tmp) / "ledger.sqlite3")
        ledger.init(int(cfg.account.initial_balance))
        try:
            fake_ctx = SimpleNamespace(call_capability=None)
            engine = sched_mod.TradingEngine(
                ctx=fake_ctx,
                config=cfg,
                quote_source=quotes.QuoteSource(timeout_seconds=8),
                ledger=ledger,
                log=lambda msg: print("[engine]", msg),
            )
            await engine.tick_once()
            print("quotes:", {k: (str(v.bid), str(v.ask)) for k, v in engine.quotes.items()})
            print("rates:", engine._rates())
            if not engine.quotes or engine._rates() is None:
                print("FAILED: 没有取到行情")
                return 1
            ok, message = engine.manual_order(pair_text="USDJPY", side=models.SIDE_BUY, units=10_000, user="smoke")
            print("open:", ok, message)
            if not ok:
                return 1
            await engine.tick_once()
            print("positions:", [(p.pair, p.side, p.units, str(p.entry_price)) for p in engine.positions])
            if not engine.positions:
                print("FAILED: 没有建玉")
                return 1
            print("equity:", engine.equity_cny(), "ratio:", engine.maintenance_ratio())
            ok, message = engine.close_position("USDJPY", user="smoke")
            print("close:", ok, message)
            await engine.tick_once()
            print("balance:", engine.account.balance, "positions:", len(engine.positions))
            issues = ledger.check_accounting(engine.account, engine.positions, pairs.PAIRS, engine.quotes, engine._rates())
            print("accounting issues:", issues)
            print("events:", engine.drain_events()[:6])
            if engine.positions:
                print("FAILED: 平仓后仍有持仓")
                return 1
            if issues:
                print("FAILED: 账本不一致")
                return 1
        finally:
            ledger.close()
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
