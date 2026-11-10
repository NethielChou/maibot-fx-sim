"""播报/报表文本（纯函数 + 中文卡片式排版，全部以 ``DISCLAIMER`` 收尾）。

卡片式排版约定（v0.3.2 起）
--------------------------
- **标题行**：``emoji 中文标题 · MM-DD HH:MM``（北京时间），标题下空一行；
- **内容行**：控制在 :data:`CARD_WIDTH`（52 个半角列 ≈ 26 个汉字）内，长文本由
  :func:`_wrap_text` 自动折行，一屏内可直接读；
- **分区**：分区之间用 :data:`CARD_DIVIDER`（13 个半角横线）单独一行；
- **小标题**：明细/理由/备注等块前用 emoji 小标题（``💬 理由`` / ``📌 规则`` / ``📭 空仓``）；
- **末行**：固定 :data:`DISCLAIMER`，单独一行；QQ 纯文本，不用 markdown 表格/代码块
  （等宽字体不可靠）。

数值口径
--------
- 金额统一走 ``fmt_money``（整数元 + 千分位），不显示浮点尾巴；
- 本模块**不碰交易 I/O**：只接收 ``AccountState`` / ``Position`` / ``Fill`` / ``QuoteSnapshot`` /
  ``dict`` 等对象并拼字符串；估值口径与账本一致（多按 bid、空按 ask；JPY/USD 金额按
  USDJPY、USDCNY 换算成元）；
- 供 scheduler 直接调用的事件播报：``fill_report`` / ``order_submitted`` / ``order_rejected`` /
  ``order_suspended`` / ``loss_cut_report`` / ``swap_report`` / ``decision_report`` /
  ``rate_unavailable_text`` / ``event_soon_text``；
- 供 ``/外汇`` 命令使用：``status`` / ``positions_list`` / ``pending_list`` / ``quotes_overview``
  / ``margin_overview`` / ``swap_list`` / ``record_list`` / ``help_text`` / ``card_notice``。

行情卡（v0.3.4）
----------------
``quotes_overview`` 分三段：**可交易**（配置白名单，逐行「买入 / 卖出 + 点差」）/
**人民币参考汇率**（8 个只展示、不可交易的参考对，4 位小数，日元按 100 日元折算；
``cny_reference=False`` 时整段隐藏）/ **📖 怎么看**（给不了解外汇的人看的说明）。
USDCNY 只出现在参考栏，**不进**可交易栏。
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

try:  # 宿主把插件当包加载时走相对导入
    from . import pairs as pairs_mod
except ImportError:  # 单测/脚本以顶层模块导入
    import pairs as pairs_mod  # type: ignore[no-redef]

DISCLAIMER = "【纯本地虚拟游戏】模拟盘，不构成投资建议"

#: 分区横线：13 个半角横线，单独占一行（卡片式排版固定分隔线）。
CARD_DIVIDER = "─" * 13
#: 单行最大显示宽度（半角列；≈ 26 个汉字），超出自动折行。
CARD_WIDTH = 52
#: 播报展示用固定时区：北京时间（UTC+8，无夏令时），与行情源（新浪）口径一致。
DISPLAY_TZ = timezone(timedelta(hours=8))
#: 品种货币符号（报价货币决定后半部分，如 USDJPY → ``$¥``）。
PAIR_SYMBOL = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "AUD": "A$",
    "TRY": "₺",
    "MXN": "MX$",
    "ZAR": "R",
    "CNY": "¥",
    "JPY": "¥",
}

FLAGS = {
    "USD": "🇺🇸",
    "EUR": "🇪🇺",
    "GBP": "🇬🇧",
    "AUD": "🇦🇺",
    "TRY": "🇹🇷",
    "MXN": "🇲🇽",
    "ZAR": "🇿🇦",
    "CNY": "🇨🇳",
    "JPY": "🇯🇵",
    "HKD": "🇭🇰",
    "CAD": "🇨🇦",
    "NZD": "🇳🇿",
}

#: 人民币参考汇率栏的固定小数位（v0.3.4；日元按 100 日元折算后同口径）。
REFERENCE_DIGITS = 4

#: 「📖 怎么看」说明（普通用户口径；刻意不用「买价/卖价」这类歧义缩写）。
HOWTO_LINES: tuple[str, ...] = (
    "📖 怎么看",
    "· 买入价 = 你买入要付的价格",
    "· 卖出价 = 你卖出能拿到的价格",
    "· 点差 = 买入价与卖出价的差，越小越便宜",
    "· 点差单位 pip：美元/日元 1 pip = 0.01 日元",
    "  （美元计价品种 1 pip = 0.0001 美元）",
    "· 例：美元/日元 158.030 / 158.040",
    "  点差 1.0 pip = 0.01 日元",
    "· 数据来自新浪公开行情，仅供参考",
)


ACTION_TEXT = {
    "open_long": "开多",
    "open_short": "开空",
    "close": "平仓",
    "close_all": "全部平仓",
    "hold": "观望",
}
ACTION_EMOJI = {
    "open_long": "🟢",
    "open_short": "🔴",
    "close": "🔵",
    "close_all": "🟣",
    "hold": "⏸",
}
SIDE_TEXT = {"buy": "买", "sell": "卖"}
POSITION_TYPE_TEXT = {"open": "新规", "close": "平仓"}
ORDER_TYPE_TEXT = {"market": "市价", "limit": "限价", "stop": "止损", "trailing_stop": "追踪止损"}
STATUS_TEXT = {
    "created": "已创建",
    "pending": "挂单中",
    "triggered": "已触发",
    "filled": "已成交",
    "cancelled": "已撤单",
    "expired": "已过期",
    "rejected": "已拒绝",
}
# 常见原因码 → 中文人话（报告用；不认识的直接原样显示）
REASON_TEXT = {
    "MARKET_CLOSED": "市场休市",
    "QUOTE_STALE": "行情过期",
    "SPREAD_TOO_WIDE": "点差过宽",
    "LIQUIDITY_ANOMALY": "行情跳变异常",
    "NEWS_BLACKOUT": "经济指标静默期",
    "ACCOUNT_HALTED": "账户已停手",
    "INSUFFICIENT_MARGIN": "保证金不足",
    "INVALID_UNITS": "数量不合法（须为 1,000 的整数倍）",
    "INVALID_PRICE_TICK": "价格未按最小tick（tick）对齐",
    "PRICE_OUT_OF_RANGE": "价格越界",
    "POSITION_NOT_FOUND": "找不到可平仓的持仓",
    "CLOSE_UNITS_EXCEED": "平仓数量超过持仓",
    "HEDGE_NOT_ALLOWED": "不允许双向持仓",
    "MAX_OPEN_ORDERS": "挂单数量已达上限",
    "SLIPPAGE_TOO_LARGE": "滑点过大",
    "DRIFT_TOO_LARGE": "决策价与执行价偏离过大",
    "NOT_MODIFIABLE": "该订单当前状态不可修改",
    "USER_CANCELLED": "用户撤单",
    "DAY_ROLLOVER": "日切过期",
    "GTD_EXPIRED": "到期",
    "RISK_CANCELLED": "风控撤单",
    "OCO_CANCELLED": "OCO 另一腿已成交",
    "LLM_FAILED": "模型调用失败",
    "LLM_TIMEOUT": "模型调用超时",
    "LLM_BUDGET_EXHAUSTED": "今日模型调用预算已用尽",
    "LLM_INVALID_OUTPUT": "模型输出无法解析",
    "INVALID_JSON": "输出不是合法 JSON",
    "INVALID_ACTION": "动作非法",
    "INVALID_PAIR": "品种非法",
    "INVALID_PRICE": "价格非法",
    "EVENT_WINDOW": "经济指标事件窗内禁止开新仓",
    "MAX_POSITIONS": "持仓数已达风控上限",
    "NANPIN_FORBIDDEN": "禁止对浮亏持仓同向加仓（逆势加仓）",
    "RATE_UNAVAILABLE": "汇率不可用（缺 USDCNY 或 USDJPY，已暂停估值/开仓）",
}


# ---------------------------------------------------------------------------
# 基础格式化
# ---------------------------------------------------------------------------


def append_disclaimer(text: str) -> str:
    """给文本末尾追加统一免责声明；已包含则不重复。"""

    body = str(text or "").rstrip()
    if not body:
        return DISCLAIMER
    if DISCLAIMER in body:
        return body
    return f"{body}\n{DISCLAIMER}"


def fmt_money(value: Any) -> str:
    """整数元 → ``1,234,567 元``（千分位，无小数）。"""

    try:
        return f"{int(value):,} 元"
    except (TypeError, ValueError):
        return "- 元"


def fmt_signed_money(value: Any) -> str:
    """有符号金额 → ``+1,000 元`` / ``-2,100 元``。"""

    try:
        return f"{int(value):+,} 元"
    except (TypeError, ValueError):
        return "- 元"


def fmt_price(value: Any) -> str:
    """价格 → 规范十进制字符串（None → ``-``）。"""

    if value is None:
        return "-"
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def fmt_pips(value: Any) -> str:
    """pips → 保留 2 位的字符串。"""

    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, ValueError):
        return "-"


def _units_text(units: Any) -> str:
    """数量 → ``1万单位`` / ``5,000 货币``。"""

    try:
        number = int(units)
    except (TypeError, ValueError):
        return f"{units}"
    if number % 10_000 == 0:
        return f"{number // 10_000}万单位"
    return f"{number:,} 货币"


def pair_name(pairs: dict[str, Any] | None, code: str) -> str:
    """只要中文名（卡片里不再重复代码）；查不到时按代码解析一次，再退化为代码。"""

    spec = (pairs or {}).get(str(code))
    name_cn = str(getattr(spec, "name_cn", "") or "") if spec is not None else ""
    if name_cn:
        return name_cn
    text = str(code or "").strip()
    if not text:
        return "-"
    try:
        resolved = pairs_mod.resolve_pair(text)
    except Exception:  # noqa: BLE001 - 名称解析失败不影响播报
        resolved = None
    return str(getattr(resolved, "name_cn", "") or text)


def _pair_symbol(pairs: dict[str, Any] | None, code: str) -> str:
    """货币对符号：``$¥`` / ``€¥`` / ``€$``（按品种的报价货币映射）。"""

    text = str(code or "").strip().upper()
    spec = (pairs or {}).get(text)
    quote_currency = str(getattr(spec, "quote_currency", "") or "") if spec is not None else ""
    if len(text) < 6:
        return ""
    base, quote = text[:3], (quote_currency or text[3:6])
    return f"{PAIR_SYMBOL.get(base, base)}{PAIR_SYMBOL.get(quote, quote)}"


def _pair_flags(pairs: dict[str, Any] | None, code: str) -> str:
    """货币对国旗：``🇺🇸🇯🇵``（按品种的基准/报价货币映射，查不到时用币种代码）。"""

    text = str(code or "").strip().upper()
    spec = (pairs or {}).get(text)
    quote_currency = str(getattr(spec, "quote_currency", "") or "") if spec is not None else ""
    if len(text) < 6:
        return ""
    base, quote = text[:3], (quote_currency or text[3:6])
    return f"{FLAGS.get(base, base)}{FLAGS.get(quote, quote)}"


def _price_text(value: Any, spec: Any = None) -> str:
    """价格按品种最小位数格式化（避免 ``212.4508100142`` 这类长尾数字）。"""

    if value is None:
        return "-"
    digits = getattr(spec, "digits", None) if spec is not None else None
    try:
        number = Decimal(str(value))
        if digits is not None:
            quantum = Decimal(1).scaleb(-int(digits))
            return format(number.quantize(quantum), "f")
        return format(number, "f")
    except (InvalidOperation, ValueError, TypeError):
        return fmt_price(value)


def _spec_of(pairs: dict[str, Any] | None, code: str) -> Any:
    """取品种规格；调用方给的 pairs 不完整时回退内置 PAIRS（保证价格位数正确）。"""

    code = str(code or "")
    spec = (pairs or {}).get(code)
    if spec is not None:
        return spec
    try:
        return pairs_mod.PAIRS.get(code.upper())
    except Exception:  # noqa: BLE001 - 查不到规格不影响播报
        return None


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _round_money(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# 卡片式排版工具（宽度 / 折行 / 标题 / 标签 / 分隔线）
# ---------------------------------------------------------------------------


def _char_width(char: str) -> int:
    """单个字符的显示宽度（半角 1 列、全角/宽字符 2 列、组合符 0 列）。"""

    if unicodedata.combining(char) or char in ("\ufe0f", "\u200d"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: Any) -> int:
    """文本显示宽度（半角列）：用于保证一行不超宽（QQ 一屏可直接读）。"""

    return sum(_char_width(ch) for ch in str(text or ""))


def _pad_right(text: Any, width: int) -> str:
    """按显示宽度右侧补空格（列对齐用；行尾空格会被 :func:`_wrap_text` 去掉）。"""

    value = str(text if text is not None else "")
    return value + " " * max(0, int(width) - display_width(value))


def _wrap_text(text: Any, width: int = CARD_WIDTH) -> list[str]:
    """按显示宽度折行；保留原有换行，空行原样保留。"""

    raw_text = "" if text is None else str(text)
    out: list[str] = []
    for raw_line in raw_text.splitlines() or [""]:
        line = raw_line.rstrip()
        if not line:
            out.append("")
            continue
        current = ""
        current_width = 0
        for char in line:
            char_width = _char_width(char)
            if current and current_width + char_width > width:
                out.append(current.rstrip())
                current, current_width = "", 0
                if char == " ":
                    continue
            current += char
            current_width += char_width
        if current.strip():
            out.append(current.rstrip())
    return out or [""]


def _block(*lines: Any) -> list[str]:
    """把若干行（可能是长文本）展开成不超宽的卡片行。"""

    out: list[str] = []
    for line in lines:
        out.extend(_wrap_text(line))
    return out


def _stamp(when: Any = None) -> str:
    """时间戳 → ``MM-DD HH:MM``（北京时间；字符串原样返回，缺省用当前时间）。"""

    moment = when
    if isinstance(moment, datetime):
        if moment.tzinfo is not None and moment.utcoffset() is not None:
            moment = moment.astimezone(DISPLAY_TZ)
        return moment.strftime("%m-%d %H:%M")
    text = str(moment or "").strip()
    if text and text not in ("None", "-"):
        return text
    return datetime.now(DISPLAY_TZ).strftime("%m-%d %H:%M")


def _title(heading: str, when: Any = None) -> list[str]:
    """标题行（``emoji 中文标题 · MM-DD HH:MM``）+ 空行。"""

    return [f"{heading} · {_stamp(when)}", ""]


def _label(label: Any, width: int = 6) -> str:
    """中文标签对齐：补全角空格到 ``width + 1`` 个汉字宽（值从同一列开始）。"""

    text = str(label or "")
    used = max(0, display_width(text) // 2)
    pad = max(1, int(width) + 1 - used)
    return f"{text}{'　' * pad}"


def _kv(label: Any, value: Any, width: int = 6) -> str:
    """``标签　值``：标签对齐、值左对齐（卡片里的字段行）。"""

    return f"{_label(label, width)}{value}"


def _section(heading: str) -> list[str]:
    """分区小标题：前面空一行，便于一屏内分区阅读。"""

    return ["", str(heading)]


def _footer(note: Any = "") -> list[str]:
    """卡片收尾：空行 + 分隔线 +（可选）一行备注；``DISCLAIMER`` 由调用方追加。"""

    out = ["", CARD_DIVIDER]
    text = str(note or "").strip()
    if text:
        out.extend(_block(text))
    return out


def _render(lines: list[str]) -> str:
    """拼接卡片行：连续空行压成一行（LLM 理由里的多空行不会撑散版面）。"""

    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed).rstrip()


def card_notice(
    heading: str,
    lines: Iterable[Any] | None = None,
    *,
    footer: Any = "",
    when: Any = None,
    stamp: bool = True,
) -> str:
    """通用小卡片：``emoji 中文标题 · 时间`` + 空行 + 内容 + 分隔线 + 备注 + 声明。

    ``heading`` 需自带 emoji（如 ``"✅ 已提交买入委托"``）；``stamp=False`` 时不加时间。
    """

    head = f"{heading} · {_stamp(when)}" if stamp else str(heading)
    body: list[str] = [head, ""]
    for line in lines or []:
        body.extend(_wrap_text(line))
    body.extend(_footer(footer))
    return append_disclaimer(_render(body))


# ---------------------------------------------------------------------------
# 估值（与 accounting.py 同口径的最小实现，report 只做展示）
# ---------------------------------------------------------------------------


def position_unrealized(position: Any, quote: Any, spec: Any, rates: Any) -> int | None:
    """单手浮动盈亏（元）；行情或汇率不可用时返回 ``None``。

    多头按 bid、空头按 ask（拿不到就退回 mid）；JPY 计价品种「价差 × 数量 ÷ jpy_per_cny」，
    USD 计价（EURUSD）「价差 × 数量 × cny_per_usd」，最后 1 元四舍五入。
    """

    if quote is None or spec is None or rates is None:
        return None
    side = str(_attr(position, "side", ""))
    bid = _attr(quote, "bid")
    ask = _attr(quote, "ask")
    mark = bid if side == "buy" else ask
    if mark is None:
        mark = _attr(quote, "mid")
    if mark is None:
        return None
    entry = _attr(position, "entry_price")
    units = int(_attr(position, "units", 0) or 0)
    if entry is None or units <= 0:
        return None
    direction = 1 if side == "buy" else -1
    diff = Decimal(str(mark)) - Decimal(str(entry))
    pnl = diff * units * direction
    currency = str(_attr(spec, "quote_currency", "JPY"))
    if currency == "JPY":
        pnl = pnl / Decimal(str(rates.jpy_per_cny))
    elif currency == "USD":
        pnl = pnl * Decimal(str(rates.cny_per_usd))
    else:
        return None
    return _round_money(pnl)


def required_margin(position: Any, spec: Any, rates: Any, leverage: int = 10) -> int:
    """单笔持仓的所需保证金（元，1 元向上取整）；优先用持仓上记录的值。"""

    stored = int(_attr(position, "margin", 0) or 0)
    if stored > 0:
        return stored
    if spec is None or rates is None:
        return 0
    price = _attr(position, "entry_price")
    units = int(_attr(position, "units", 0) or 0)
    if price is None or units <= 0 or leverage <= 0:
        return 0
    notional = Decimal(str(price)) * units
    currency = str(_attr(spec, "quote_currency", "JPY"))
    if currency == "JPY":
        notional = notional / Decimal(str(rates.jpy_per_cny))
    elif currency == "USD":
        notional = notional * Decimal(str(rates.cny_per_usd))
    else:
        return 0
    return int((notional / Decimal(leverage)).to_integral_value(rounding="ROUND_CEILING"))


def _account_metrics(
    account: Any,
    positions: list[Any],
    quotes: dict[str, Any] | None,
    pairs: dict[str, Any] | None,
    rates: Any,
    leverage: int = 10,
) -> tuple[int, int, int]:
    """返回 (equity, margin_total, floating)。汇率不可用时只算已记录的保证金、浮动记 0。"""

    quotes = quotes or {}
    floating = 0
    margin = 0
    for position in positions or []:
        code = str(_attr(position, "pair", ""))
        spec = _spec_of(pairs, code)
        pnl = position_unrealized(position, quotes.get(code), spec, rates)
        if pnl is not None:
            floating += pnl
        margin += required_margin(position, spec, rates, leverage)
    balance = int(_attr(account, "balance", 0) or 0)
    return balance + floating, margin, floating


def maintenance_ratio_text(equity: int, margin_total: int) -> str:
    """保证金维持率（%）文本；无持仓返回 ``—``。"""

    if margin_total <= 0:
        return "—"
    ratio = Decimal(equity) * Decimal(100) / Decimal(margin_total)
    return f"{ratio:.2f}%"


def _market_state_text(account: Any) -> str:
    parts: list[str] = []
    if bool(_attr(account, "halted", False)):
        parts.append("已停手")
    if bool(_attr(account, "loss_cut_pending", False)):
        parts.append("强平处理中")
    return "、".join(parts) if parts else "正常"


def _loss_cut_ratio(account: Any) -> int:
    value = _attr(account, "loss_cut_ratio", None)
    try:
        return int(value) if value is not None else 50
    except (TypeError, ValueError):
        return 50


def _floating_text(floating: int | None, rates: Any) -> str:
    if floating is not None:
        return fmt_signed_money(floating)
    return "汇率不可用" if rates is None else "行情不可用"


def _spread_text(quote: Any, spec: Any) -> str:
    """点差（pip，1 位小数）；算不出来返回空串。"""

    pip = _attr(spec, "pip") if spec is not None else None
    bid = _attr(quote, "bid")
    ask = _attr(quote, "ask")
    if bid is None or ask is None or not pip:
        return ""
    try:
        spread = (Decimal(str(ask)) - Decimal(str(bid))) / Decimal(str(pip))
    except (InvalidOperation, ValueError):
        return ""
    if spread <= 0:
        base = _attr(spec, "base_spread_pips") if spec is not None else None
        if base is not None:
            try:
                spread = Decimal(str(base))
            except (InvalidOperation, ValueError):
                pass
    return f"{spread:.1f}"


def _order_price_text(request: Any) -> str:
    """委托方式 + 价格（市价只出现一次，不重复显示「市价 | 市价」）。"""

    order_type = str(_attr(request, "order_type", "") or "")
    price = _attr(request, "limit_price", None) or _attr(request, "trigger_price", None)
    if price is None:
        price = _attr(request, "price", None)  # DecisionIntent 只有 price 字段
    if price is not None:
        label = {"limit": "限价", "stop": "触发价", "trailing_stop": "追踪"}.get(order_type, "委托价")
        return f"{label} {fmt_price(price)}"
    return ORDER_TYPE_TEXT.get(order_type, "市价")


# ---------------------------------------------------------------------------
# 报价 / 持仓 / 挂单 / 记录
# ---------------------------------------------------------------------------


def _reference_scale(reference: Any) -> int:
    """参考对的展示倍数（``JPYCNY`` 为 100，其余 1）；坏数据按 1 处理。"""

    try:
        return int(getattr(reference, "scale", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _reference_label(reference: Any) -> str:
    """参考对显示名：日元按「100日元」显示（0.0422 太难读），其余用中文名。"""

    scale = _reference_scale(reference)
    if scale != 1:
        return f"{scale}日元"
    return str(getattr(reference, "name_cn", "") or getattr(reference, "code", "-"))


def _reference_price(value: Any, reference: Any) -> str:
    """参考汇率价格：× scale 后固定 4 位小数（``None`` → ``-``）。"""

    if value is None:
        return "-"
    quantum = Decimal(1).scaleb(-REFERENCE_DIGITS)
    try:
        number = Decimal(str(value)) * Decimal(_reference_scale(reference))
        return format(number.quantize(quantum), "f")
    except (InvalidOperation, ValueError, TypeError):
        return "-"


def _reference_spread(quote: Any, reference: Any) -> str:
    """参考对点差（显示点，1 位小数）；算不出或 ≤0 时返回空串（不显示噪音）。"""

    bid = _attr(quote, "bid")
    ask = _attr(quote, "ask")
    if bid is None or ask is None:
        return ""
    try:
        diff = (Decimal(str(ask)) - Decimal(str(bid))) * Decimal(_reference_scale(reference))
        pip = Decimal(str(getattr(reference, "pip", pairs_mod.REFERENCE_PIP)))
    except (InvalidOperation, ValueError, TypeError):
        return ""
    if diff <= 0 or pip <= 0:
        return ""
    return f"{diff / pip:.1f}"


def _aligned_quote_rows(cells: list[tuple[str, str, str, str, str]]) -> list[str]:
    """把 ``(旗, 名, 买入, 卖出, 点差)`` 五元组排成按本栏最宽值对齐的卡片行。"""

    name_width = max((display_width(cell[1]) for cell in cells), default=0) + 2
    price_width = max((display_width(text) for cell in cells for text in (cell[2], cell[3])), default=0)
    spread_width = max((display_width(cell[4]) for cell in cells), default=0)
    rows: list[str] = []
    for symbol, name, bid_text, ask_text, spread in cells:
        row = (
            f"{symbol} {_pad_right(name, name_width)}"
            f"{_pad_right(bid_text, price_width)} / {_pad_right(ask_text, price_width)}"
        )
        if spread:
            row += f"  点差 {_pad_right(spread, spread_width)}"
        rows.extend(_block(row))
    return rows


def _reference_rows(rows: list[tuple[Any, Any]]) -> list[str]:
    """参考汇率栏：普通参考对按 4 位小数对齐；日元按「100日元 = X 元」单独一行。"""

    flat = [(reference, quote) for reference, quote in rows if _reference_scale(reference) == 1]
    name_width = (
        max((display_width(str(getattr(reference, "name_cn", ""))) for reference, _q in flat), default=0) + 2
    )
    price_width = max(
        (display_width(_reference_price(_attr(quote, "bid"), reference)) for reference, quote in flat),
        default=0,
    )
    spread_width = max(
        (display_width(_reference_spread(quote, reference)) for reference, quote in flat), default=0
    )
    lines: list[str] = []
    for reference, quote in rows:
        flags = _pair_flags(None, str(getattr(reference, "code", "")))
        bid_text = _reference_price(_attr(quote, "bid"), reference)
        ask_text = _reference_price(_attr(quote, "ask"), reference)
        spread = _reference_spread(quote, reference)
        if _reference_scale(reference) != 1:
            row = f"{flags} {_reference_label(reference)} = {bid_text} / {ask_text} 元"
            if spread:
                row += f"  点差 {spread}"
            lines.extend(_block(row))
            continue
        row = (
            f"{flags} {_pad_right(getattr(reference, 'name_cn', ''), name_width)}"
            f"{_pad_right(bid_text, price_width)} / {_pad_right(ask_text, price_width)}"
        )
        if spread:
            row += f"  点差 {_pad_right(spread, spread_width)}"
        lines.extend(_block(row))
    return lines


def quotes_overview(
    quotes: dict[str, Any] | None,
    pairs: dict[str, Any] | None = None,
    *,
    cny_reference: bool = True,
) -> str:
    """``/外汇 行情``：可交易品种 + 人民币参考汇率 + 「📖 怎么看」说明（卡片式）。

    分区：

    - **可交易**：只来自 ``pairs``（配置白名单，默认 8 个品种）逐行「旗 + 中文名 +
      买入 / 卖出 + 点差（pip）」；
    - **人民币参考汇率**（``cny_reference=True`` 时）：:data:`pairs.REFERENCE_PAIRS` 的
      8 个参考对，金额固定 4 位小数；``JPYCNY`` 按「100日元 = X 元」显示（价格 ×100），
      避免 0.0422 这种难读数字。**只展示、不可交易**，永远不进可交易栏；
    - **📖 怎么看**：给不了解外汇的人看的说明（:data:`HOWTO_LINES`）。

    USDCNY 既是换算锚又是参考对：只出现在参考栏（避免被误当可下单货币对）。
    标题时间取最新一条快照的行情时间（北京时间）。
    """

    quotes = dict(quotes or {})
    references = pairs_mod.reference_pairs()
    reference_codes = {str(reference.code) for reference in references}
    tradable = {
        code: quote for code, quote in quotes.items() if str(code) not in reference_codes
    }
    ordered: list[str] = [code for code in (pairs or {}) if code in tradable]
    ordered.extend(sorted(code for code in tradable if code not in ordered))
    shown: list[tuple[str, Any]] = [(code, tradable[code]) for code in ordered]

    reference_rows: list[tuple[Any, Any]] = []
    if cny_reference:
        for reference in references:
            quote = quotes.get(reference.code)
            if quote is None:
                continue
            if _attr(quote, "bid") is None or _attr(quote, "ask") is None:
                continue
            reference_rows.append((reference, quote))

    if not shown and not reference_rows:
        lines = _title("📊 外汇行情")
        lines.extend(_block("📭 暂无行情（等待第一次拉取）"))
        lines.extend(_footer("📝 行情恢复后自动刷新，无需操作"))
        return append_disclaimer(_render(lines))

    latest: datetime | None = None
    for _code, quote in list(shown) + list(reference_rows):
        ts = _attr(quote, "quote_ts")
        if isinstance(ts, datetime) and (latest is None or ts > latest):
            latest = ts

    lines = _title("📊 外汇行情", latest)
    subtitle = f"可交易 {len(shown)} 个品种" if shown else "可交易暂无行情"
    if reference_rows:
        subtitle += " + 人民币参考汇率"
    lines.extend(_block(subtitle))
    lines.extend(["", "【可交易】"])
    if shown:
        cells = [
            (
                _pair_flags(pairs, code),
                pair_name(pairs, code),
                _price_text(_attr(quote, "bid"), _spec_of(pairs, code)),
                _price_text(_attr(quote, "ask"), _spec_of(pairs, code)),
                _spread_text(quote, _spec_of(pairs, code)),
            )
            for code, quote in shown
        ]
        lines.extend(_aligned_quote_rows(cells))
    else:
        lines.extend(_block("📭 暂无行情（等待第一次拉取）"))

    if cny_reference:
        lines.extend(["", CARD_DIVIDER, "【人民币参考汇率】（仅参考，暂不可交易）"])
        if reference_rows:
            lines.extend(_reference_rows(reference_rows))
            lines.extend(_block("📝 均为 4 位小数；日元按 100 日元折算"))
        else:
            lines.extend(_block("📭 暂无参考汇率（等待下一次拉取）"))

    lines.extend(["", CARD_DIVIDER])
    if shown:
        lines.extend(_block(*HOWTO_LINES))
    return append_disclaimer(_render(lines))


def positions_list(
    positions: list[Any],
    quotes: dict[str, Any] | None = None,
    pairs: dict[str, Any] | None = None,
    rates: Any = None,
) -> str:
    """``/外汇 持仓``：逐持仓显示方向/数量/开仓价/浮动/止损止盈/保证金。"""

    positions = list(positions or [])
    lines = _title("📋 当前持仓")
    if not positions:
        lines.extend(_block("📭 空仓中，当前没有持仓"))
        lines.extend(_footer("📝 开仓：/外汇 买 美元日元 1万 市价"))
        return append_disclaimer(_render(lines))
    quotes = quotes or {}
    total_floating = 0
    all_quoted = True
    for index, position in enumerate(positions):
        code = str(_attr(position, "pair", ""))
        spec = _spec_of(pairs, code)
        side_text = "多" if str(_attr(position, "side", "")) == "buy" else "空"
        mark = "🟢" if side_text == "多" else "🔴"
        floating = position_unrealized(position, quotes.get(code), spec, rates)
        if floating is None:
            all_quoted = False
        else:
            total_floating += floating
        if index:
            lines.append("")
        lines.extend(
            _block(
                f"{mark} {pair_name(pairs, code)} · {side_text} {_units_text(_attr(position, 'units', 0))}",
                f"开仓 {_price_text(_attr(position, 'entry_price'), spec)}｜浮动 {_floating_text(floating, rates)}",
                f"止损 {_price_text(_attr(position, 'stop_price'), spec)}"
                f"｜止盈 {_price_text(_attr(position, 'take_price'), spec)}"
                f"｜保证金 {fmt_money(_attr(position, 'margin', 0))}",
            )
        )
    summary = f"📝 共 {len(positions)} 笔"
    if all_quoted:
        summary += f"｜浮动合计 {fmt_signed_money(total_floating)}"
    lines.extend(_footer(summary))
    return append_disclaimer(_render(lines))


def margin_overview(
    account: Any,
    positions: list[Any] | None = None,
    quotes: dict[str, Any] | None = None,
    pairs: dict[str, Any] | None = None,
    rates: Any = None,
    *,
    leverage: int = 10,
) -> str:
    """``/外汇 保证金``：账户净值/占用保证金/维持率 + 逐笔占用 + 强平规则。"""

    positions = list(positions or [])
    quotes = quotes or {}
    equity, margin, floating = _account_metrics(account, positions, quotes, pairs, rates, leverage)
    lines = _title("💰 保证金 · 账户概览")
    lines.extend(
        _block(
            _kv("账户净值", fmt_money(equity)),
            _kv("占用保证金", fmt_money(margin)),
            _kv("保证金维持率", maintenance_ratio_text(equity, margin)),
            _kv("浮动盈亏", fmt_signed_money(floating)),
        )
    )
    if rates is None:
        lines.extend(_section("⚠️ 汇率不可用"))
        lines.extend(_block("估值与维持率暂停，暂不开新仓", "（绝不用假汇率）"))
    if not positions:
        lines.extend(_section("📭 空仓"))
        lines.extend(_block("当前空仓，无保证金占用"))
    else:
        lines.extend(_section("📌 持仓占用"))
        for index, position in enumerate(positions):
            code = str(_attr(position, "pair", ""))
            spec = _spec_of(pairs, code)
            side_text = "多" if str(_attr(position, "side", "")) == "buy" else "空"
            piece = required_margin(position, spec, rates, leverage)
            pnl = position_unrealized(position, quotes.get(code), spec, rates)
            if index:
                lines.append("")
            lines.extend(
                _block(
                    f"{pair_name(pairs, code)} {side_text} {_units_text(_attr(position, 'units', 0))}",
                    f"所需保证金 {fmt_money(piece)}｜浮动 {_floating_text(pnl, rates)}",
                )
            )
    lines.extend(_footer(f"📌 强平规则：账户净值 < 所需保证金 × {_loss_cut_ratio(account)}%"))
    return append_disclaimer(_render(lines))


def pending_list(orders: Iterable[Any] | None) -> str:
    """``/外汇 挂单``：逐挂单显示 id/品种/方向/类型/数量/委托价/状态。"""

    orders = list(orders or [])
    lines = _title("🧾 当前挂单")
    if not orders:
        lines.extend(_block("📭 没有在途挂单"))
        lines.extend(_footer("📝 挂单不占用保证金，成交后才占用"))
        return append_disclaimer(_render(lines))
    for index, order in enumerate(orders):
        request = _attr(order, "request", None)
        code = str(_attr(request, "pair", "") if request is not None else "")
        side = SIDE_TEXT.get(str(_attr(request, "side", "")), "?")
        position_type = POSITION_TYPE_TEXT.get(str(_attr(request, "position_type", "")), "?")
        status = str(_attr(order, "status", ""))
        units = _attr(request, "units", 0) if request is not None else 0
        if index:
            lines.append("")
        lines.extend(
            _block(
                f"🟡 {_attr(order, 'order_id', '?')} · {pair_name(None, code)} {position_type}{side}",
                f"{_units_text(units)}｜{_order_price_text(request)}｜{STATUS_TEXT.get(status, status)}",
            )
        )
    lines.extend(_footer(f"📝 共 {len(orders)} 笔挂单｜不占用保证金，成交后才占用"))
    return append_disclaimer(_render(lines))


def record_list(trades: Iterable[Any] | None) -> str:
    """``/外汇 记录``：最近成交（支持 ledger.list_trades 的 dict 或 Trade 对象）。"""

    trades = list(trades or [])
    lines = _title("📝 最近成交")
    if not trades:
        lines.extend(_block("📭 还没有成交记录"))
        lines.extend(_footer("📝 默认显示最近 10 笔（/外汇 记录 N 可调）"))
        return append_disclaimer(_render(lines))
    for index, trade in enumerate(trades):
        code = str(_attr(trade, "pair", ""))
        position_type = POSITION_TYPE_TEXT.get(str(_attr(trade, "position_type", "")), "?")
        side = SIDE_TEXT.get(str(_attr(trade, "side", "")), "?")
        pnl = int(_attr(trade, "realized_pnl", 0) or 0)
        pnl_text = fmt_signed_money(pnl) if position_type == "平仓" else "-"
        ts_text = _stamp(_attr(trade, "ts"))
        if index:
            lines.append("")
        lines.extend(
            _block(
                f"🧾 {ts_text} · {position_type}{side} {pair_name(None, code)}",
                f"{_units_text(_attr(trade, 'units', 0))} @ {fmt_price(_attr(trade, 'price'))}",
                f"手续费 {fmt_money(_attr(trade, 'fee', 0))}｜盈亏 {pnl_text}",
            )
        )
    lines.extend(_footer(f"📝 共 {len(trades)} 笔｜正负盈亏以元计"))
    return append_disclaimer(_render(lines))


# ---------------------------------------------------------------------------
# 账户总览
# ---------------------------------------------------------------------------


def status(
    account: Any,
    positions: list[Any] | None = None,
    quotes: dict[str, Any] | None = None,
    pairs: dict[str, Any] | None = None,
    rates: Any = None,
    *,
    pending: Iterable[Any] | None = None,
    leverage: int = 10,
    market_open: bool | None = None,
    auto_trade: bool | None = None,
    paused: bool | None = None,
) -> str:
    """``/外汇 状态``：账户概览（净值/保证金/盈亏）+ 持仓/挂单/运行状态一屏摘要。"""

    positions = list(positions or [])
    quotes = quotes or {}
    equity, margin, floating = _account_metrics(account, positions, quotes, pairs, rates, leverage)
    pending_count = len(list(pending or []))
    lines = _title("📊 麦麦炒外汇 · 账户概览")
    if rates is None:
        lines.extend(_block("⚠️ 汇率不可用：净值与维持率暂停估值", "（绝不用假汇率，暂不开新仓）"))
        lines.append("")
    lines.extend(
        _block(
            _kv("账户净值", fmt_money(equity)),
            _kv("现金余额", fmt_money(_attr(account, "balance", 0))),
            _kv("浮动盈亏", fmt_signed_money(floating)),
            _kv("占用保证金", fmt_money(margin)),
            _kv("保证金维持率", maintenance_ratio_text(equity, margin)),
        )
    )
    lines.append("")
    lines.extend(
        _block(
            _kv("已实现盈亏", fmt_signed_money(_attr(account, "realized_pnl", 0))),
            _kv("累计手续费", fmt_money(_attr(account, "fees_paid", 0))),
            _kv("隔夜利息", fmt_signed_money(_attr(account, "swap_paid", 0))),
        )
    )
    lines.append("")
    lines.extend(
        _block(
            f"📌 持仓 {len(positions)} 笔｜挂单 {pending_count} 笔"
            f"｜今日开仓 {int(_attr(account, 'trades_today', 0) or 0)} 次"
        )
    )
    state_bits = [f"账户{_market_state_text(account)}"]
    if market_open is not None:
        state_bits.append("市场开盘中" if market_open else "市场休市")
    if auto_trade is not None:
        state_bits.append("自动交易开启" if auto_trade else "自动交易关闭")
    if paused:
        state_bits.append("已暂停")
    lines.extend(_block("🌐 " + "｜".join(state_bits)))
    if positions:
        lines.extend(_section("📌 持仓明细"))
        for position in positions:
            code = str(_attr(position, "pair", ""))
            spec = _spec_of(pairs, code)
            side_text = "多" if str(_attr(position, "side", "")) == "buy" else "空"
            floating_one = position_unrealized(position, quotes.get(code), spec, rates)
            lines.extend(
                _block(
                    f"{pair_name(pairs, code)} {side_text}"
                    f" {_units_text(_attr(position, 'units', 0))}"
                    f"｜浮动 {_floating_text(floating_one, rates)}"
                )
            )
    lines.extend(_footer(f"📝 强平线：账户净值 < 所需保证金 × {_loss_cut_ratio(account)}%"))
    return append_disclaimer(_render(lines))


# ---------------------------------------------------------------------------
# 财经日历 / 事件（纯函数）
# ---------------------------------------------------------------------------


def event_soon_text(event: Any) -> str:
    """「即将公布」卡片播报（``event`` 为 ``econ_calendar.CalendarEvent`` 或同构 dict）。

    有高影响事件临近时由 scheduler 播报一次，提醒群友点差/波动会放大、事件窗内禁止开新仓。
    """

    currency = str(_attr(event, "currency", "") or "").strip()
    name = str(_attr(event, "name", "") or "").strip() or "重要数据"
    impact_attr = _attr(event, "impact_text", None)
    if impact_attr:
        impact_text = str(impact_attr)
    else:
        impact_text = {"high": "高", "medium": "中", "low": "低"}.get(
            str(_attr(event, "impact", "") or ""), "高"
        )
    at = _attr(event, "at", None)
    when = at.strftime("%m-%d %H:%M") if hasattr(at, "strftime") else "-"
    subject = " ".join(part for part in (currency, name) if part)
    lines = _title("⏰ 财经日历")
    lines.extend(
        _block(
            f"📢 即将公布：{subject}（{impact_text}影响）",
            _kv("时间", f"{when}（北京时间）", width=2),
            "",
            "⚠️ 公布前后波动会放大、点差可能变宽",
        )
    )
    lines.extend(_footer("📝 事件窗内麦麦不新开仓；已有持仓照常按风控处理"))
    return append_disclaimer(_render(lines))


# ---------------------------------------------------------------------------
# 事件播报（scheduler 直接调用）
# ---------------------------------------------------------------------------


def rate_unavailable_text() -> str:
    """汇率缺失播报（每个缺失时段只发一次）：不放行新开仓、不估值。"""

    lines = _title("⚠️ 汇率不可用")
    lines.extend(
        _block(
            "缺少 USDCNY / USDJPY 行情",
            "无法把盈亏与保证金换算成元",
            "",
            "📌 已暂停估值与开新仓（绝不用假汇率）",
            "📌 已成交持仓保留，汇率恢复后自动继续",
        )
    )
    lines.extend(_footer("📝 等两条汇率锚都回来再继续，不影响已记录账目"))
    return append_disclaimer(_render(lines))


def fill_report(fill: Any, spec: Any = None) -> str:
    """成交播报：新规/平仓 + 方向/数量/价格/滑点/手续费/已实现盈亏。"""

    code = str(_attr(fill, "pair", ""))
    name = str(getattr(spec, "name_cn", "") or "") if spec is not None else ""
    name = name or pair_name(None, code)
    side = SIDE_TEXT.get(str(_attr(fill, "side", "")), "?")
    position_type = POSITION_TYPE_TEXT.get(str(_attr(fill, "position_type", "")), "?")
    mark = "🟢" if side == "买" else "🔴"
    lines = _title("✅ 成交回报", _attr(fill, "ts"))
    lines.extend(
        _block(
            f"{mark} {position_type}{side} {name}",
            f"成交 {_units_text(_attr(fill, 'units', 0))} @ {fmt_price(_attr(fill, 'price'))}",
        )
    )
    detail = f"滑点 {fmt_pips(_attr(fill, 'slippage_pips', 0))} pip"
    fee = int(_attr(fill, "fee", 0) or 0)
    if fee:
        detail += f"｜手续费 {fmt_money(fee)}"
    lines.extend(_block(detail))
    if position_type == "平仓":
        lines.extend(_block(f"已实现盈亏 {fmt_signed_money(_attr(fill, 'realized_pnl', 0))}"))
    lines.extend(_footer("📝 持仓与保证金已同步更新"))
    return append_disclaimer(_render(lines))


def order_submitted(order: Any) -> str:
    """下单受理播报：订单号/品种/类型/方向/数量/委托价/状态。"""

    request = _attr(order, "request", None)
    code = str(_attr(request, "pair", "") if request is not None else "")
    side = SIDE_TEXT.get(str(_attr(request, "side", "") if request is not None else ""), "?")
    position_type = POSITION_TYPE_TEXT.get(str(_attr(request, "position_type", "")), "?") if request else "?"
    status = str(_attr(order, "status", ""))
    units = _attr(request, "units", 0) if request is not None else 0
    order_id = str(_attr(order, "order_id", "?") or "?")
    lines = _title("📨 订单已受理", _attr(order, "created_at"))
    lines.extend(
        _block(
            f"🆔 {order_id}",
            f"{pair_name(None, code)} {position_type}{side} {_units_text(units)}",
            f"{_order_price_text(request)}｜{STATUS_TEXT.get(status, status)}",
        )
    )
    lines.extend(_footer("📝 挂单不占用保证金，成交后另发成交播报"))
    return append_disclaimer(_render(lines))


def _reason_text(reason: Any) -> str:
    text = str(reason or "").strip()
    if not text:
        return "未注明原因"
    head = text.split("：", 1)[0]
    label = REASON_TEXT.get(head) or REASON_TEXT.get(text)
    if label is None:
        return text
    return f"{label}（{text}）" if label != text else label


def order_rejected(order_id: Any, reason: Any) -> str:
    """拒单播报（含原因码翻译）。"""

    lines = _title("❌ 订单被拒绝")
    subject = str(order_id or "").strip()
    if subject:
        lines.extend(_block(f"🆔 {subject}"))
    lines.extend(_section("💬 原因"))
    lines.extend(_block(_reason_text(reason)))
    lines.extend(_footer("📝 本次未成交；可调整数量/价格后重试"))
    return append_disclaimer(_render(lines))


def order_suspended(order_id: Any, reason: Any) -> str:
    """挂起播报（行情过期/休市等，挂单不丢）。"""

    lines = _title("⏸ 订单已挂起")
    subject = str(order_id or "").strip()
    if subject:
        lines.extend(_block(f"🆔 {subject}"))
    lines.extend(_section("💬 原因"))
    lines.extend(_block(_reason_text(reason)))
    lines.extend(_footer("📝 行情恢复后继续评估，不会丢单"))
    return append_disclaimer(_render(lines))


def decision_report(intent: Any) -> str:
    """决策播报：动作/品种/数量/价格/理由/发言；hold 也明确说观望。"""

    action = str(_attr(intent, "action", "") or "hold")
    action_text = ACTION_TEXT.get(action, action or "观望")
    reason = str(_attr(intent, "reason", "") or "").strip()
    speech = str(_attr(intent, "speech", "") or "").strip()
    lines = _title("🤔 麦麦的决定")
    if action == "hold":
        lines.extend(_block("⏸ 观望（本轮不交易）"))
    else:
        code = str(_attr(intent, "pair", ""))
        units = int(_attr(intent, "units", 0) or 0)
        mark = ACTION_EMOJI.get(action, "⭐")
        lines.extend(_block(f"{mark} {action_text} {pair_name(None, code)}".rstrip()))
        if units:
            lines.extend(_block(f"数量  {units:,} 单位｜{_order_price_text(intent)}"))
        else:
            lines.extend(_block(f"委托  {_order_price_text(intent)}"))
        stop_loss = _attr(intent, "stop_loss")
        take_profit = _attr(intent, "take_profit")
        legs: list[str] = []
        if stop_loss is not None:
            legs.append(f"止损  {fmt_price(stop_loss)}")
        if take_profit is not None:
            legs.append(f"止盈  {fmt_price(take_profit)}")
        if legs:
            lines.extend(_block("｜".join(legs)))
    if reason:
        lines.extend(_section("💬 理由"))
        lines.extend(_block(reason))
    if speech:
        lines.append("")
        lines.extend(_block(f"✨ 麦麦说：{speech}"))
    lines.append("")
    lines.append(DISCLAIMER)
    return append_disclaimer(_render(lines))


def loss_cut_report(
    fills: Iterable[Any] | None,
    account: Any,
    equity: int,
    positions: Iterable[Any] | None = None,
) -> str:
    """强制平仓播报：被强平的持仓逐条列出 + 强平后账户净值 + 剩余持仓数。"""

    del account  # 只用于调用方语义；强平后净值由 ``equity`` 直接给出
    fills = list(fills or [])
    remaining = list(positions or [])
    lines = _title("🚨 强制平仓")
    lines.extend(_block("保证金维持率跌破强平线", "程序已一次性全部平仓"))
    lines.extend(_section("📉 平仓明细"))
    total_pnl = 0
    for index, fill in enumerate(fills):
        realized = int(_attr(fill, "realized_pnl", 0) or 0)
        total_pnl += realized
        if index:
            lines.append("")
        lines.extend(
            _block(
                f"{pair_name(None, str(_attr(fill, 'pair', '')))}"
                f" {_units_text(_attr(fill, 'units', 0))} @ {fmt_price(_attr(fill, 'price'))}",
                f"已实现 {fmt_signed_money(realized)}",
            )
        )
    lines.extend(_section("📌 强平结果"))
    lines.extend(_block(f"本次强平合计 {fmt_signed_money(total_pnl)}"))
    lines.extend(_block(f"强平后账户净值 {fmt_money(equity)}｜剩余持仓 {len(remaining)} 笔"))
    lines.extend(_footer("📝 模拟盘按 50% 维持率强平；真实市场亏损同样残酷"))
    return append_disclaimer(_render(lines))


def swap_report(total_swap: Any, day_count: Any) -> str:
    """日切隔夜利息播报：合计金额 + 天数 + 正负含义。"""

    amount = int(total_swap or 0)
    days_topup = int(day_count or 0)
    tone = "收取（持仓利息收入）" if amount >= 0 else "支付（持仓利息成本）"
    lines = _title("🌙 隔夜利息入账")
    lines.extend(
        _block(
            _kv("金额", fmt_signed_money(amount), width=2),
            _kv("方向", tone, width=2),
            _kv("结算", f"{days_topup} 个日切", width=2),
        )
    )
    lines.extend(_footer("📝 周三按 3 日分计息；周末不单独计息"))
    return append_disclaimer(_render(lines))


def swap_list(records: Iterable[Any] | None, pairs: dict[str, Any] | None = None) -> str:
    """``/外汇 隔夜``：最近隔夜利息明细（日期/品种/方向/数量/金额/日分）。"""

    records = list(records or [])
    lines = _title("🌙 隔夜利息")
    if not records:
        lines.extend(_block("📭 还没有隔夜利息记录（日切后才会入账）"))
        lines.extend(_footer("📝 周三按 3 日分计息；周末不单独计息"))
        return append_disclaimer(_render(lines))
    total = 0
    for index, item in enumerate(records):
        amount = int(_attr(item, "amount", 0) or 0)
        total += amount
        code = str(_attr(item, "pair", "") or "")
        side_text = "买" if str(_attr(item, "side", "")) == "buy" else "卖"
        day = str(_attr(item, "rollover_date", "") or "-")
        days = _attr(item, "days", "-") or "-"
        units = int(_attr(item, "units", 0) or 0)
        if index:
            lines.append("")
        lines.extend(
            _block(
                f"📅 {day} · {pair_name(pairs, code)} {side_text} {_units_text(units)}",
                f"{fmt_signed_money(amount)}（{days} 日分）",
            )
        )
    lines.extend(_footer(f"📆 合计 {fmt_signed_money(total)}（正=收取，负=支付）"))
    return append_disclaimer(_render(lines))


# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------


def _cmd_line(command: str, desc: str = "", width: int = 20) -> str:
    """帮助行：命令 + 说明（说明列按显示宽度对齐到 ``width`` 列）。"""

    text = str(command or "").rstrip()
    if not desc:
        return text
    pad = max(2, int(width) - display_width(text))
    return f"{text}{' ' * pad}{desc}"


def help_text() -> str:
    """``/外汇 帮助``：命令清单，按「查询 / 交易 / 管理」分组（卡片式排版）。"""

    lines = _title("🧭 命令帮助")
    lines.extend(_block("🔍 查询（所有人）"))
    lines.extend(
        _block(
            _cmd_line("/外汇 状态", "账户净值与摘要"),
            _cmd_line("/外汇 持仓", "逐笔持仓与浮动"),
            _cmd_line("/外汇 挂单", "在途挂单"),
            _cmd_line("/外汇 行情", "8 个品种 + 人民币参考汇率"),
            _cmd_line("/外汇 保证金", "保证金与维持率明细"),
            _cmd_line("/外汇 隔夜", "最近隔夜利息"),
            _cmd_line("/外汇 记录 [N]", "最近成交（默认 10 条）"),
            _cmd_line("/外汇 帮助", "本帮助"),
        )
    )
    lines.extend(_block("📖 行情怎么看：/外汇 行情 卡片底部有说明"))
    lines.extend(_footer(""))
    lines.extend(_block("💼 交易（仅管理员）"))
    lines.extend(
        _block(
            "/外汇 买 美元日元 1万 限价 158.5",
            "/外汇 卖 美元日元 1万 市价",
            _cmd_line("/外汇 平 美元日元", "平该品种持仓"),
            _cmd_line("/外汇 全平", "平掉全部持仓"),
            _cmd_line("/外汇 撤单 <订单号>", "撤掉指定挂单"),
            _cmd_line("/外汇 试跑", "只决策不下单"),
        )
    )
    lines.extend(_footer(""))
    lines.extend(_block("⚙️ 管理（仅管理员）"))
    lines.extend(
        _block(
            _cmd_line("/外汇 暂停 / 恢复", "暂停或恢复自动交易"),
            _cmd_line("/外汇 补给 10万", "补充虚拟资金"),
            _cmd_line("/外汇 重置 确认", "重置本金并清空账本"),
        )
    )
    lines.extend(_footer("📝 管理员名单在插件设置页维护｜自动交易默认关闭"))
    return append_disclaimer(_render(lines))


