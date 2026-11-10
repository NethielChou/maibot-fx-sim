"""pairs.py 单元测试：品种规格、解析、配置白名单、USDJPY 中间价。

离线纯函数测试，不联网、不 import maibot_sdk。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from models import PairSpec, QuoteSnapshot
from pairs import (
    DEFAULT_PAIR_CODES,
    PAIRS,
    get_pair,
    pair_codes_from_config,
    pairs_from_config,
    resolve_pair,
    usdjpy_mid,
)

_NOW = datetime(2026, 11, 6, 9, 27, 42, tzinfo=timezone.utc)


class TestPairSpecs(unittest.TestCase):
    """品种规格表（Rev 1.1 §1.2 / §4.2 / §3.3）。"""

    def test_all_eight_pairs_present(self) -> None:
        self.assertEqual(len(PAIRS), 8)
        self.assertEqual(len(DEFAULT_PAIR_CODES), 8)
        self.assertEqual(set(DEFAULT_PAIR_CODES), set(PAIRS))
        self.assertEqual(
            DEFAULT_PAIR_CODES,
            ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "TRYJPY", "MXNJPY", "ZARJPY", "EURUSD"],
        )

    def test_usdjpy_spec(self) -> None:
        spec = PAIRS["USDJPY"]
        self.assertIsInstance(spec, PairSpec)
        self.assertEqual(spec.name_cn, "美元/日元")
        self.assertEqual(spec.digits, 3)
        self.assertEqual(spec.tick, Decimal("0.001"))
        self.assertEqual(spec.pip, Decimal("0.01"))
        self.assertEqual(spec.quote_currency, "JPY")
        self.assertEqual(spec.sina_code, "fx_susdjpy")
        self.assertEqual(spec.source_kind, "raw")
        self.assertEqual(spec.base_spread_pips, Decimal("0.3"))
        self.assertEqual(spec.max_spread_pips, Decimal("8.0"))
        self.assertEqual((spec.swap_long_jpy, spec.swap_short_jpy), (Decimal("160"), Decimal("-210")))
        self.assertEqual(spec.max_units, 100_000)

    def test_eurusd_spec(self) -> None:
        spec = PAIRS["EURUSD"]
        self.assertEqual(spec.digits, 5)
        self.assertEqual(spec.tick, Decimal("0.00001"))
        self.assertEqual(spec.pip, Decimal("0.0001"))
        self.assertEqual(spec.quote_currency, "USD")
        self.assertEqual(spec.source_kind, "raw")
        self.assertEqual(spec.base_spread_pips, Decimal("0.5"))
        self.assertEqual((spec.swap_long_jpy, spec.swap_short_jpy), (Decimal("-101"), Decimal("55")))

    def test_high_yield_pairs_spec(self) -> None:
        for code in ("TRYJPY", "MXNJPY", "ZARJPY"):
            spec = PAIRS[code]
            self.assertEqual(spec.digits, 4, code)
            self.assertEqual(spec.tick, Decimal("0.0001"), code)
            self.assertEqual(spec.pip, Decimal("0.01"), code)
            self.assertEqual(spec.source_kind, "computed", code)
            self.assertEqual(spec.max_units, 200_000, code)
        self.assertEqual(PAIRS["TRYJPY"].sina_code, "fx_stryjpy")
        self.assertEqual(PAIRS["MXNJPY"].sina_code, "fx_smxnjpy")
        self.assertEqual(PAIRS["ZARJPY"].sina_code, "fx_szarjpy")

    def test_computed_pairs(self) -> None:
        for code in ("EURJPY", "GBPJPY", "AUDJPY", "TRYJPY", "MXNJPY", "ZARJPY"):
            spec = PAIRS[code]
            self.assertEqual(spec.source_kind, "computed", code)
            self.assertEqual(spec.quote_currency, "JPY", code)
        self.assertEqual(PAIRS["GBPJPY"].max_spread_pips, Decimal("6.0"))
        self.assertEqual(PAIRS["TRYJPY"].max_spread_pips, Decimal("12.0"))


class TestGetPair(unittest.TestCase):
    def test_get_pair_returns_registered_instance(self) -> None:
        self.assertIs(get_pair("USDJPY"), PAIRS["USDJPY"])

    def test_get_pair_is_case_insensitive(self) -> None:
        self.assertIs(get_pair("usdjpy"), PAIRS["USDJPY"])
        self.assertIs(get_pair(" eur/jpy "), PAIRS["EURJPY"])

    def test_get_pair_unknown_raises_keyerror_with_reason(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            get_pair("XAUUSD")
        self.assertIn("UNKNOWN_PAIR", str(ctx.exception))

    def test_get_pair_non_string_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_pair(None)  # type: ignore[arg-type]


class TestResolvePair(unittest.TestCase):
    def test_resolve_by_code(self) -> None:
        self.assertEqual(resolve_pair("USDJPY").code, "USDJPY")
        self.assertEqual(resolve_pair("usdjpy").code, "USDJPY")
        self.assertEqual(resolve_pair("USD/JPY").code, "USDJPY")
        self.assertEqual(resolve_pair("fx_susdjpy").code, "USDJPY")

    def test_resolve_by_chinese_name(self) -> None:
        self.assertEqual(resolve_pair("美元日元").code, "USDJPY")
        self.assertEqual(resolve_pair("美元/日元").code, "USDJPY")
        self.assertEqual(resolve_pair("欧元/美元").code, "EURUSD")

    def test_resolve_by_short_alias(self) -> None:
        expected = {
            "美日": "USDJPY",
            "欧日": "EURJPY",
            "镑日": "GBPJPY",
            "英日": "GBPJPY",
            "澳日": "AUDJPY",
            "土日": "TRYJPY",
            "墨日": "MXNJPY",
            "南日": "ZARJPY",
            "欧美": "EURUSD",
        }
        for text, code in expected.items():
            self.assertEqual(resolve_pair(text).code, code, text)

    def test_resolve_unknown_returns_none(self) -> None:
        for text in ("黄金", "XAUUSD", "", "   ", "美元"):
            self.assertIsNone(resolve_pair(text), text)

    def test_resolve_non_string_returns_none(self) -> None:
        self.assertIsNone(resolve_pair(None))  # type: ignore[arg-type]


class TestConfigPools(unittest.TestCase):
    def test_empty_config_returns_all_default(self) -> None:
        codes = pair_codes_from_config([])
        self.assertEqual(codes, DEFAULT_PAIR_CODES)
        self.assertIsNot(codes, DEFAULT_PAIR_CODES)  # 返回副本，调用方修改不影响常量

        pairs = pairs_from_config([])
        self.assertEqual(list(pairs.keys()), DEFAULT_PAIR_CODES)

    def test_unknown_codes_filtered_but_order_kept(self) -> None:
        codes = pair_codes_from_config(["EURJPY", "FOO", "USDJPY", "eurusd"])
        self.assertEqual(codes, ["EURJPY", "USDJPY", "EURUSD"])
        self.assertEqual(pair_codes_from_config(["USDJPY", "USDJPY"]), ["USDJPY"])

    def test_chinese_names_in_config_are_resolved(self) -> None:
        self.assertEqual(pair_codes_from_config(["美日", "欧美"]), ["USDJPY", "EURUSD"])

    def test_all_unknown_returns_empty(self) -> None:
        self.assertEqual(pair_codes_from_config(["NOPE"]), [])
        self.assertEqual(pairs_from_config(["NOPE"]), {})

    def test_pairs_from_config_values(self) -> None:
        pairs = pairs_from_config(["USDJPY", "EURUSD"])
        self.assertEqual(list(pairs.keys()), ["USDJPY", "EURUSD"])
        self.assertIs(pairs["USDJPY"], PAIRS["USDJPY"])


class TestReferencePairs(unittest.TestCase):
    """人民币参考汇率表（v0.3.4）：只有名称/代码/小数位/展示倍数，不可交易。"""

    def test_reference_table(self) -> None:
        import pairs

        self.assertEqual(
            pairs.REFERENCE_PAIR_CODES,
            ["USDCNY", "EURCNY", "JPYCNY", "GBPCNY", "AUDCNY", "HKDCNY", "CADCNY", "NZDCNY"],
        )
        self.assertEqual(len(pairs.REFERENCE_PAIRS), 8)
        self.assertEqual(len(pairs.reference_pairs()), 8)
        self.assertEqual(pairs.REFERENCE_PAIRS["USDCNY"].name_cn, "美元/人民币")
        self.assertEqual(pairs.REFERENCE_PAIRS["JPYCNY"].scale, 100)
        self.assertEqual(pairs.REFERENCE_PAIRS["USDCNY"].pip, pairs.REFERENCE_PIP)
        for code, reference in pairs.REFERENCE_PAIRS.items():
            self.assertEqual(reference.code, code)
            self.assertEqual(reference.digits, 4, code)
            # 没有交易规则字段（点差上限/杠杆/隔夜利息/最大数量）
            for attr in ("max_spread_pips", "swap_long_jpy", "max_units", "tick"):
                self.assertFalse(hasattr(reference, attr), f"{code} 不该有 {attr}")

    def test_reference_kept_out_of_tradable_tables(self) -> None:
        import pairs

        for code in pairs.REFERENCE_PAIR_CODES:
            self.assertNotIn(code, pairs.PAIRS, code)
            self.assertNotIn(code, pairs.DEFAULT_PAIR_CODES, code)
            self.assertIsNone(pairs.resolve_pair(code), code)
        self.assertEqual(pairs.pair_codes_from_config(pairs.REFERENCE_PAIR_CODES), [])
        self.assertEqual(pairs.pairs_from_config(pairs.REFERENCE_PAIR_CODES), {})

    def test_get_reference(self) -> None:
        import pairs

        self.assertEqual(pairs.get_reference("usdcny").code, "USDCNY")
        self.assertEqual(pairs.get_reference("美元/人民币").code, "USDCNY")
        self.assertEqual(pairs.get_reference("fx_susdcny").code, "USDCNY")
        self.assertIsNone(pairs.get_reference("USDJPY"))
        self.assertIsNone(pairs.get_reference(None))

    def test_usdcny_constants_match_fxconv_anchor(self) -> None:
        import fxconv
        import pairs

        self.assertEqual(pairs.REFERENCE_PAIRS["USDCNY"].code, fxconv.USDCNY_CODE)
        self.assertEqual(pairs.REFERENCE_PAIRS["USDCNY"].sina_code, fxconv.USDCNY_SINA_CODE)


class TestUsdjpyMid(unittest.TestCase):
    @staticmethod
    def _quote(bid, ask, mid) -> QuoteSnapshot:
        return QuoteSnapshot(
            pair="USDJPY",
            bid=None if bid is None else Decimal(bid),
            ask=None if ask is None else Decimal(ask),
            mid=None if mid is None else Decimal(mid),
            quote_ts=_NOW,
            recv_ts=_NOW,
        )

    def test_mid_field_preferred(self) -> None:
        quotes = {"USDJPY": self._quote("158.180", "158.190", "158.185")}
        self.assertEqual(usdjpy_mid(quotes), Decimal("158.185"))

    def test_mid_computed_from_bid_ask_when_missing(self) -> None:
        quotes = {"USDJPY": self._quote("158.180", "158.190", None)}
        self.assertEqual(usdjpy_mid(quotes), Decimal("158.185"))

    def test_missing_pair_returns_none(self) -> None:
        self.assertIsNone(usdjpy_mid({}))
        self.assertIsNone(usdjpy_mid({"EURUSD": self._quote("1.1", "1.2", None)}))

    def test_incomplete_quote_returns_none(self) -> None:
        self.assertIsNone(usdjpy_mid({"USDJPY": self._quote(None, "158.190", None)}))
        self.assertIsNone(usdjpy_mid({"USDJPY": self._quote(None, None, None)}))
        self.assertIsNone(usdjpy_mid({"USDJPY": self._quote("0", "0", None)}))


class TestPipValue(unittest.TestCase):
    """PairSpec.pip_value_jpy 的品种侧行为（EURUSD 走 USDJPY 换算）。"""

    def test_jpy_quote_pip_value(self) -> None:
        self.assertEqual(
            PAIRS["USDJPY"].pip_value_jpy(10_000, Decimal("158.185")),
            Decimal("100"),
        )

    def test_eurusd_pip_value(self) -> None:
        self.assertEqual(
            PAIRS["EURUSD"].pip_value_jpy(10_000, Decimal("158.185")),
            Decimal("0.0001") * Decimal(10_000) * Decimal("158.185"),
        )


if __name__ == "__main__":
    unittest.main()
