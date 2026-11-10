"""命令处理：``/外汇`` 子命令解析、权限校验、生成回复文本（不直接发送）。

权限模型（INTERFACES）：
- 查询类（状态/持仓/挂单/行情/记录/保证金/隔夜/帮助）所有人可用；
- 控制类（试跑/暂停/恢复/买/卖/平/全平/撤单/重置/补给）仅管理员（``[admin] users``）；
- 非本插件命令、非白名单群/平台、私聊未开启 → 返回 ``None``（表示「不该响应」）；
- 本模块**不 import** ``plugin`` / ``scheduler``（避免循环依赖），只依赖 duck-type 接口：
  ``services.actions`` 字典优先，缺失时回退到 ``services.engine`` 的同名方法。

命令示例::

    /外汇 状态
    /外汇 买 美元日元 1万 限价 158.5 止损 158.0
    /外汇 卖 美元日元 1万 市价
    /外汇 平 美元日元
    /外汇 撤单 ORD-xxxx
    /外汇 补给 10万

注意：本插件只有虚拟资金，**不存在「入金/出金」**；对应功能叫「补给/重置本金」。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

try:  # 宿主把 plugin.py 当包加载时走相对导入
    from . import fxconv, report
except ImportError:  # 单测/脚本直接以顶层模块导入
    import fxconv  # type: ignore[no-redef]
    import report  # type: ignore[no-redef]

# 子命令别名 → 内部规范中文子命令
SUB_ALIASES: dict[str, str] = {
    "": "状态",
    "状态": "状态",
    "status": "状态",
    "账户": "状态",
    "资产": "状态",
    "acct": "状态",
    "持仓": "持仓",
    "positions": "持仓",
    "position": "持仓",
    "仓位": "持仓",
    "挂单": "挂单",
    "orders": "挂单",
    "order": "挂单",
    "委托": "挂单",
    "行情": "行情",
    "quote": "行情",
    "quotes": "行情",
    "报价": "行情",
    "记录": "记录",
    "history": "记录",
    "trades": "记录",
    "成交": "记录",
    "保证金": "保证金",
    "margin": "保证金",
    "保证金维持率": "保证金",
    "隔夜": "隔夜",
    "swap": "隔夜",
    "利息": "隔夜",
    "帮助": "帮助",
    "help": "帮助",
    "试跑": "试跑",
    "dry": "试跑",
    "演练": "试跑",
    "暂停": "暂停",
    "pause": "暂停",
    "恢复": "恢复",
    "resume": "恢复",
    "买": "买",
    "买入": "买",
    "buy": "买",
    "做多": "买",
    "卖": "卖",
    "卖出": "卖",
    "sell": "卖",
    "做空": "卖",
    "平": "平",
    "平仓": "平",
    "close": "平",
    "全平": "全平",
    "全平仓": "全平",
    "close_all": "全平",
    "撤单": "撤单",
    "cancel": "撤单",
    "重置": "重置",
    "reset": "重置",
    "补给": "补给",
    "补充": "补给",
    "deposit": "补给",
    "topup": "补给",
}
QUERY_SUBS = {"状态", "持仓", "挂单", "行情", "记录", "保证金", "隔夜", "帮助", "试跑"}
CONTROL_SUBS = {"暂停", "恢复", "买", "卖", "平", "全平", "撤单", "重置", "补给"}
# 注意：试跑只做一次决策（不下单），群友也可以围观，因此放在查询类。

_COMMAND_RE = re.compile(r"^/(?:外汇|fx)(?:\s+(?P<rest>.*))?$", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(万)?\s*(?:元|人民币)?\s*$")


class CommandError(ValueError):
    """命令解析/参数错误（消息带原因码，如 ``INVALID_UNITS``）。"""


@dataclass
class CommandServices:
    """命令层依赖（由 plugin.py 在运行时构造）。

    - ``account``：当前可变 ``AccountState``（补给会直接改它并落库）；
    - ``positions`` / ``quotes``：无参回调，保证拿到调度器最新内存状态；
    - ``pairs``：品种代码 → ``PairSpec``；
    - ``ledger``：``ledger.Ledger``（补给/记录/隔夜用）；
    - ``engine``：``scheduler.TradingEngine``（duck-type）；
    - ``actions``：可选回调字典，优先于 engine 同名方法：
      ``pause/resume/reset/manual/close/cancel/dry_run``。
    """

    cfg: Any = None
    account: Any = None
    positions: Callable[[], list[Any]] = field(default_factory=lambda: list)
    quotes: Callable[[], dict[str, Any]] = field(default_factory=dict)
    pairs: dict[str, Any] = field(default_factory=dict)
    ledger: Any = None
    engine: Any = None
    actions: dict[str, Callable[..., Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """解析 ``/外汇 [子命令] [参数...]``，返回 ``(规范子命令, 参数列表)``。

    非本插件命令返回 ``None``；只有 ``/外汇`` 没带参数时默认「状态」。
    """

    if not isinstance(text, str):
        return None
    match = _COMMAND_RE.match(text.strip())
    if match is None:
        return None
    rest = (match.group("rest") or "").strip()
    tokens = rest.split() if rest else []
    raw_sub = tokens[0] if tokens else ""
    args = tokens[1:]
    canonical = SUB_ALIASES.get(raw_sub) or SUB_ALIASES.get(raw_sub.lower())
    if canonical is None:
        return raw_sub, args
    return canonical, args


def _normalize_admin_entry(entry: str) -> str:
    return str(entry or "").replace("：", ":").strip()


def is_admin(user_id: str, platform: str, admins: list[str] | None) -> bool:
    """判断是否管理员。

    支持两种白名单写法（``[admin] users``）：
    - ``平台:用户ID``（如 ``qq:123456``）：平台与用户都匹配才算；
    - 纯用户 ID（如 ``123456``）：只校验用户 ID（兼容旧配置）。
    """

    user_text = str(user_id or "").replace("：", ":").strip()
    platform_text = str(platform or "").strip().lower()
    if not user_text:
        return False
    if ":" in user_text:  # 宿主直接把 "qq:123456" 传进来
        head, _, tail = user_text.partition(":")
        if tail:
            platform_text = platform_text or head.strip().lower()
            user_text = tail.strip()
    for raw in admins or []:
        entry = _normalize_admin_entry(raw)
        if not entry:
            continue
        if ":" in entry:
            head, _, tail = entry.partition(":")
            if platform_text and head.strip().lower() != platform_text:
                continue
            if tail.strip() and tail.strip() == user_text:
                return True
        elif entry == user_text:
            return True
    return False


def _admin_users(cfg: Any) -> list[str]:
    admin_cfg = getattr(cfg, "admin", None)
    users = getattr(admin_cfg, "users", None) if admin_cfg is not None else None
    if users is None and isinstance(cfg, dict):
        users = (cfg.get("admin") or {}).get("users")
    return [str(item) for item in (users or [])]


def parse_units(text: str) -> int:
    """数量量解析：``1万`` / ``1万单位`` / ``0.5万`` / ``10000`` / ``5,000``。"""

    raw = str(text or "").strip().replace(",", "")
    if not raw:
        raise CommandError("INVALID_UNITS：缺少数量（例：1万 / 10000）")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*万(?:单位|货币|通[货貨])?", raw)
    if match is not None:
        units = Decimal(match.group(1)) * Decimal(10_000)
    else:
        try:
            units = Decimal(raw)
        except (InvalidOperation, ValueError) as exc:
            raise CommandError(f"INVALID_UNITS：数量无法解析 {text!r}") from exc
    if units != units.to_integral_value():
        raise CommandError(f"INVALID_UNITS：数量必须是整数，收到 {text!r}")
    value = int(units)
    if value <= 0:
        raise CommandError(f"INVALID_UNITS：数量必须为正，收到 {text!r}")
    if value % 1_000 != 0:
        raise CommandError(f"INVALID_UNITS：数量必须是 1,000 的整数倍，收到 {value}")
    return value


def parse_cny_amount(text: str) -> int:
    """元金额解析（补给用）：``10万`` / ``100000`` / ``10万元`` / ``100000元``。"""

    raw = str(text or "").strip().replace(",", "")
    if not raw:
        raise CommandError("INVALID_AMOUNT：缺少金额（例：10万）")
    match = _AMOUNT_RE.match(raw)
    if match is None:
        raise CommandError(f"INVALID_AMOUNT：金额无法解析 {text!r}")
    amount = Decimal(match.group(1))
    if match.group(2) == "万":
        amount *= Decimal(10_000)
    if amount != amount.to_integral_value():
        raise CommandError(f"INVALID_AMOUNT：金额必须是整数元，收到 {text!r}")
    value = int(amount)
    if value <= 0:
        raise CommandError(f"INVALID_AMOUNT：金额必须为正，收到 {text!r}")
    return value


def _parse_price(text: str) -> Decimal:
    raw = str(text or "").strip().replace(",", "")
    try:
        price = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise CommandError(f"INVALID_PRICE：价格无法解析 {text!r}") from exc
    if not price.is_finite() or price <= 0:
        raise CommandError(f"INVALID_PRICE：价格必须为正数，收到 {text!r}")
    return price


def _parse_trade_args(args: list[str], *, default_units: int = 10_000) -> dict[str, Any]:
    """解析「买/卖」参数：``品种 [数量] [市价|限价 价|止损 价] ...``。

    支持关键字（大小写/中文混写）：
    - 数量：``1万`` / ``10000``（缺省 1万单位）
    - 委托方式：``市价`` / ``market``；``限价`` / ``limit`` + 价格；``触发`` / ``stop`` + 触发价
    - 附加：``止损 <价格>`` / ``止盈 <价格>``（挂在成单后的保护单上）
    """

    if not args:
        raise CommandError("INVALID_ARGS：用法：/外汇 买 美元日元 1万 限价 158.5 止损 158.0")
    pair_text = str(args[0]).strip()
    units = default_units
    order_type = "market"
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    index = 1
    while index < len(args):
        token = str(args[index]).strip()
        lowered = token.lower()
        if lowered in ("市价", "market", "市价单"):
            order_type = "market"
            index += 1
        elif lowered in ("限价", "limit"):
            if index + 1 >= len(args):
                raise CommandError("INVALID_ARGS：限价单缺少价格（例：限价 158.5）")
            order_type = "limit"
            price = _parse_price(args[index + 1])
            index += 2
        elif lowered in ("触发", "触价", "停损", "stop"):
            if index + 1 >= len(args):
                raise CommandError("INVALID_ARGS：止损单缺少触发价（例：止损 158.0）")
            order_type = "stop"
            price = _parse_price(args[index + 1])
            index += 2
        elif lowered in ("止损", "止损价", "sl", "保护"):
            if index + 1 >= len(args):
                raise CommandError("INVALID_ARGS：止损缺少价格（例：止损 158.0）")
            stop_loss = _parse_price(args[index + 1])
            index += 2
        elif lowered in ("止盈", "止盈", "take_profit", "tp"):
            if index + 1 >= len(args):
                raise CommandError("INVALID_ARGS：止盈缺少价格（例：止盈 159.5）")
            take_profit = _parse_price(args[index + 1])
            index += 2
        elif re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:万|通[货貨])?", token.replace(",", "")):
            units = parse_units(token)
            index += 1
        else:
            raise CommandError(f"INVALID_ARGS：无法识别的参数 {token!r}")
    return {
        "pair_text": pair_text,
        "units": units,
        "order_type": order_type,
        "price": price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


# ---------------------------------------------------------------------------
# 调用辅助（同步/异步回调都能用；无 actions 时回退 engine）
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _action(services: CommandServices, key: str) -> Callable[..., Any] | None:
    actions = services.actions or {}
    fn = actions.get(key)
    if callable(fn):
        return fn
    engine = services.engine
    if engine is None:
        return None
    fallback = {
        "pause": "pause",
        "resume": "resume",
        "reset": "reset",
        "manual": "manual_order",
        "close": "close_position",
        "cancel": "cancel_order",
        "dry_run": "dry_run",
    }.get(key)
    if fallback is None:
        return None
    candidate = getattr(engine, fallback, None)
    return candidate if callable(candidate) else None


async def _invoke(
    services: CommandServices,
    key: str,
    *args: Any,
    required: bool = True,
    **kwargs: Any,
) -> tuple[bool, Any]:
    """调用 action；返回 ``(是否调用了回调, 回调返回值)``。``required=False`` 时不报错。"""

    fn = _action(services, key)
    if fn is None:
        if required:
            raise CommandError(f"UNSUPPORTED_ACTION：当前版本不支持 {key}")
        return False, None
    return True, await _maybe_await(fn(*args, **kwargs))


def _positions(services: CommandServices) -> list[Any]:
    getter = services.positions
    if callable(getter):
        result = getter()
    else:  # 容忍直接传 list
        result = getter
    return list(result or [])


def _quotes(services: CommandServices) -> dict[str, Any]:
    getter = services.quotes
    if callable(getter):
        result = getter()
    else:
        result = getter
    return dict(result or {})


def _account(services: CommandServices) -> Any:
    account = services.account
    if callable(account):
        return account()
    return account


def _rates(quotes: dict[str, Any]) -> FxRates | None:
    """从行情字典取换算锚（USDJPY + USDCNY）；任一缺失返回 ``None``。"""

    return fxconv.rates_from_quotes(quotes)


def _price_text(order_type: str, price: Decimal | None) -> str:
    """委托方式 + 价格（市价只出现一次，避免「市价｜市价」重复）。"""

    if price is not None:
        label = {"limit": "限价", "stop": "触发"}.get(order_type, "委托价")
        return f"{label} {report.fmt_price(price)}"
    return {"market": "市价", "limit": "限价", "stop": "触发"}.get(order_type, "市价")


def _ok_text(text: str) -> str:
    return report.append_disclaimer(text)


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------


async def _cmd_status(services: CommandServices) -> str:
    quotes = _quotes(services)
    account = _account(services)
    cfg = services.cfg
    pending: list[Any] = []
    try:
        load_pending = getattr(services.ledger, "load_orders_pending", None)
        if callable(load_pending):
            pending = load_pending()
    except Exception:  # noqa: BLE001 - 挂单查询不影响状态
        pending = []
    engine = services.engine
    leverage = int(getattr(getattr(cfg, "account", None), "leverage", 10) or 10)
    return report.status(
        account,
        _positions(services),
        quotes,
        services.pairs,
        _rates(quotes),
        pending=pending,
        leverage=leverage,
        auto_trade=bool(getattr(getattr(cfg, "decision", None), "auto_trade", False)),
        paused=bool(getattr(engine, "paused", False)),
    )


async def _cmd_positions(services: CommandServices) -> str:
    quotes = _quotes(services)
    return report.positions_list(_positions(services), quotes, services.pairs, _rates(quotes))


async def _cmd_pending(services: CommandServices) -> str:
    orders: list[Any] = []
    load_pending = getattr(services.ledger, "load_orders_pending", None)
    if callable(load_pending):
        orders = load_pending()
    return report.pending_list(orders)


async def _cmd_quotes(services: CommandServices) -> str:
    market_cfg = getattr(services.cfg, "market", None)
    cny_reference = bool(getattr(market_cfg, "cny_reference_enabled", True))
    return report.quotes_overview(
        _quotes(services), services.pairs, cny_reference=cny_reference
    )


async def _cmd_records(services: CommandServices, args: list[str]) -> str:
    limit = 10
    if args:
        try:
            limit = int(str(args[0]).strip())
        except ValueError as exc:
            raise CommandError(f"INVALID_ARGS：记录条数必须是 1-50 的整数，收到 {args[0]!r}") from exc
        limit = max(1, min(50, limit))
    trades: list[Any] = []
    list_trades = getattr(services.ledger, "list_trades", None)
    if callable(list_trades):
        trades = list_trades(limit)
    return report.record_list(trades)


async def _cmd_margin(services: CommandServices) -> str:
    quotes = _quotes(services)
    leverage = int(getattr(getattr(services.cfg, "account", None), "leverage", 10) or 10)
    return report.margin_overview(
        _account(services),
        _positions(services),
        quotes,
        services.pairs,
        _rates(quotes),
        leverage=leverage,
    )


async def _cmd_swaps(services: CommandServices, args: list[str]) -> str:
    limit = 10
    if args:
        try:
            limit = max(1, min(50, int(str(args[0]).strip())))
        except ValueError as exc:
            raise CommandError(f"INVALID_ARGS：条数必须是 1-50 的整数，收到 {args[0]!r}") from exc
    records: list[dict[str, Any]] = []
    list_swaps = getattr(services.ledger, "list_swaps", None)
    if callable(list_swaps):
        records = list_swaps(limit)
    return report.swap_list(records, services.pairs)


async def _cmd_trade(services: CommandServices, args: list[str], *, side: str) -> str:
    parsed = _parse_trade_args(args)
    pair_text = parsed["pair_text"]
    order_type = parsed["order_type"]
    price = parsed["price"]
    units = parsed["units"]
    side_text = "买入" if side == "buy" else "卖出"
    user_text = ""
    called, result = await _invoke(
        services,
        "manual",
        pair_text=pair_text,
        side=side,
        units=units,
        order_type=order_type,
        price=price,
        user=user_text,
    )
    if not called:
        raise CommandError("UNSUPPORTED_ACTION：当前版本不支持手动下单")
    ok, detail = _unpack_result(result)
    if not ok:
        return report.card_notice(f"❌ {side_text}失败", [detail])
    lines = [
        f"品种　{pair_text}",
        f"数量　{units:,} 货币",
        f"委托　{_price_text(order_type, price)}",
        f"回执　{detail}",
    ]
    stop_loss = parsed["stop_loss"]
    take_profit = parsed["take_profit"]
    if stop_loss is not None or take_profit is not None:
        try:
            notes = await _attach_protection(services, parsed, side)
        except Exception as exc:  # noqa: BLE001 - 保护单失败不改变主单结果
            notes = [f"保护单异常：{type(exc).__name__}: {exc}"]
        if notes:
            lines.extend(["", "🛡 保护单"])
            lines.extend(notes)
    return report.card_notice(
        f"✅ 已提交{side_text}委托",
        lines,
        footer="📝 成交后会单独发成交播报",
    )


async def _attach_protection(services: CommandServices, parsed: dict[str, Any], side: str) -> list[str]:
    """开仓后挂保护单：止损 → close stop；止盈 → close limit（尽力而为，不阻塞主回复）。"""

    close_side = "sell" if side == "buy" else "buy"
    notes: list[str] = []
    manual = _action(services, "manual")
    if manual is None:
        return ["⚠️ 当前版本不支持追加保护单"]
    for key, order_type in (("stop_loss", "stop"), ("take_profit", "limit")):
        price = parsed.get(key)
        if price is None:
            continue
        try:
            result = manual(
                pair_text=parsed["pair_text"],
                side=close_side,
                units=parsed["units"],
                order_type=order_type,
                price=price,
                user="",
                position_type="close",
            )
            result = await _maybe_await(result)
            ok, detail = _unpack_result(result)
            label = "止损" if key == "stop_loss" else "止盈"
            notes.append(f"📌 {label}单{'已挂上' if ok else '挂单失败'}（{detail}）")
        except Exception as exc:  # noqa: BLE001 - 保护单失败不改变主单结果
            notes.append(f"⚠️ 保护单异常：{type(exc).__name__}: {exc}")
    return notes


def _unpack_result(result: Any) -> tuple[bool, str]:
    """把回调返回值统一成 ``(ok, detail)``：支持 bool / tuple / str / None。"""

    if isinstance(result, tuple):
        if len(result) >= 2:
            return bool(result[0]), str(result[1])
        if len(result) == 1:
            return bool(result[0]), ""
        return True, ""
    if isinstance(result, bool):
        return result, ""
    if result is None:
        return True, "已执行"
    return True, str(result)


async def _cmd_close(services: CommandServices, args: list[str]) -> str:
    if not args:
        raise CommandError("INVALID_ARGS：用法：/外汇 平 美元日元")
    pair_text = str(args[0]).strip()
    called, result = await _invoke(services, "close", pair_text, required=False)
    if not called:
        raise CommandError("UNSUPPORTED_ACTION：当前版本不支持手动平仓")
    ok, detail = _unpack_result(result)
    if not ok:
        return report.card_notice("❌ 平仓失败", [detail])
    return report.card_notice("✅ 已提交平仓委托", [f"品种　{pair_text}", f"回执　{detail}"])


async def _cmd_close_all(services: CommandServices) -> str:
    positions = _positions(services)
    if not positions:
        return report.card_notice("📭 当前没有持仓", ["没有需要平的持仓"], footer="📝 全平只对当前持仓生效")
    codes = sorted({str(getattr(position, "pair", "")) for position in positions if getattr(position, "pair", "")})
    close_fn = _action(services, "close")
    if close_fn is None:
        raise CommandError("UNSUPPORTED_ACTION：当前版本不支持手动平仓")
    notes: list[str] = []
    for code in codes:
        result = await _maybe_await(close_fn(code))
        ok, detail = _unpack_result(result)
        mark = "🟢" if ok else "🔴"
        notes.append(f"{mark} {report.pair_name(services.pairs, code)} {'已提交' if ok else '失败'}（{detail}）")
    return report.card_notice("✅ 全平处理完成", notes)


async def _cmd_cancel(services: CommandServices, args: list[str]) -> str:
    if not args:
        raise CommandError("INVALID_ARGS：用法：/外汇 撤单 <订单号>")
    order_id = str(args[0]).strip()
    called, result = await _invoke(services, "cancel", order_id, required=False)
    if not called:
        raise CommandError("UNSUPPORTED_ACTION：当前版本不支持撤单")
    ok, detail = _unpack_result(result)
    if not ok:
        return report.card_notice("❌ 撤单失败", [f"订单号　{order_id}", detail])
    return report.card_notice("✅ 已撤单", [f"订单号　{order_id}", f"回执　{detail}"])


async def _cmd_pause(services: CommandServices) -> str:
    await _invoke(services, "pause", required=False)
    return report.card_notice(
        "⏸ 自动交易已暂停",
        ["行情/风控/隔夜利息仍在运行", "恢复用：/外汇 恢复"],
    )


async def _cmd_resume(services: CommandServices) -> str:
    await _invoke(services, "resume", required=False)
    return report.card_notice("▶️ 自动交易已恢复", ["麦麦会按设定间隔继续决策"])


async def _cmd_reset(services: CommandServices, args: list[str]) -> str:
    if not args or str(args[0]).strip() not in ("确认", "confirm", "CONFIRM"):
        return report.card_notice(
            "🔄 重置账户",
            ["⚠️ 会清空虚拟账户与全部成交记录", "确认请发送：/外汇 重置 确认"],
            footer="📝 重置不可撤销（仅模拟盘）",
        )
    await _invoke(services, "reset", required=False)
    cfg = services.cfg
    initial = getattr(getattr(cfg, "account", None), "initial_balance", 100_000)
    return report.card_notice(
        "🔄 已重置本金",
        [f"本金　{report.fmt_money(initial)}", "账本已清空并重新开户"],
    )


async def _cmd_deposit(services: CommandServices, args: list[str]) -> str:
    if not args:
        raise CommandError("INVALID_ARGS：用法：/外汇 补给 10万")
    amount = parse_cny_amount(args[0])
    account = _account(services)
    ledger = services.ledger
    if account is None or ledger is None:
        raise CommandError("UNSUPPORTED_ACTION：账本未就绪，无法补给")
    account.balance = int(getattr(account, "balance", 0) or 0) + amount
    # 补给同时抬高「初始本金」，保证账本恒等式 balance = initial + Σ已实现 − Σ手续费 + Σswap 成立
    account.initial_balance = int(getattr(account, "initial_balance", 0) or 0) + amount
    save_account = getattr(ledger, "save_account", None)
    if callable(save_account):
        save_account(account)
    return report.card_notice(
        "💰 已补给虚拟资金",
        [
            f"补给　{report.fmt_money(amount)}",
            f"现金　{report.fmt_money(account.balance)}",
            f"本金　{report.fmt_money(account.initial_balance)}",
        ],
        footer="📝 模拟盘补给，不涉及任何真实资金",
    )


async def _cmd_dry_run(services: CommandServices) -> str:
    called, result = await _invoke(services, "dry_run", required=False)
    if not called:
        return report.card_notice("🧪 试跑不可用", ["当前版本不支持试跑"])
    return str(result) if result else report.card_notice("🧪 试跑完成", ["本轮只做决策，未下单"])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _response_allowed(services: CommandServices, platform: str, group_id: str | None) -> bool:
    """白名单判断：非白名单群/平台/未开启私聊 → 不响应。"""

    cfg = services.cfg
    notify = getattr(cfg, "notify", None)
    if notify is None and isinstance(cfg, dict):
        notify = cfg.get("notify")
    if notify is None:
        return True
    expected_platform = str(getattr(notify, "platform", "") or "").strip().lower()
    current_platform = str(platform or "").strip().lower()
    if expected_platform and current_platform and expected_platform != current_platform:
        return False
    group_text = str(group_id or "").strip()
    if group_text:
        allowed = {str(item).strip() for item in (getattr(notify, "group_ids", None) or [])}
        return bool(allowed) and group_text in allowed
    return bool(getattr(notify, "private_enabled", False))


async def handle(
    services: CommandServices,
    *,
    platform: str,
    user_id: str,
    group_id: str | None,
    text: str,
) -> str | None:
    """执行 ``/外汇`` 命令并返回回复文本；不该响应时返回 ``None``。

    - 非命令 / 非白名单群 / 未开启私聊 → ``None``；
    - 控制类命令非管理员 → 提示权限不足（返回文本，不执行）；
    - 任何内部异常都转成用户可读文本，**不**向上抛（避免宿主日志刷异常栈）。
    """

    parsed = parse_command(text)
    if parsed is None:
        return None
    if not _response_allowed(services, platform, group_id):
        return None
    sub, args = parsed
    canonical = SUB_ALIASES.get(sub) or SUB_ALIASES.get(sub.lower()) or sub

    admin = is_admin(user_id, platform, _admin_users(services.cfg))
    if canonical in CONTROL_SUBS and not admin:
        return report.card_notice(
            "🔒 仅管理员可用",
            ["这个命令只有管理员能用", "在插件设置页「管理员」名单里添加 qq:QQ号"],
        )
    if canonical not in QUERY_SUBS and canonical not in CONTROL_SUBS:
        return report.append_disclaimer(f"⚠️ 未知子命令：{sub}\n\n" + report.help_text())

    if canonical == "状态":
        try:
            return await _cmd_status(services)
        except Exception as exc:  # noqa: BLE001 - 命令层兜底
            return report.card_notice("⚠️ 命令执行出错", [f"{type(exc).__name__}: {exc}"])
    try:
        if canonical == "持仓":
            return await _cmd_positions(services)
        if canonical == "挂单":
            return await _cmd_pending(services)
        if canonical == "行情":
            return await _cmd_quotes(services)
        if canonical == "记录":
            return await _cmd_records(services, args)
        if canonical == "保证金":
            return await _cmd_margin(services)
        if canonical == "隔夜":
            return await _cmd_swaps(services, args)
        if canonical == "帮助":
            return report.help_text()
        if canonical == "试跑":
            return await _cmd_dry_run(services)
        if canonical == "暂停":
            return await _cmd_pause(services)
        if canonical == "恢复":
            return await _cmd_resume(services)
        if canonical == "买":
            return await _cmd_trade(services, args, side="buy")
        if canonical == "卖":
            return await _cmd_trade(services, args, side="sell")
        if canonical == "平":
            return await _cmd_close(services, args)
        if canonical == "全平":
            return await _cmd_close_all(services)
        if canonical == "撤单":
            return await _cmd_cancel(services, args)
        if canonical == "重置":
            return await _cmd_reset(services, args)
        if canonical == "补给":
            return await _cmd_deposit(services, args)
    except CommandError as exc:
        return report.card_notice("⚠️ 参数有问题", [str(exc)])
    except Exception as exc:  # noqa: BLE001 - 命令层兜底，避免异常抛给宿主
        return report.card_notice("⚠️ 命令执行出错", [f"{type(exc).__name__}: {exc}"])
    return _ok_text(f"⚠️ 未知子命令：{sub}\n\n" + report.help_text())
