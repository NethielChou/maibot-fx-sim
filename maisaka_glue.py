"""让麦麦知道「外汇插件存在」的上下文注入（Maisaka Hook）。

为什么需要
----------
插件注册了 ``/外汇`` 命令，但麦麦本人（Planner）并不会自动知道这件事：
群友问「有没有外汇功能」时，她只能凭自己的常识回答，于是会说"暂时没有外汇功能哦"。
期货插件用同样的方式解决过：在 Planner 请求前把一段**事实**塞进系统消息。

实现约束（实测踩过的坑，见 ``docs/INTERFACES.md``）
------------------------------------------------
1. Hook 载荷里的 ``items`` 是 Host 的 RPC 快照，就地修改不会回传；
   必须返回 ``{"action": "continue", "modified_kwargs": {...}}`` 才生效。
2. ``items`` 的元素结构是 ``{"item_type": "SystemMessageItem", "meta": {...},
   "parts": [{"type": "text", "text": ...}]}``——写错结构不会报错，会被静默忽略。
3. 用 marker 做幂等，避免同一请求被注入多次。
4. 注入失败绝不影响插件加载与交易：一律吞异常、返回 ``None``/``continue``。

本模块只做字符串与结构处理，不碰账本/交易，可完全离线单测。
"""

from __future__ import annotations

import json
from typing import Any

#: 注入块的幂等标记（出现在 parts 里就不再注入）。
INJECT_MARKER = "[外汇插件事实]"

#: 可注入的最小/最大长度（太长的文本会挤占 Planner 上下文）。
MAX_BLOCK_CHARS = 900

__all__ = [
    "INJECT_MARKER",
    "MAX_BLOCK_CHARS",
    "build_fact_block",
    "inject_extra_prompt",
    "inject_fact",
    "has_marker",
]


def build_fact_block(
    *,
    bot_name: str = "麦麦",
    plugin_name: str = "麦麦外汇战士",
    auto_trade: bool = False,
    account_yuan: int | None = None,
    leverage: int | None = None,
    pair_count: int | None = None,
    admin_only_trading: bool = True,
    max_chars: int = MAX_BLOCK_CHARS,
) -> str:
    """生成注入给麦麦的「外汇插件事实」文本（纯函数，便于单测）。"""

    name = str(bot_name or "麦麦").strip() or "麦麦"
    plugin = str(plugin_name or "麦麦外汇战士").strip() or "麦麦外汇战士"
    lines = [
        f"{INJECT_MARKER} 你自己正在运行「{plugin}」插件：",
        f"- 你有一个本地虚拟外汇账户（纯模拟盘，不接任何真实交易账户、不涉及真实资金）。",
    ]
    facts: list[str] = []
    if account_yuan is not None:
        facts.append(f"账户约 {int(account_yuan):,} 元")
    if leverage is not None:
        facts.append(f"{int(leverage)} 倍杠杆")
    if pair_count is not None:
        facts.append(f"{int(pair_count)} 个货币对")
    if facts:
        lines.append("- " + "，".join(facts) + "。")
    if admin_only_trading:
        lines.append("- 只有管理员能下单/平仓；普通群友可以查询、也可以聊外汇。")
    lines.append(
        "- 命令：`/外汇 帮助`、`/外汇 状态`、`/外汇 持仓`、`/外汇 行情`、"
        "`/外汇 保证金`、`/外汇 隔夜`、`/外汇 记录`。"
    )
    lines.append(
        f"- 状态：自动交易当前{'已开启' if auto_trade else '关闭（先观察，不自动下单）'}。"
    )
    who = "" if name == "麦麦" else f"（你是{name}）"
    lines.append(
        f"- 被问到外汇/汇率/换汇/有没有外汇功能时，按上面这些真实情况回答；"
        f"可以邀请对方发 `/外汇 帮助` 或 `/外汇 行情` 试试。"
        f"不要否认这个功能存在，也不要声称能连接真实券商或提供投资建议。"
        f"用你自己的口气说就好{who}。"
    )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def has_marker(items: Any, marker: str = INJECT_MARKER) -> bool:
    """``items`` 里是否已存在含标记的文本 part（幂等判断）。"""

    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and marker in str(part.get("text", "")):
                return True
    return False


def inject_fact(
    kwargs: Any,
    block: str,
    *,
    marker: str = INJECT_MARKER,
) -> dict[str, Any] | None:
    """把 ``block`` 追加进 ``kwargs['items']`` 的第一个 SystemMessageItem。

    返回可直接交给 Hook 的 ``{"action": "continue", "modified_kwargs": ...}``；
    无法注入（结构不符 / 已注入 / 文本为空）时返回 ``None``，由调用方放行原请求。
    """

    if not block or marker not in block:
        return None
    if not isinstance(kwargs, dict):
        return None
    items = kwargs.get("items")
    if not isinstance(items, list) or not items:
        return None
    if has_marker(items, marker):
        return None
    try:
        patched = json.loads(json.dumps(items))  # 深拷贝，避免污染 Host 载荷
    except (TypeError, ValueError):
        return None
    target = None
    for item in patched:
        if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
            target = item
            break
    if target is None:
        return None
    parts = target.get("parts")
    if not isinstance(parts, list):
        return None
    parts.append({"type": "text", "text": block})
    modified = dict(kwargs)
    modified["items"] = patched
    return {"action": "continue", "modified_kwargs": modified}


def inject_extra_prompt(
    kwargs: Any,
    block: str,
    *,
    marker: str = INJECT_MARKER,
) -> dict[str, Any] | None:
    """把 ``block`` 追加进 ``kwargs['extra_prompt']``（replyer.before_request 用）。

    为什么需要单独一条
    ------------------
    ``maisaka.replyer.before_request`` 的载荷**没有** ``items``，只有 ``extra_prompt``
    （追加到本次回复的额外要求）；而最终回复文本由 replyer 生成，所以只注入 Planner
    不足以保证她"知道插件存在"。这里按该 Hook 的契约写 extra_prompt。

    返回 ``{"action": "continue", "modified_kwargs": ...}``；无法注入时返回 ``None``。
    """

    if not block or marker not in block:
        return None
    if not isinstance(kwargs, dict):
        return None
    current = kwargs.get("extra_prompt")
    text = str(current or "")
    if marker in text:
        return None
    merged = block if not text.strip() else f"{text.rstrip()}\n\n{block}"
    modified = dict(kwargs)
    modified["extra_prompt"] = merged
    return {"action": "continue", "modified_kwargs": modified}
