"""新浪外汇行情抓取与解析（P0 唯一真行情源）。

数据源（``tmp/forex-design/forex-domain.md`` §3.2 实测）
--------------------------------------------------------
- 端点：``https://hq.sinajs.cn/list=fx_susdjpy,fx_seurjpy,...``（GB18030 编码）
- **必须**带 ``Referer: https://finance.sina.com.cn``（不带 → 403）
- 单次可拼多个代码；响应形如 ``var hq_str_fx_susdjpy="...";``，引号内 18 字段：
  idx0 行情时间 ``HH:MM:SS``（北京）/ idx1 bid / idx2 ask / idx3 昨收 /
  idx4 量（不可用）/ idx5 今开 / idx6 今日最高 / idx7 今日最低 / idx8 最新价 /
  idx9 中文名 / idx10 涨跌幅 / idx11 涨跌额 / idx12 振幅（口径不定，不可用）/
  idx13 备注（"此行情由新浪财经计算得出"=合成价）/ idx14 52 周高 / idx15 52 周低 /
  idx16 空 / idx17 日期 ``YYYY-MM-DD``。
- 计算品种（交叉盘）**买卖价可能相等或出现幻影点差**（两腿不同步），本模块原样保存
  bid/ask，点差由 :mod:`execution_cost` 按基准合成，**不要**在这里修正。

状态优先级（重要，调用方请照此实现）
------------------------------------
``quote_usable`` 的判定顺序是 **CLOSED 优先于 STALE**：
周末/休市时新浪常返回"僵尸时间戳"（周五或更早的日期），时间戳检查一定会判
STALE，但正确的对外语义是 **CLOSED**（休市），不是行情过期。因此
:mod:`execution_cost` 先判 ``is_market_open``，再判 :func:`is_fresh`；
本模块的 :func:`is_fresh` 只负责"时间戳新鲜度"，不感知交易日历。

接口
----
- :class:`QuoteSource`           异步抓取（``urllib.request`` + ``asyncio.to_thread``）
- :func:`parse_sina_fx`          纯函数解析（fixture 可离线单测）
- :func:`parse_sina_row`         单行字段映射（纯函数，暴露 idx 语义便于排障）
- :func:`is_fresh`               时间戳新鲜度判定

换算锚（v0.3.0）与人民币参考汇率（v0.3.4）
------------------------------------------
账户币种是虚拟人民币（元），需要两条真实行情锚：``USDJPY``（8 个品种里已有）与
``USDCNY``（在岸人民币，新浪代码 ``fx_susdcny``，不是可交易品种）。``QuoteSource`` 在
``include_usdcny=True``（默认）时会把 ``fx_susdcny`` 拼进同一次请求，解析结果里多一个
``USDCNY`` 快照（``[market] usdcny_enabled`` 可关闭）。

v0.3.4 起，``include_cny_reference=True``（默认，对应 ``[market] cny_reference_enabled``）时
还会把 8 个**只展示、不可交易**的人民币参考对（:data:`pairs.REFERENCE_PAIRS`）拼进**同一次**
请求：``fx_susdcny / fx_seurcny / fx_sjpycny / fx_sgbpcny / fx_saudcny / fx_shkdcny /
fx_scadcny / fx_snzdcny``（GB18030 + 18 字段解析逻辑与交易品种完全一致，复用
:func:`parse_sina_fx`）。参考对只是多几个快照键，**不会**进入 ``pairs_from_config``、
可交易列表或下单校验。

异常：抓取/解码/解析失败统一抛 ``RuntimeError``，消息带原因码（``QUOTE_FETCH_FAILED`` /
``QUOTE_EMPTY_RESPONSE`` 等）。本模块不 import ``maibot_sdk``。
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fxconv import USDCNY_CODE, USDCNY_SINA_CODE
from models import QuoteSnapshot
from pairs import REFERENCE_PAIR_CODES, get_pair, get_reference, reference_pairs

#: 新浪行情端点模盘口（``{codes}`` 为逗号分隔的 ``fx_*`` 代码）。
SINA_URL_TEMPLATE = "https://hq.sinajs.cn/list={codes}"

#: 必带 Referer，否则新浪返回 403（实测）。
SINA_REFERER = "https://finance.sina.com.cn"

#: 普通浏览器 UA（新浪对默认 urllib UA 可能不友好，带上更稳）。
SINA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 默认超时（秒），与 ``config.MarketConfig.quote_timeout_seconds`` 默认值一致。
DEFAULT_TIMEOUT_SECONDS = 8

#: 新浪外汇字段数（18 段）。
SINA_FX_FIELD_COUNT = 18

#: 行情时间无时区 → 按北京时间解释（新浪服务器时间为北京时间）。
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

#: 匹配 ``var hq_str_<code>="<payload>";``（贪婪度受引号限制，兼容中文名）。
_SINA_LINE_RE = re.compile(r'var\s+hq_str_([A-Za-z0-9_]+)\s*=\s*"([^"]*)"')

#: 换算锚：不在可交易品种表里，但允许通过行情源一起拉取（代码 → 新浪代码）。
_CONVERSION_SINA_CODES: dict[str, str] = {USDCNY_CODE: USDCNY_SINA_CODE}


def _sina_code_for(code: str) -> str | None:
    """标准代码 → 新浪代码：换算锚 → 人民币参考对 → 8 个交易品种；未知返回 ``None``。"""

    if code in _CONVERSION_SINA_CODES:
        return _CONVERSION_SINA_CODES[code]
    reference = get_reference(code)
    if reference is not None:
        return reference.sina_code
    try:
        return get_pair(code).sina_code
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# 解析：单行字段映射
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SinaFxRow:
    """新浪 ``fx_s*`` 一行的字段映射（18 段，见模块 docstring）。"""

    bid: Decimal | None
    ask: Decimal | None
    prev_close: Decimal | None
    day_open: Decimal | None
    day_high: Decimal | None
    day_low: Decimal | None
    last: Decimal | None
    quote_ts: datetime | None
    raw_fields: tuple[str, ...]


def _to_decimal(text: str) -> Decimal | None:
    """字段文本 → Decimal；空串/非法返回 ``None``（不抛异常，交给 quote_usable 判脏数据）。"""
    value = text.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _parse_quote_ts(time_text: str, date_text: str) -> datetime | None:
    """把 ``HH:MM:SS`` + ``YYYY-MM-DD`` 组合成 UTC aware datetime（无时区→按北京时间）。

    任一部分缺失/非法返回 ``None``（周末僵尸时间戳的判定交给调用方：CLOSED 优先）。
    """
    time_part = time_text.strip()
    date_part = date_text.strip()
    if not time_part or not date_part:
        return None
    try:
        local = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return local.replace(tzinfo=SHANGHAI_TZ).astimezone(timezone.utc)


def parse_sina_row(payload: str) -> SinaFxRow:
    """解析引号内的 18 段（纯函数）；字段不足抛 ``ValueError("BAD_SINA_ROW")``。"""
    fields = payload.split(",")
    if len(fields) < SINA_FX_FIELD_COUNT:
        raise ValueError(
            f"BAD_SINA_ROW: 期望至少 {SINA_FX_FIELD_COUNT} 段，实际 {len(fields)} 段: {payload[:64]!r}"
        )
    return SinaFxRow(
        bid=_to_decimal(fields[1]),
        ask=_to_decimal(fields[2]),
        prev_close=_to_decimal(fields[3]),
        day_open=_to_decimal(fields[5]),
        day_high=_to_decimal(fields[6]),
        day_low=_to_decimal(fields[7]),
        last=_to_decimal(fields[8]),
        quote_ts=_parse_quote_ts(fields[0], fields[17]),
        raw_fields=tuple(fields),
    )


# ---------------------------------------------------------------------------
# 解析：整段响应 → QuoteSnapshot 字典
# ---------------------------------------------------------------------------
def parse_sina_fx(
    text: str,
    recv_ts: datetime,
    seq: int,
    codes: list[str],
) -> dict[str, QuoteSnapshot]:
    """解析新浪响应文本为 ``{品种代码: QuoteSnapshot}``（纯函数，可离线单测）。

    参数
    ----
    text     : ``var hq_str_fx_xxx="...";`` 形式的原始文本（已解码为 str）
    recv_ts  : 插件收到行情的时刻（UTC aware；naive 按 UTC 处理）
    seq      : 本次抓取序号（同一 tick 的多个品种共享）
    codes    : 本次**请求**的品种代码（标准代码，如 ``["USDJPY", "EURUSD"]``）；
               响应里多余的行会被忽略，未知代码直接跳过

    行为
    ----
    - 空 payload（``""``）、字段不足、无法解析的行 → 跳过（不中断其他品种）。
    - bid/ask 非法 → 对应字段为 ``None``（由 ``quote_usable`` 判 ``QUOTE_MISSING``）。
    - ``quote_ts`` 由 idx17 日期 + idx0 时间组合，按 ``Asia/Shanghai`` 解释后转 UTC；
      数据日期是否当日由调用方结合 ``is_fresh`` / ``CLOSED`` 判定。
    """
    if recv_ts.tzinfo is None or recv_ts.utcoffset() is None:
        recv_ts = recv_ts.replace(tzinfo=timezone.utc)

    # 请求的饮品代码 → 新浪代码（未知代码忽略）
    wanted: dict[str, str] = {}
    for code in codes:
        sina = _sina_code_for(str(code))
        if sina is None:
            continue
        wanted[sina] = str(code)

    quotes: dict[str, QuoteSnapshot] = {}
    for match in _SINA_LINE_RE.finditer(text):
        sina_code, payload = match.group(1), match.group(2)
        pair_code = wanted.get(sina_code)
        if pair_code is None or not payload.strip():
            continue
        try:
            row = parse_sina_row(payload)
        except ValueError:
            continue
        bid, ask = row.bid, row.ask
        mid = (bid + ask) / Decimal(2) if bid is not None and ask is not None else None
        quotes[pair_code] = QuoteSnapshot(
            pair=pair_code,
            bid=bid,
            ask=ask,
            mid=mid,
            quote_ts=row.quote_ts,
            recv_ts=recv_ts,
            source="sina",
            seq=seq,
        )
    return quotes


# ---------------------------------------------------------------------------
# 新鲜度
# ---------------------------------------------------------------------------
def is_fresh(quote: QuoteSnapshot, now: datetime, stale_seconds: int) -> bool:
    """行情快照是否新鲜：``|now − quote_ts| <= stale_seconds``。

    - ``quote_ts`` 为 ``None``（源无时间戳）→ ``False``（不能确认新鲜即视为过期）。
    - 允许源时钟小幅超前：负龄（``quote_ts`` 在 ``now`` 之后）按绝对值比较；
      超前超过 ``stale_seconds`` 视为异常 → ``False``。
    - **注意**：本函数不判休市。周末僵尸时间戳也会返回 ``False``，调用方必须
      **先判 CLOSED 再判 STALE**（见模块 docstring）。
    """
    if quote.quote_ts is None:
        return False
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    quote_ts = quote.quote_ts
    if quote_ts.tzinfo is None or quote_ts.utcoffset() is None:
        return False
    age = (now - quote_ts).total_seconds()
    return abs(age) <= stale_seconds


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def decode_sina_bytes(raw: bytes) -> str:
    """按 GB18030 解码新浪响应（UTF-8 会出替换符；GB18030 兼容 GBK）。"""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError(f"INVALID_RESPONSE_TYPE: 需要 bytes，收到 {type(raw).__name__}")
    return bytes(raw).decode("gb18030", errors="replace")


class QuoteSource:
    """新浪行情源：``urllib.request`` 在 ``asyncio.to_thread`` 里跑，避免阻塞事件循环。

    典型用法（调度层）::

        source = QuoteSource(timeout_seconds=cfg.market.quote_timeout_seconds)
        quotes = await source.fetch(codes, now_utc(), seq)

    失败语义：网络/HTTP/解码/空结果 → ``RuntimeError``（消息带原因码），
    由调度层"保留上一 tick 快照 + 标记 stale"降级（P0 不做备源）。
    """

    def __init__(
        self,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        referer: str = SINA_REFERER,
        user_agent: str = SINA_USER_AGENT,
        include_usdcny: bool = True,
        include_cny_reference: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"INVALID_TIMEOUT: 超时必须为正数，收到 {timeout_seconds}")
        self.timeout_seconds = int(timeout_seconds)
        self.referer = referer
        self.user_agent = user_agent
        #: 是否顺带拉取 USDCNY 换算锚（对应 ``[market] usdcny_enabled``）。
        self.include_usdcny = bool(include_usdcny)
        #: 是否顺带拉取人民币参考汇率（对应 ``[market] cny_reference_enabled``；
        #: 只多几个快照键，不进可交易列表）。
        self.include_cny_reference = bool(include_cny_reference)

    # -- 请求代码列表 -------------------------------------------------------
    def requested_codes(self, codes: list[str]) -> list[str]:
        """本次实际请求的代码（交易品种 + 换算锚 + 人民币参考对；去重保序）。

        顺序固定：先调用方给的品种，再 ``USDCNY``（换算锚），最后按
        :data:`pairs.REFERENCE_PAIRS` 的顺序补参考对（含 USDCNY，已存在则跳过）。
        """

        requested = [str(code) for code in codes]
        if self.include_usdcny and USDCNY_CODE not in requested:
            requested.append(USDCNY_CODE)
        if self.include_cny_reference:
            for code in REFERENCE_PAIR_CODES:
                if code not in requested:
                    requested.append(code)
        return requested

    # -- URL ---------------------------------------------------------------
    def build_url(self, codes: list[str]) -> str:
        """按品种代码列表构造请求 URL（顺序与入参一致，未知代码抛 ``KeyError``）。"""
        sina_codes: list[str] = []
        for code in codes:
            sina = _sina_code_for(str(code))
            if sina is None:
                get_pair(code)  # 未知 → KeyError("UNKNOWN_PAIR: ...")
                continue
            if sina not in sina_codes:
                sina_codes.append(sina)
        if self.include_usdcny and USDCNY_SINA_CODE not in sina_codes:
            sina_codes.append(USDCNY_SINA_CODE)
        if self.include_cny_reference:
            for reference in reference_pairs():
                if reference.sina_code not in sina_codes:
                    sina_codes.append(reference.sina_code)
        if not sina_codes:
            raise ValueError("EMPTY_CODES: 行情代码列表为空")
        return SINA_URL_TEMPLATE.format(codes=",".join(sina_codes))

    # -- 同步 HTTP（在线程池里执行） ---------------------------------------
    def _fetch_bytes(self, url: str) -> bytes:
        """同步 GET，返回原始字节；任何网络/HTTP 异常转 ``RuntimeError``。"""
        request = urllib.request.Request(
            url,
            headers={
                "Referer": self.referer,
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"QUOTE_HTTP_ERROR: HTTP {exc.code} url={url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"QUOTE_FETCH_FAILED: {exc.reason} url={url}") from exc
        except TimeoutError as exc:  # 3.10+ 的 socket.timeout 别名
            raise RuntimeError(
                f"QUOTE_TIMEOUT: 超过 {self.timeout_seconds}s url={url}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"QUOTE_FETCH_FAILED: {exc} url={url}") from exc

    # -- 异步抓取 -----------------------------------------------------------
    async def fetch(
        self,
        codes: list[str],
        now: datetime,
        seq: int,
    ) -> dict[str, QuoteSnapshot]:
        """抓取并解析一批品种的行情快照。

        参数
        ----
        codes : 品种标准代码列表（空列表 → 使用全部默认 8 品种）
        now   : 本次抓取的"收到时刻"（UTC aware，写入 ``recv_ts``）
        seq   : 抓取序号（写入 ``QuoteSnapshot.seq``，引擎用于丢弃旧快照）

        返回 ``{品种代码: QuoteSnapshot}``；响应里一个品种都解析不到时抛
        ``RuntimeError("QUOTE_EMPTY_RESPONSE")``，网络失败抛 ``QUOTE_FETCH_FAILED`` 等。

        返回的字典里除可交易品种外，还可能有 ``USDCNY`` 换算锚与人民币参考对
        （``EURCNY``/``JPYCNY`` 等）；调用方应按 ``pairs_from_config`` 的白名单取可交易部分。
        """
        if not codes:
            from pairs import DEFAULT_PAIR_CODES

            codes = list(DEFAULT_PAIR_CODES)
        requested = self.requested_codes(codes)

        url = self.build_url(codes)
        try:
            raw = await asyncio.to_thread(self._fetch_bytes, url)
        except RuntimeError:
            raise
        except Exception as exc:  # 兜底：线程/未知异常也统一成 RuntimeError
            raise RuntimeError(f"QUOTE_FETCH_FAILED: {exc} url={url}") from exc

        text = decode_sina_bytes(raw)
        quotes = parse_sina_fx(text, now, seq, requested)
        if not quotes:
            raise RuntimeError(
                f"QUOTE_EMPTY_RESPONSE: 未解析到任何品种"
                f"（请求 {len(requested)} 个，字节 {len(raw)}）"
            )
        return quotes
