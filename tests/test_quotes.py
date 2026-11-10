"""quotes.py 单元测试：新浪响应解析、字段映射、新鲜度、GB18030、抓取异常、人民币参考对。

全部离线：fixture 文本 + 固定 UTC 时间；抓取路径用 monkeypatch 替换 ``_fetch_bytes``，
不产生任何网络请求。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from models import QuoteSnapshot
from quotes import (
    QuoteSource,
    decode_sina_bytes,
    is_fresh,
    parse_sina_fx,
    parse_sina_row,
)

#: 固定"收到时刻"：2026-11-06（周五）17:27:42 北京 = 09:27:42 UTC。
NOW = datetime(2026, 11, 6, 9, 27, 42, tzinfo=timezone.utc)

#: 新浪 18 字段行（引号内内容）。
def _row(
    time: str = "17:27:42",
    bid: str = "158.180",
    ask: str = "158.190",
    prev: str = "158.100",
    open_: str = "158.150",
    high: str = "158.300",
    low: str = "158.000",
    last: str = "158.180",
    name: str = "美元兑日元即期汇率",
    date: str = "2026-11-06",
) -> str:
    fields = [
        time,
        bid,
        ask,
        prev,
        "68",
        open_,
        high,
        low,
        last,
        name,
        "-0.050000",
        "-0.080000",
        "0.100000",
        "",
        "160.000",
        "150.000",
        "",
        date,
    ]
    assert len(fields) == 18
    return ",".join(fields)


SINA_TEXT = (
    'var hq_str_fx_susdjpy="' + _row() + '";\n'
    'var hq_str_fx_seurjpy="'
    + _row(
        bid="176.830",
        ask="176.920",
        prev="176.500",
        open_="176.600",
        high="177.000",
        low="176.400",
        last="176.830",
        name="欧元兑日元即期汇率",
    )
    + '";\n'
    'var hq_str_fx_seurusd="'
    + _row(
        bid="1.117100",
        ask="1.117200",
        prev="1.121800",
        open_="1.121700",
        high="1.123400",
        low="1.116600",
        last="1.117100",
        name="欧元兑美元即期汇率",
    )
    + '";\n'
    'var hq_str_fx_sgbpjpy="'
    + _row(bid="211.810", ask="211.830", name="英镑兑日元即期汇率")
    + '";\n'
    'var hq_str_fx_smxnjpy="";\n'
    'var hq_str_fx_susdcny="'
    + _row(
        bid="6.6708",
        ask="6.6709",
        prev="6.6720",
        open_="6.6715",
        high="6.6730",
        low="6.6700",
        last="6.6708",
        name="美元兑人民币即期汇率",
    )
    + '";\n'
    # ---------- 人民币参考汇率（v0.3.4，只展示不可交易） ----------
    'var hq_str_fx_seurcny="'
    + _row(bid="7.4847", ask="7.4870", name="欧元兑人民币即期汇率")
    + '";\n'
    'var hq_str_fx_sjpycny="'
    + _row(bid="0.042210", ask="0.042210", name="日元兑人民币即期汇率")
    + '";\n'
    'var hq_str_fx_sgbpcny="'
    + _row(bid="8.7400", ask="8.7420", name="英镑兑人民币即期汇率")
    + '";\n'
    'var hq_str_fx_saudcny="'
    + _row(bid="4.7950", ask="4.7980", name="澳元兑人民币即期汇率")
    + '";\n'
    'var hq_str_fx_shkdcny="'
    + _row(bid="0.85700", ask="0.85720", name="港元兑人民币即期汇率")
    + '";\n'
    'var hq_str_fx_scadcny="'
    + _row(bid="5.1350", ask="5.1370", name="加元兑人民币即期汇率")
    + '";\n'
    'var hq_str_fx_snzdcny="'
    + _row(bid="4.4380", ask="4.4410", name="新西兰元兑人民币即期汇率")
    + '";\n'
    'var hq_str_fx_stryjpy="3.1558,3.1565";\n'
)

ALL_CODES = ["USDJPY", "EURJPY", "EURUSD", "GBPJPY", "MXNJPY", "TRYJPY"]

#: 人民币参考对的全部代码（顺序固定，与 pairs.REFERENCE_PAIR_CODES 一致）。
REFERENCE_CODES = ["USDCNY", "EURCNY", "JPYCNY", "GBPCNY", "AUDCNY", "HKDCNY", "CADCNY", "NZDCNY"]

#: 参考对的新浪代码（与 8 个交易品种同一次请求）。
REFERENCE_SINA_CODES = [
    "fx_susdcny",
    "fx_seurcny",
    "fx_sjpycny",
    "fx_sgbpcny",
    "fx_saudcny",
    "fx_shkdcny",
    "fx_scadcny",
    "fx_snzdcny",
]


class TestParseRow(unittest.TestCase):
    """18 段字段映射（idx0 时间、1 bid、2 ask、3 昨收、5 开、6 高、7 低、8 最新、17 日期）。"""

    def test_field_mapping(self) -> None:
        row = parse_sina_row(_row())
        self.assertEqual(row.bid, Decimal("158.180"))
        self.assertEqual(row.ask, Decimal("158.190"))
        self.assertEqual(row.prev_close, Decimal("158.100"))
        self.assertEqual(row.day_open, Decimal("158.150"))
        self.assertEqual(row.day_high, Decimal("158.300"))
        self.assertEqual(row.day_low, Decimal("158.000"))
        self.assertEqual(row.last, Decimal("158.180"))
        self.assertEqual(len(row.raw_fields), 18)

    def test_quote_ts_interpreted_as_shanghai(self) -> None:
        row = parse_sina_row(_row(time="17:27:42", date="2026-11-06"))
        self.assertEqual(row.quote_ts, NOW)  # 17:27:42+08:00 → 09:27:42 UTC

    def test_invalid_numbers_become_none(self) -> None:
        row = parse_sina_row(_row(bid="", ask="oops", prev="--"))
        self.assertIsNone(row.bid)
        self.assertIsNone(row.ask)
        self.assertIsNone(row.prev_close)

    def test_invalid_date_yields_none_ts(self) -> None:
        row = parse_sina_row(_row(date="not-a-date"))
        self.assertIsNone(row.quote_ts)

    def test_too_few_fields_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_sina_row("3.1558,3.1565")
        self.assertIn("BAD_SINA_ROW", str(ctx.exception))


class TestParseSinaFx(unittest.TestCase):
    def test_parse_single_pair(self) -> None:
        quotes = parse_sina_fx(SINA_TEXT, NOW, seq=7, codes=["USDJPY"])
        self.assertEqual(list(quotes.keys()), ["USDJPY"])
        quote = quotes["USDJPY"]
        self.assertIsInstance(quote, QuoteSnapshot)
        self.assertEqual(quote.pair, "USDJPY")
        self.assertEqual(quote.bid, Decimal("158.180"))
        self.assertEqual(quote.ask, Decimal("158.190"))
        self.assertEqual(quote.mid, Decimal("158.185"))
        self.assertEqual(quote.quote_ts, NOW)
        self.assertEqual(quote.recv_ts, NOW)
        self.assertEqual(quote.seq, 7)
        self.assertEqual(quote.source, "sina")

    def test_parse_multiple_pairs_and_skip_unrequested(self) -> None:
        quotes = parse_sina_fx(SINA_TEXT, NOW, seq=0, codes=["USDJPY", "EURJPY", "EURUSD"])
        self.assertEqual(set(quotes.keys()), {"USDJPY", "EURJPY", "EURUSD"})
        self.assertNotIn("GBPJPY", quotes)  # 响应里有但未请求 → 忽略

    def test_empty_and_malformed_rows_skipped(self) -> None:
        quotes = parse_sina_fx(SINA_TEXT, NOW, seq=0, codes=ALL_CODES)
        self.assertNotIn("MXNJPY", quotes)  # 空 payload
        self.assertNotIn("TRYJPY", quotes)  # 字段不足 18
        self.assertIn("GBPJPY", quotes)

    def test_unknown_code_ignored(self) -> None:
        self.assertEqual(parse_sina_fx(SINA_TEXT, NOW, 0, ["XAUUSD"]), {})

    def test_computed_pair_keeps_phantom_spread(self) -> None:
        quotes = parse_sina_fx(SINA_TEXT, NOW, 0, ["EURJPY"])
        self.assertEqual(quotes["EURJPY"].bid, Decimal("176.830"))
        self.assertEqual(quotes["EURJPY"].ask, Decimal("176.920"))  # 9 pip 幻影点差原样保留

    def test_invalid_price_fields_saved_as_none(self) -> None:
        text = 'var hq_str_fx_susdjpy="' + _row(bid="", ask="bad") + '";'
        quote = parse_sina_fx(text, NOW, 0, ["USDJPY"])["USDJPY"]
        self.assertIsNone(quote.bid)
        self.assertIsNone(quote.ask)
        self.assertIsNone(quote.mid)

    def test_quote_ts_none_when_date_invalid(self) -> None:
        text = 'var hq_str_fx_susdjpy="' + _row(date="") + '";'
        quote = parse_sina_fx(text, NOW, 0, ["USDJPY"])["USDJPY"]
        self.assertIsNone(quote.quote_ts)


class TestIsFresh(unittest.TestCase):
    @staticmethod
    def _quote(ts: datetime | None) -> QuoteSnapshot:
        return QuoteSnapshot(
            pair="USDJPY",
            bid=Decimal("158.180"),
            ask=Decimal("158.190"),
            mid=Decimal("158.185"),
            quote_ts=ts,
            recv_ts=NOW,
        )

    def test_fresh_within_stale_seconds(self) -> None:
        self.assertTrue(is_fresh(self._quote(NOW - timedelta(seconds=30)), NOW, 60))
        self.assertTrue(is_fresh(self._quote(NOW), NOW, 60))

    def test_stale_when_older_than_threshold(self) -> None:
        self.assertFalse(is_fresh(self._quote(NOW - timedelta(seconds=61)), NOW, 60))

    def test_missing_ts_is_not_fresh(self) -> None:
        self.assertFalse(is_fresh(self._quote(None), NOW, 60))

    def test_small_future_skew_tolerated(self) -> None:
        self.assertTrue(is_fresh(self._quote(NOW + timedelta(seconds=5)), NOW, 60))
        self.assertFalse(is_fresh(self._quote(NOW + timedelta(seconds=120)), NOW, 60))

    def test_weekend_zombie_timestamp_is_stale(self) -> None:
        # 周末僵尸时间戳（周五 17:27 北京 → 周一时已 2 天多）→ 过期；
        # 是否对外报 CLOSED 由 execution_cost 先判（CLOSED 优先于 STALE）。
        monday = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
        self.assertFalse(is_fresh(self._quote(NOW), monday, 60))


class TestDecode(unittest.TestCase):
    def test_gb18030_decode(self) -> None:
        raw = SINA_TEXT.encode("gb18030")
        text = decode_sina_bytes(raw)
        self.assertIn("美元兑日元即期汇率", text)
        self.assertIn("fx_susdjpy", text)

    def test_decode_requires_bytes(self) -> None:
        with self.assertRaises(TypeError):
            decode_sina_bytes("not-bytes")  # type: ignore[arg-type]


class TestUsdcnyAnchor(unittest.TestCase):
    """USDCNY 只作人民币换算锚：能解析、能按需关闭，不进入可交易品种表。"""

    def test_parse_usdcny(self) -> None:
        quotes = parse_sina_fx(SINA_TEXT, NOW, seq=1, codes=["USDCNY"])
        self.assertEqual(list(quotes.keys()), ["USDCNY"])
        self.assertEqual(quotes["USDCNY"].bid, Decimal("6.6708"))
        self.assertEqual(quotes["USDCNY"].ask, Decimal("6.6709"))
        self.assertEqual(quotes["USDCNY"].mid, Decimal("6.67085"))

    def test_usdcny_not_tradable(self) -> None:
        import pairs

        self.assertNotIn("USDCNY", pairs.PAIRS)
        self.assertNotIn("USDCNY", pairs.DEFAULT_PAIR_CODES)
        self.assertIsNone(pairs.resolve_pair("USDCNY"))


class TestCnyReferenceQuotes(unittest.TestCase):
    """人民币参考汇率（v0.3.4）：只展示、不可交易；与 8 个品种同一次请求拉取。"""

    def test_reference_codes_and_sina_codes(self) -> None:
        import pairs

        self.assertEqual(pairs.REFERENCE_PAIR_CODES, REFERENCE_CODES)
        self.assertEqual(
            [pairs.REFERENCE_PAIRS[code].sina_code for code in REFERENCE_CODES],
            REFERENCE_SINA_CODES,
        )
        for code in REFERENCE_CODES:
            reference = pairs.REFERENCE_PAIRS[code]
            self.assertEqual(reference.digits, 4, code)
            self.assertEqual(reference.name_cn.count("/"), 1, code)

    def test_jpy_reference_scaled_by_100(self) -> None:
        import pairs

        reference = pairs.REFERENCE_PAIRS["JPYCNY"]
        self.assertEqual(reference.scale, 100)
        self.assertEqual(reference.sina_code, "fx_sjpycny")
        for code in ("USDCNY", "EURCNY", "GBPCNY", "AUDCNY"):
            self.assertEqual(pairs.REFERENCE_PAIRS[code].scale, 1, code)

    def test_reference_not_tradable(self) -> None:
        import pairs

        for code in REFERENCE_CODES:
            self.assertNotIn(code, pairs.PAIRS, code)
            self.assertNotIn(code, pairs.DEFAULT_PAIR_CODES, code)
            self.assertIsNone(pairs.resolve_pair(code), code)
            self.assertIsNone(pairs.resolve_pair(pairs.REFERENCE_PAIRS[code].name_cn), code)
        # 配置白名单里写参考对 → 忽略（不会变成可下单品种）
        self.assertEqual(pairs.pair_codes_from_config(["USDCNY", "JPYCNY"]), [])
        self.assertEqual(
            list(pairs.pairs_from_config(["USDCNY", "USDJPY"])), ["USDJPY"]
        )

    def test_get_reference_accepts_code_name_and_sina_code(self) -> None:
        import pairs

        self.assertIsNotNone(pairs.get_reference("JPYCNY"))
        self.assertIsNotNone(pairs.get_reference("jpycny"))
        self.assertIsNotNone(pairs.get_reference("日元/人民币"))
        self.assertIsNotNone(pairs.get_reference("fx_sjpycny"))
        self.assertIsNone(pairs.get_reference("USDJPY"))
        self.assertIsNone(pairs.get_reference("XAUUSD"))

    def test_parse_reference_rows(self) -> None:
        quotes = parse_sina_fx(SINA_TEXT, NOW, seq=2, codes=REFERENCE_CODES)
        self.assertEqual(set(quotes.keys()), set(REFERENCE_CODES))
        self.assertEqual(quotes["EURCNY"].bid, Decimal("7.4847"))
        self.assertEqual(quotes["EURCNY"].ask, Decimal("7.4870"))
        self.assertEqual(quotes["JPYCNY"].bid, Decimal("0.042210"))
        self.assertEqual(quotes["HKDCNY"].bid, Decimal("0.85700"))
        self.assertEqual(quotes["NZDCNY"].ask, Decimal("4.4410"))
        for code in REFERENCE_CODES:
            self.assertEqual(quotes[code].source, "sina", code)


class TestQuoteSource(unittest.TestCase):
    """抓取路径：``_fetch_bytes`` 被替换成 fixture，绝不联网。"""

    def test_build_url(self) -> None:
        source = QuoteSource(timeout_seconds=8)
        url = source.build_url(["USDJPY", "EURJPY", "EURUSD"])
        # 默认顺带拉 USDCNY 换算锚 + 8 个人民币参考对（都不是可交易品种）
        sina = ["fx_susdjpy", "fx_seurjpy", "fx_seurusd", *REFERENCE_SINA_CODES]
        self.assertEqual(url, "https://hq.sinajs.cn/list=" + ",".join(sina))

    def test_build_url_without_usdcny(self) -> None:
        source = QuoteSource(timeout_seconds=8, include_usdcny=False)
        url = source.build_url(["USDJPY", "EURJPY"])
        # 换算锚关了，但参考对里仍含 fx_susdcny（参考栏要用）
        self.assertIn("fx_susdcny", url)
        self.assertEqual(url.count("fx_susdcny"), 1)

    def test_build_url_without_reference(self) -> None:
        source = QuoteSource(timeout_seconds=8, include_cny_reference=False)
        url = source.build_url(["USDJPY", "EURJPY"])
        self.assertEqual(url, "https://hq.sinajs.cn/list=fx_susdjpy,fx_seurjpy,fx_susdcny")
        self.assertTrue(all(code not in url for code in REFERENCE_SINA_CODES[1:]))

    def test_build_url_usdcny_not_duplicated(self) -> None:
        source = QuoteSource()
        url = source.build_url(["USDJPY"])
        self.assertEqual(url.count("fx_susdcny"), 1)

    def test_requested_codes_dedup_and_order(self) -> None:
        source = QuoteSource()
        requested = source.requested_codes(["USDJPY", "EURJPY"])
        self.assertEqual(requested, ["USDJPY", "EURJPY", *REFERENCE_CODES])
        self.assertEqual(requested.count("USDCNY"), 1, "USDCNY 既是换算锚又是参考对，只能出现一次")
        no_ref = QuoteSource(include_cny_reference=False).requested_codes(["USDJPY"])
        self.assertEqual(no_ref, ["USDJPY", "USDCNY"])

    def test_build_url_unknown_pair(self) -> None:
        source = QuoteSource()
        with self.assertRaises(KeyError):
            source.build_url(["XAUUSD"])

    def test_build_url_empty_codes(self) -> None:
        source = QuoteSource(include_usdcny=False, include_cny_reference=False)
        with self.assertRaises(ValueError) as ctx:
            source.build_url([])
        self.assertIn("EMPTY_CODES", str(ctx.exception))

    def test_fetch_parses_fixture_bytes(self) -> None:
        source = QuoteSource(timeout_seconds=1)
        source._fetch_bytes = lambda url: SINA_TEXT.encode("gb18030")  # type: ignore[assignment]
        quotes = asyncio.run(source.fetch(["USDJPY", "EURJPY"], NOW, seq=3))
        self.assertEqual(set(quotes.keys()), {"USDJPY", "EURJPY", *REFERENCE_CODES})
        self.assertEqual(quotes["USDJPY"].recv_ts, NOW)
        self.assertEqual(quotes["USDJPY"].seq, 3)
        self.assertEqual(quotes["USDCNY"].bid, Decimal("6.6708"))
        self.assertEqual(quotes["JPYCNY"].ask, Decimal("0.042210"))

    def test_fetch_without_reference(self) -> None:
        source = QuoteSource(timeout_seconds=1, include_cny_reference=False)
        source._fetch_bytes = lambda url: SINA_TEXT.encode("gb18030")  # type: ignore[assignment]
        quotes = asyncio.run(source.fetch(["USDJPY", "EURJPY"], NOW, seq=4))
        self.assertEqual(set(quotes.keys()), {"USDJPY", "EURJPY", "USDCNY"})

    def test_fetch_without_usdcny(self) -> None:
        source = QuoteSource(timeout_seconds=1, include_usdcny=False, include_cny_reference=False)
        source._fetch_bytes = lambda url: SINA_TEXT.encode("gb18030")  # type: ignore[assignment]
        quotes = asyncio.run(source.fetch(["USDJPY", "EURJPY"], NOW, seq=4))
        self.assertEqual(set(quotes.keys()), {"USDJPY", "EURJPY"})

    def test_fetch_wraps_oserror_into_runtimeerror(self) -> None:
        source = QuoteSource(timeout_seconds=1)

        def _boom(url: str) -> bytes:
            raise OSError("network down")

        source._fetch_bytes = _boom  # type: ignore[assignment]
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(source.fetch(["USDJPY"], NOW, 1))
        self.assertIn("QUOTE_FETCH_FAILED", str(ctx.exception))

    def test_fetch_raises_on_empty_response(self) -> None:
        source = QuoteSource(timeout_seconds=1)
        source._fetch_bytes = lambda url: b'var hq_str_fx_susdjpy="";'  # type: ignore[assignment]
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(source.fetch(["USDJPY"], NOW, 1))
        self.assertIn("QUOTE_EMPTY_RESPONSE", str(ctx.exception))

    def test_invalid_timeout_rejected(self) -> None:
        with self.assertRaises(ValueError):
            QuoteSource(timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
