"""品种规格（PairSpec）与品种解析：麦麦炒外汇 P0。

职责
----
1. 定义 P0 支持的 8 个品种规格。口径来源：``tmp/forex-design/margin-swap-spec.md``
   Rev 1.1 §1.2（品种表）+ §4.2（基准点差/上限）+ §3.3（近似隔夜利息），
   以及 ``docs/INTERFACES.md`` 的「PairSpec 数据」节。
2. 提供「品种代码 / 中文名 / 常用简称」→ :class:`models.PairSpec` 的解析。
3. 按配置白名单生成品种列表（``pair_codes_from_config`` / ``pairs_from_config``）。
4. 提供 USDJPY / USDCNY 中间价（账户币种=虚拟元，两条汇率锚；USDCNY 不是可交易品种，
   只用于换算，不进入可交易列表）。
5. 定义**人民币参考汇率**（:data:`REFERENCE_PAIRS`，v0.3.4）：8 个只展示、**不可交易**的
   参考对（USDCNY/EURCNY/JPYCNY/GBPCNY/AUDCNY/HKDCNY/CADCNY/NZDCNY），只有名称/代码/
   小数位/展示倍数，没有点差上限/杠杆/隔夜利息；它们**不进入** ``PAIRS``、
   ``DEFAULT_PAIR_CODES``、``pairs_from_config`` 与下单校验。

约定
----
- 纯数据 + 纯函数，不触达 I/O，不 import ``maibot_sdk``（只依赖 ``models``）。
- 价格一律 ``Decimal``；``pip`` 仅用于显示与点差单位，账务靠 ``tick`` 取整。
- 异常消息带原因码：未知代码 → ``KeyError("UNKNOWN_PAIR: ...")``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fxconv import USDCNY_CODE, USDCNY_SINA_CODE
from models import PairSpec, QuoteSnapshot

# ---------------------------------------------------------------------------
# 品种规格（8 品种）
# ---------------------------------------------------------------------------
# 字段顺序与 models.PairSpec 一致：
# code / name_cn / digits / tick / pip / quote_currency / sina_code / source_kind
# / base_spread_pips / max_spread_pips / swap_long_jpy / swap_short_jpy / max_units
#
# source_kind：
#   "raw"      —— 新浪直接给出有效 bid/ask（USDJPY / EURUSD）
#   "computed" —— 交叉盘由源"计算"得出，买卖价可能是 0 点差或幻影点差，
#                 点差必须由 execution_cost 按基准合成（EURJPY/GBPJPY/AUDJPY/
#                 TRYJPY/MXNJPY/ZARJPY）
#
# 高金利货币（TRY/MXN/ZAR）digits=4、tick=0.0001，但 pip 仍按设计约定取 0.01
# （即 1 pip = 100 tick 的"显示 pip"），见 Rev 1.1 §1.2 注。
_PAIR_LIST: list[PairSpec] = [
    PairSpec(
        code="USDJPY",
        name_cn="美元/日元",
        digits=3,
        tick=Decimal("0.001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_susdjpy",
        source_kind="raw",
        base_spread_pips=Decimal("0.3"),
        max_spread_pips=Decimal("8.0"),
        swap_long_jpy=Decimal("160"),
        swap_short_jpy=Decimal("-210"),
        max_units=100_000,
    ),
    PairSpec(
        code="EURJPY",
        name_cn="欧元/日元",
        digits=3,
        tick=Decimal("0.001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_seurjpy",
        source_kind="computed",
        base_spread_pips=Decimal("0.7"),
        max_spread_pips=Decimal("5.0"),
        swap_long_jpy=Decimal("80"),
        swap_short_jpy=Decimal("-130"),
        max_units=100_000,
    ),
    PairSpec(
        code="GBPJPY",
        name_cn="英镑/日元",
        digits=3,
        tick=Decimal("0.001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_sgbpjpy",
        source_kind="computed",
        base_spread_pips=Decimal("1.0"),
        max_spread_pips=Decimal("6.0"),
        swap_long_jpy=Decimal("185"),
        swap_short_jpy=Decimal("-250"),
        max_units=100_000,
    ),
    PairSpec(
        code="AUDJPY",
        name_cn="澳元/日元",
        digits=3,
        tick=Decimal("0.001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_saudjpy",
        source_kind="computed",
        base_spread_pips=Decimal("0.7"),
        max_spread_pips=Decimal("5.0"),
        swap_long_jpy=Decimal("85"),
        swap_short_jpy=Decimal("-135"),
        max_units=100_000,
    ),
    PairSpec(
        code="TRYJPY",
        name_cn="土耳其里拉/日元",
        digits=4,
        tick=Decimal("0.0001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_stryjpy",
        source_kind="computed",
        base_spread_pips=Decimal("0.8"),
        max_spread_pips=Decimal("12.0"),
        swap_long_jpy=Decimal("33"),
        swap_short_jpy=Decimal("-60"),
        max_units=200_000,
    ),
    PairSpec(
        code="MXNJPY",
        name_cn="墨西哥比索/日元",
        digits=4,
        tick=Decimal("0.0001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_smxnjpy",
        source_kind="computed",
        base_spread_pips=Decimal("0.6"),
        max_spread_pips=Decimal("10.0"),
        swap_long_jpy=Decimal("18"),
        swap_short_jpy=Decimal("-38"),
        max_units=200_000,
    ),
    PairSpec(
        code="ZARJPY",
        name_cn="南非兰特/日元",
        digits=4,
        tick=Decimal("0.0001"),
        pip=Decimal("0.01"),
        quote_currency="JPY",
        sina_code="fx_szarjpy",
        source_kind="computed",
        base_spread_pips=Decimal("0.6"),
        max_spread_pips=Decimal("10.0"),
        swap_long_jpy=Decimal("16"),
        swap_short_jpy=Decimal("-35"),
        max_units=200_000,
    ),
    PairSpec(
        code="EURUSD",
        name_cn="欧元/美元",
        digits=5,
        tick=Decimal("0.00001"),
        pip=Decimal("0.0001"),
        quote_currency="USD",
        sina_code="fx_seurusd",
        source_kind="raw",
        base_spread_pips=Decimal("0.5"),
        max_spread_pips=Decimal("8.0"),
        swap_long_jpy=Decimal("-101"),
        swap_short_jpy=Decimal("55"),
        max_units=100_000,
    ),
]

#: 品种代码 → 规格（只读；调用方不得修改其中的 PairSpec）。
PAIRS: dict[str, PairSpec] = {spec.code: spec for spec in _PAIR_LIST}

#: 默认品种列表（= 全部 8 品种，顺序固定，供调度层轮询）。
DEFAULT_PAIR_CODES: list[str] = [spec.code for spec in _PAIR_LIST]


# ---------------------------------------------------------------------------
# 人民币参考汇率（只展示、不可交易；v0.3.4）
# ---------------------------------------------------------------------------
#: 参考栏「点差」的显示单位（1 个显示点 = 0.001）：只为可读性，**不参与撮合**。
#: 例：USDCNY 6.6708 / 6.6709 → 点差 0.1；EURCNY 7.4847 / 7.4870 → 点差 2.3。
REFERENCE_PIP = Decimal("0.001")


@dataclass(frozen=True)
class ReferencePair:
    """人民币参考汇率（**只展示、不可交易**）：没有交易规则，只够渲染一张卡。

    与 :class:`models.PairSpec` 的区别：没有 tick / 点差上限 / 杠杆 / 隔夜利息 / 数量上限，
    因此**不能**用于下单、风控、账务或 ``pairs_from_config``。
    """

    code: str
    name_cn: str
    sina_code: str
    #: 展示小数位（参考栏统一 4 位）。
    digits: int = 4
    #: 展示倍数：``JPYCNY`` 按「100 日元 = X 元」显示（0.0422 太难读），其余为 1。
    scale: int = 1
    #: 点差的显示单位（见 :data:`REFERENCE_PIP`；不参与撮合）。
    pip: Decimal = REFERENCE_PIP


#: 参考对（顺序即卡片顺序；新浪代码 2026-11-10 实测可用，与 8 个交易品种同一次请求拉取）。
#: 说明：``fx_susdcnh``（离岸人民币）实测也可用，但它是另一套价（USD/CNH），为避免卡面太长
#: 与「在岸/离岸」口径混淆，**默认不展示**（要加只需在下面追加一行）。
_REFERENCE_LIST: list[ReferencePair] = [
    ReferencePair(code=USDCNY_CODE, name_cn="美元/人民币", sina_code=USDCNY_SINA_CODE),
    ReferencePair(code="EURCNY", name_cn="欧元/人民币", sina_code="fx_seurcny"),
    ReferencePair(code="JPYCNY", name_cn="日元/人民币", sina_code="fx_sjpycny", scale=100),
    ReferencePair(code="GBPCNY", name_cn="英镑/人民币", sina_code="fx_sgbpcny"),
    ReferencePair(code="AUDCNY", name_cn="澳元/人民币", sina_code="fx_saudcny"),
    ReferencePair(code="HKDCNY", name_cn="港元/人民币", sina_code="fx_shkdcny"),
    ReferencePair(code="CADCNY", name_cn="加元/人民币", sina_code="fx_scadcny"),
    ReferencePair(code="NZDCNY", name_cn="新西兰元/人民币", sina_code="fx_snzdcny"),
]

#: 参考对代码 → 规格（只读；不进 ``PAIRS``，下单/风控查不到）。
REFERENCE_PAIRS: dict[str, ReferencePair] = {ref.code: ref for ref in _REFERENCE_LIST}

#: 参考对代码列表（顺序固定，供行情层拼请求）。
REFERENCE_PAIR_CODES: list[str] = [ref.code for ref in _REFERENCE_LIST]


def reference_pairs() -> list[ReferencePair]:
    """参考对列表（副本；顺序固定，调用方不得修改其中的 :class:`ReferencePair`）。"""

    return list(_REFERENCE_LIST)


# ---------------------------------------------------------------------------
# 别名表（代码 / 中文全名 / 简称 / 新浪代码 → 标准代码）
# ---------------------------------------------------------------------------
_ALIASES: dict[str, list[str]] = {
    "USDJPY": ["美元日元", "美元/日元", "美日", "美元兑日元", "美元日元", "usd/jpy"],
    "EURJPY": ["欧元日元", "欧元/日元", "欧日", "欧元兑日元", "欧元日元", "eur/jpy"],
    "GBPJPY": ["英镑日元", "英镑/日元", "镑日", "英日", "英镑兑日元", "英镑日元", "gbp/jpy"],
    "AUDJPY": ["澳元日元", "澳元/日元", "澳日", "澳元兑日元", "豪美元日元", "aud/jpy"],
    "TRYJPY": ["土耳其里拉日元", "土耳其里拉/日元", "土日", "里拉日元", "土耳其里拉日元", "try/jpy"],
    "MXNJPY": ["墨西哥比索日元", "墨西哥比索/日元", "墨日", "比索日元", "墨西哥比索日元", "mxn/jpy"],
    "ZARJPY": ["南非兰特日元", "南非兰特/日元", "南日", "兰特日元", "南非兰特日元", "zar/jpy"],
    "EURUSD": ["欧元美元", "欧元/美元", "欧美", "欧/美", "欧元兑美元", "欧元美元", "eur/usd"],
}


def _normalize(text: str) -> str:
    """归一化品种文本：去空白/斜杠/连字符/下划线，统一大写（中文不受影响）。"""
    cleaned = text.strip()
    for ch in (" ", "\t", "\u3000", "/", "／", "-", "_", "　"):
        cleaned = cleaned.replace(ch, "")
    return cleaned.upper()


# 建立 归一化别名 → 标准代码 的索引（含代码本身、中文名、新浪代码）。
_ALIAS_INDEX: dict[str, str] = {}
for _spec in _PAIR_LIST:
    for _text in [_spec.code, _spec.name_cn, _spec.sina_code, *_ALIASES[_spec.code]]:
        _ALIAS_INDEX[_normalize(_text)] = _spec.code

# 参考对别名索引（只给 ``get_reference`` 用，**不并入** ``_ALIAS_INDEX``，
# 否则参考对就会变成可解析品种，进而被配置白名单/下单误用）。
_REFERENCE_ALIAS_INDEX: dict[str, str] = {}
for _ref in _REFERENCE_LIST:
    for _text in (_ref.code, _ref.name_cn, _ref.sina_code):
        _REFERENCE_ALIAS_INDEX[_normalize(_text)] = _ref.code


# ---------------------------------------------------------------------------
# 解析函数
# ---------------------------------------------------------------------------
def get_pair(code: str) -> PairSpec:
    """按标准品种代码取规格；未知代码抛 ``KeyError``（原因码 ``UNKNOWN_PAIR``）。"""
    if not isinstance(code, str):
        raise KeyError(f"UNKNOWN_PAIR: {code!r}")
    key = _normalize(code)
    spec = PAIRS.get(key)
    if spec is None:
        raise KeyError(f"UNKNOWN_PAIR: {code!r}")
    return spec


def resolve_pair(text: str) -> PairSpec | None:
    """把用户输入解析为 :class:`PairSpec`；无法识别返回 ``None``。

    支持三类写法（大小写、空白、斜杠、全角斜杠均可容忍）：

    - 标准代码：``"USDJPY"`` / ``"usdjpy"`` / ``"USD/JPY"``
    - 中文全名：``"美元日元"`` / ``"美元/日元"`` / ``"欧元/美元"``
    - 常用简称：``"美日"`` / ``"欧日"`` / ``"镑日"`` / ``"澳日"`` / ``"欧美"`` 等

    也接受新浪代码 ``"fx_susdjpy"``（方便排障）。
    """
    if not isinstance(text, str):
        return None
    key = _normalize(text)
    if not key:
        return None
    if key in PAIRS:
        return PAIRS[key]
    code = _ALIAS_INDEX.get(key)
    if code is None:
        return None
    return PAIRS[code]


def get_reference(code: str) -> ReferencePair | None:
    """按标准代码 / 中文名 / 新浪代码取**参考对**；不是参考对返回 ``None``（不抛异常）。

    注意：参考对**不是**可交易品种——:func:`resolve_pair` / :func:`pairs_from_config`
    查不到它们（本函数与它们互不影响）。
    """
    if not isinstance(code, str):
        return None
    key = _normalize(code)
    if not key:
        return None
    reference = REFERENCE_PAIRS.get(key)
    if reference is not None:
        return reference
    ref_code = _REFERENCE_ALIAS_INDEX.get(key)
    if ref_code is None:
        return None
    return REFERENCE_PAIRS.get(ref_code)


def pair_codes_from_config(configured: list[str]) -> list[str]:
    """把配置中的品种白名单规范成标准代码列表。

    规则（与 Lead 约定一致）：

    - 入参为空（或 ``None``）→ 返回 :data:`DEFAULT_PAIR_CODES` 的副本（全部 8 品种）。
    - 否则逐个 :func:`resolve_pair`，**未知项忽略**，保持原顺序并去重。
    - 注意：非空但全部未知时返回空列表（由调用方决定是否回退默认，避免悄悄吞掉配置错误）。
    - 人民币参考对（:data:`REFERENCE_PAIRS`）**不是**可交易品种，写进配置也只会被忽略。
    """
    if not configured:
        return list(DEFAULT_PAIR_CODES)
    codes: list[str] = []
    for item in configured:
        spec = resolve_pair(str(item))
        if spec is not None and spec.code not in codes:
            codes.append(spec.code)
    return codes


def pairs_from_config(configured: list[str]) -> dict[str, PairSpec]:
    """按配置白名单构造 ``{code: PairSpec}``（保持配置顺序）。

    规则：空配置 = 全部默认 8 品种；未知代码忽略；结果可为空（若配置项全部非法）。
    人民币参考对不在 ``PAIRS`` 里，因此**永远不会**出现在返回的可交易品种表中。
    """
    if not configured:
        return {code: PAIRS[code] for code in DEFAULT_PAIR_CODES}
    result: dict[str, PairSpec] = {}
    for item in configured:
        spec = resolve_pair(str(item))
        if spec is not None and spec.code not in result:
            result[spec.code] = spec
    return result


def usdjpy_mid(quotes: dict[str, QuoteSnapshot]) -> Decimal | None:
    """返回 USDJPY 中间价（市场数据；账户换算请用 ``fxconv.rates_from_quotes``）；不可用返回 ``None``。

    优先用快照自带的 ``mid``；缺失时用 ``(bid + ask) / 2`` 现场计算；
    bid/ask 缺失、非正或价格为 ``None`` 时返回 ``None``（调用方按"USDJPY 不可用"处理）。
    """
    return quote_mid(quotes, "USDJPY")


def usdcny_mid(quotes: dict[str, QuoteSnapshot]) -> Decimal | None:
    """返回 USDCNY 中间价（人民币换算锚，不是可交易品种）；不可用返回 ``None``。"""

    return quote_mid(quotes, "USDCNY")


def quote_mid(quotes: dict[str, QuoteSnapshot], code: str) -> Decimal | None:
    """取某个代码的中间价（``mid`` 优先，其次 ``(bid+ask)/2``）；不可用返回 ``None``。"""

    quote = quotes.get(code)
    if quote is None:
        return None
    mid = quote.mid
    if mid is not None and mid > 0:
        return mid
    bid, ask = quote.bid, quote.ask
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / Decimal(2)
