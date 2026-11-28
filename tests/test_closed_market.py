"""休市（周末/收盘后）行为回归：不播报、不日切。

用户实测（2026-11-28 周六 12:18 / 12:32 / 13:23）：
群里每 5 分钟收到两条卡片——
「行情校验未通过」（9 个品种全 QUOTE_STALE）和「汇率不可用」（缺 USDJPY/USDCNY）。
原因：外汇市场周末休市，行情源停在周五收盘时点，插件的行情校验/日切分支
**只在开市时才有意义**，却仍然播报。休市期间这类告警属于噪音。

口径：
- 休市时：不播报「行情校验未通过」/「汇率不可用」，也不做日切（周末不计息）；
- 开市时：行为保持不变（该报的照报）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tests.test_codex_regressions as reg  # noqa: E402

# 2026-11-28 是周六（NY 时间周五晚 → 休市）；2026-11-27 周五 14:00 UTC 仍是开市时段。
SATURDAY = datetime(2026, 11, 28, 4, 30, tzinfo=timezone.utc)      # 北京时间 12:30 周六
FRIDAY_OPEN = datetime(2026, 11, 27, 14, 0, tzinfo=timezone.utc)   # 北京时间 22:00 周五，开市


class ClosedMarketTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = reg.load_modules()
        cls.config = reg.test_config()

    def _engine(self, db, source):
        fixture = reg.Fixture(self.modules, self.config, pathlib.Path(tempfile.mkdtemp()) / "x.sqlite3")
        engine = fixture.engine(db, source=source)
        return fixture, engine

    def test_stale_quotes_are_silent_when_market_closed(self) -> None:
        """周六：全部品种过期也不往群里播「行情校验未通过」。"""

        source = reg.ScriptedSource(self.modules.models)
        source.mode = "stale_source"  # 源时间落后 2 小时（模拟休市停在收盘价）
        fixture = reg.Fixture(self.modules, self.config, pathlib.Path(tempfile.mkdtemp()) / "a.sqlite3")
        with fixture.database() as db:
            _, engine = self._engine(db, source)
            fixture.tick(engine, SATURDAY)
            engine.drain_events()
            for offset in (2, 120, 300):
                fixture.tick(engine, SATURDAY + timedelta(seconds=offset))
                events = engine.drain_events()
                self.assertEqual(
                    [e for e in events if "行情校验未通过" in e], [],
                    "休市时不应播报行情校验失败",
                )
                self.assertEqual(
                    [e for e in events if "汇率不可用" in e], [],
                    "休市时不应播报汇率不可用",
                )

    def test_missing_anchor_is_silent_when_market_closed(self) -> None:
        """周六缺 USDCNY 锚：不播报汇率不可用。"""

        source = reg.ScriptedSource(self.modules.models)
        source.mode = "missing_usdcny"
        fixture = reg.Fixture(self.modules, self.config, pathlib.Path(tempfile.mkdtemp()) / "b.sqlite3")
        with fixture.database() as db:
            _, engine = self._engine(db, source)
            fixture.tick(engine, SATURDAY)
            events = engine.drain_events()
            self.assertEqual([e for e in events if "汇率不可用" in e], [])

    def test_stale_quotes_still_reported_when_market_open(self) -> None:
        """周五开市时段：行情过期该报就报（不能被休市规则误伤）。"""

        base = reg.ScriptedSource(self.modules.models)
        fixture = reg.Fixture(self.modules, self.config, pathlib.Path(tempfile.mkdtemp()) / "c.sqlite3")

        class StaleAfterFirst(reg.ScriptedSource):
            def __init__(self, models):
                super().__init__(models)
                self.calls = 0

            async def fetch(self, codes, now, seq):
                self.calls += 1
                if self.calls > 1:
                    self.mode = "stale_source"
                return await super().fetch(codes, now, seq)

        source = StaleAfterFirst(self.modules.models)
        with fixture.database() as db:
            _, engine = self._engine(db, source)
            fixture.tick(engine, FRIDAY_OPEN)
            engine.drain_events()
            fixture.tick(engine, FRIDAY_OPEN + timedelta(seconds=1))
            events = engine.drain_events()
            self.assertTrue(
                [e for e in events if "行情校验未通过" in e],
                "开市时段行情过期必须播报",
            )

    def test_no_rollover_while_market_closed(self) -> None:
        """休市不推进日切水位（周末不计息，等开市补算）。"""

        source = reg.ScriptedSource(self.modules.models)
        fixture = reg.Fixture(self.modules, self.config, pathlib.Path(tempfile.mkdtemp()) / "d.sqlite3")
        with fixture.database() as db:
            _, engine = self._engine(db, source)
            fixture.tick(engine, SATURDAY)
            before = dict(engine._warn_at)
            self.assertEqual(engine._last_rollover_date, "", "休市首 tick 不应推进日切水位")
            del before  # 仅断言水位

    def test_market_open_helper(self) -> None:
        self.assertFalse(self.modules.scheduler.TradingEngine._market_open(SATURDAY))
        self.assertTrue(self.modules.scheduler.TradingEngine._market_open(FRIDAY_OPEN))


if __name__ == "__main__":
    unittest.main()