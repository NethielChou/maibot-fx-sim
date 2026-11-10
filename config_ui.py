"""WebUI 配置页中文化与分组（常用 / 交易 / 高级 三个页签）。

只**重组** SDK 生成的配置 Schema（页签/分组/字段标题/提示/隐藏/禁用），
不改配置模型、默认值和 TOML 路径（键路径仍是 ``account.initial_balance`` 等）。

约定：
- 输出结构沿用 SDK：``{"plugin_id", "plugin_info", "sections": {...}, "layout": {...}}``；
- ``sections`` 的 key 是 UI 分组 id（可多个分组映射到同一个配置节，如 market/market_advanced）；
- ``layout`` 是 ``{"type": "tabs", "tabs": [{"id","title","sections":[...]}]}``；
- 开发者/P0 未启用的字段放进 ``_UI_HIDDEN``（仍保留在 schema 里，只是不展示）；
- 输入 schema 不被原地修改（返回新 dict）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DISCLAIMER_HINT = "模拟盘参数，仅影响本地虚拟账户，不涉及真实资金。"


@dataclass(frozen=True)
class _UiSection:
    """配置页里的一个展示块。"""

    source: str
    title: str
    description: str
    fields: dict[str, str]
    collapsed: bool = False


_UI_SECTIONS: dict[str, _UiSection] = {
    # ---------- 常用：账户 ----------
    "account_core": _UiSection(
        source="account",
        title="虚拟账户",
        description="初始资金、杠杆与下单单位；默认 10 万元、10 倍、1万单位，新手保持默认即可。",
        fields={
            "initial_balance": "初始资金（元）",
            "leverage": "杠杆倍数（1–25，默认 10）",
            "unit_contracts": "1 个合约单位（货币）",
            "min_units": "最小下单单位（货币）",
            "loss_cut_ratio": "强平线（保证金维持率 %）",
            "commission_per_10k": "每 1万单位手续费（元）",
        },
    ),
    # ---------- 常用：播报 ----------
    "notify": _UiSection(
        source="notify",
        title="播报与群聊",
        description="麦麦把交易汇报发到哪个群，以及是否合并成一条转发消息防刷屏。",
        fields={
            "platform": "播报平台",
            "group_ids": "允许响应的群号",
            "private_enabled": "允许私聊",
            "event_reports": "开平仓即时播报",
            "merge_messages": "合并转发防刷屏",
            "merge_window_ms": "合并等待窗口（毫秒）",
        },
    ),
    # ---------- 常用：管理员 ----------
    "admin": _UiSection(
        source="admin",
        title="管理员",
        description="只有名单里的账户能暂停/下单/平仓/补给/重置；格式 平台:用户ID，例如 qq:123456。",
        fields={"users": "管理员账户"},
    ),
    # ---------- 交易：品种 / 订单 / 风控 / 决策 ----------
    "market_core": _UiSection(
        source="market",
        title="品种与行情节奏",
        description="先选品种白名单，再决定拉行情频率；留空=内置 8 个品种。",
        fields={
            "pairs": "品种白名单（留空=默认 8 个）",
            "quote_poll_seconds": "行情刷新间隔（秒）",
            "stale_quote_seconds": "行情过期判定（秒）",
            "cny_reference_enabled": "显示人民币参考汇率",
        },
    ),
    "orders": _UiSection(
        source="orders",
        title="订单",
        description="挂单数量上限、有效期与允许的订单类型；P0 只支持市价/限价/止损。",
        fields={
            "max_pending": "最多挂单数",
            "default_tif": "默认有效期（GTC/DAY/GTD）",
            "allow_limit": "允许限价（限价）",
            "allow_stop": "允许止损（止损）",
            "quote_stale_fill_seconds": "成交前行情最大年龄（秒）",
        },
    ),
    "spread_core": _UiSection(
        source="spread",
        title="点差与滑点（常用）",
        description="点差/滑点放大会让模拟成交更接近真实零售 FX；默认值已按日式零售口径设置。",
        fields={
            "max_multiplier": "点差/滑点总放大上限",
            "market_max_slip_pips": "市价滑点拒单阈值（pip）",
        },
    ),
    "risk_core": _UiSection(
        source="risk",
        title="风控（常用）",
        description="程序风控优先于麦麦的判断；这些上限决定它最多能冒险到什么程度。",
        fields={
            "risk_per_trade_ratio": "单笔止损风险（占账户净值）",
            "margin_per_order_ratio": "单笔保证金占用上限",
            "max_positions": "最多同时持仓数",
            "daily_trade_limit": "每日开仓上限（笔）",
            "daily_loss_limit_ratio": "当日亏损停手线",
            "max_drawdown_ratio": "历史最大回撤停手线",
        },
    ),
    "decision_core": _UiSection(
        source="decision",
        title="麦麦决策（常用）",
        description="自动交易默认关闭：先看状态/试跑，确认稳定后再打开。",
        fields={
            "auto_trade": "自动交易总开关",
            "llm_enabled": "启用 LLM 决策",
            "interval_minutes": "决策间隔（分钟）",
            "decision_windows": "允许主动开仓的时间窗（北京时间）",
            "max_llm_calls_per_day": "每日模型调用预算",
        },
    ),
    # ---------- 交易：群聊上下文 ----------
    "influence": _UiSection(
        source="influence",
        title="群聊上下文",
        description=(
            "决策前把最近群聊作为参考材料放进上下文（默认关闭）；"
            "不评分、不设权重、不落盘，也不把聊天原文写进日志。"
        ),
        fields={
            "enabled": "启用群聊上下文（参考材料）",
            "group_ids": "读取群号（留空=用播报群名单）",
            "limit": "最多带多少条最近消息",
            "hours": "回看最近多少小时",
            "timeout_seconds": "读取超时（秒）",
        },
    ),
    "swap": _UiSection(
        source="swap",
        title="隔夜利息",
        description="日切（纽约 17:00）按持仓计收/计付隔夜利息；周三按 3 日分。模拟值为近似参数。",
        fields={
            "rollover_time": "日切时刻（纽约时间 HH:MM）",
            "triple_weekday": "三倍利息星期（0=周一…2=周三）",
            "triple_enabled": "启用周三 3 日分",
        },
    ),
    # ---------- 高级 ----------
    "account_advanced": _UiSection(
        source="account",
        title="账户（高级）",
        description="双向持仓、强平方式与追证线等高级账户口径；不懂就保持默认。",
        fields={
            "currency": "账户币种（虚拟人民币）",
            "allow_hedging": "允许双向持仓（同品种反向持仓）",
            "margin_call_ratio": "保证金维持率告警/禁开新仓线（%）",
            "liquidation_mode": "强平方式（all=一次性全部平仓）",
            "swap_enabled": "计收/计付隔夜利息",
        },
        collapsed=True,
    ),
    "market_advanced": _UiSection(
        source="market",
        title="行情采集（高级）",
        description="一般不需要改；网络慢或行情异常时才调整。",
        fields={
            "quote_timeout_seconds": "行情请求超时（秒）",
            "quote_jitter_ratio": "轮询抖动（防 WAF）",
            "weekend_valuation": "周末用最后有效价估值",
            "usdcny_enabled": "行情附带 USDCNY 换算锚",
        },
        collapsed=True,
    ),
    "spread_advanced": _UiSection(
        source="spread",
        title="时间窗点差乘数（高级）",
        description="换日、周一开盘、经济指标前后会放大点差；总倍率受上限约束。",
        fields={
            "rollover_multiplier": "换日倍率",
            "monday_open_multiplier": "周一开盘 1 小时倍率",
            "news_core_multiplier": "指标核心 ±2 分钟倍率",
            "news_edge_multiplier": "指标前后 ±15 分钟倍率",
        },
        collapsed=True,
    ),
    "risk_advanced": _UiSection(
        source="risk",
        title="风控（高级）",
        description="单品种上限、逆势加仓禁止与强平冷却；高频调参可能导致玩法失衡。",
        fields={
            "max_units_per_pair": "单品种最大持仓（货币）",
            "forbid_add_to_loser": "禁止对浮亏品种同向加仓",
            "loss_cut_cooldown_minutes": "强平后冷却（分钟）",
            "post_loss_cut_resume_ratio": "强平后恢复开仓所需保证金维持率 %",
        },
        collapsed=True,
    ),
    "decision_advanced": _UiSection(
        source="decision",
        title="模型与输出（高级）",
        description="模型任务名、温度、输出长度与超时；改错会导致决策失败（失败会自动 hold）。",
        fields={
            "decision_task_name": "决策模型任务名",
            "report_task_name": "播报模型任务名",
            "temperature": "决策温度",
            "prompt_char_limit": "决策 prompt 字符上限",
            "decision_max_tokens": "决策输出 token 上限",
            "report_max_tokens": "播报输出 token 上限",
            "request_timeout_ms": "单次 LLM 超时（毫秒）",
            "max_drift_pips": "决策后允许滑价（pip）",
        },
        collapsed=True,
    ),
    "calendar": _UiSection(
        source="calendar",
        title="经济指标事件窗",
        description="高影响事件（非农/美联储议息等）公布前后禁止新开仓；默认关闭。事件时间按北京时间。",
        fields={
            "enabled": "启用经济指标事件窗",
            "source": "事件来源（auto=真实数据 / static=仅静态表）",
            "refresh_minutes": "真实数据刷新间隔（分钟）",
            "blackout_minutes": "事件窗半径（分钟，前后各这么多）",
            "block_high_impact_only": "只禁止高影响事件",
            "announce_minutes": "提前播报「即将公布」（分钟，0=不播报）",
            "events": "静态事件表（时间|币种|名称|影响，一行一个）",
        },
        collapsed=True,
    ),
    "plugin": _UiSection(
        source="plugin",
        title="插件信息",
        description="版本识别用，保持默认即可。",
        fields={"enabled": "启用插件"},
        collapsed=True,
    ),
}

# 页签 → 分组顺序
_UI_TABS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("basic", ("常用", ("account_core", "notify", "admin"))),
    (
        "trading",
        (
            "交易",
            (
                "market_core",
                "orders",
                "spread_core",
                "risk_core",
                "influence",
                "decision_core",
                "swap",
            ),
        ),
    ),
    (
        "advanced",
        (
            "高级",
            (
                "account_advanced",
                "market_advanced",
                "spread_advanced",
                "risk_advanced",
                "decision_advanced",
                "calendar",
                "plugin",
            ),
        ),
    ),
)

_UI_HINTS: dict[str, str] = {
    "account.initial_balance": "只对新建账户生效；已有账户要改本金，请用 /外汇 重置 确认（或 /外汇 补给 追加）。",
    "account.leverage": "虚拟账户杠杆（1–25，默认 10）；调高会放大盈亏与强平风险。",
    "account.unit_contracts": "1万单位=10,000；下单单位按 1,000 货币步长。",
    "account.min_units": "最小 1,000 货币（0.1 万）；小于该值的下单会被拒绝。",
    "account.loss_cut_ratio": "保证金维持率低于该值即一次性全部平仓；默认 50%（日本零售 FX 惯例）。",
    "account.commission_per_10k": "默认 0：手续费已内含在点差里；设为正数则每边按比例扣元。",
    "account.currency": "固定为虚拟人民币（CNY/元），不支持修改。",
    "account.allow_hedging": "关闭时同品种反向开仓会被拒绝；双向持仓保证金取大边。",
    "account.margin_call_ratio": "保证金维持率低于该值时禁止开新仓并提示；默认 100%。",
    "account.liquidation_mode": "all=全部强平（日式主流）；sequential=逐笔平到保证金维持率恢复。",
    "account.swap_enabled": "关闭后不收/不付隔夜利息；只影响玩法，不影响账务一致性。",
    "market.pairs": "填 USDJPY/EURJPY 等代码；留空=内置 8 个品种。修改后需重载插件。",
    "market.quote_poll_seconds": "多久拉一次新浪行情；太小会增加网络请求，默认 15 秒。",
    "market.stale_quote_seconds": "行情时间戳超过该秒数视为过期，禁止撮合/决策。",
    "market.quote_timeout_seconds": "单次行情请求最多等多久；网络差可适当加大。",
    "market.quote_jitter_ratio": "轮询间隔随机抖动比例，降低被行情源限流的概率。",
    "market.weekend_valuation": "周末休市时用最后有效价估值（允许显示、禁止成交）。",
    "market.usdcny_enabled": "行情里顺带拉 USDCNY（在岸人民币）作为换算锚；关闭后无法把金额换算成元，会拒绝开仓。",
    "market.cny_reference_enabled": "行情卡底部是否显示「人民币参考汇率」栏（USDCNY/EURCNY/JPYCNY 等 8 个参考对）；只展示、不可交易，关闭后只显示可交易品种。",
    "orders.max_pending": "同时存在的在途挂单上限；超过会被拒绝（MAX_OPEN_ORDERS）。",
    "orders.default_tif": "GTC=一直有效；DAY=日切过期；GTD=到 expires_at 过期。",
    "orders.allow_limit": "关闭后限价（限价）单会被拒绝。",
    "orders.allow_stop": "关闭后止损（止损）单会被拒绝；不建议关闭。",
    "orders.quote_stale_fill_seconds": "撮合前行情年龄超过该值 → 挂起等新鲜报价，不丢单。",
    "spread.max_multiplier": "点差/滑点放大总倍率上限（时间窗/波动叠加后的封顶）。",
    "spread.market_max_slip_pips": "市价成交滑点超过该值直接拒单（保护性）。",
    "spread.rollover_multiplier": "纽约 17:00 前后 15 分钟点差放大倍率。",
    "spread.monday_open_multiplier": "周一开盘首小时点差放大倍率。",
    "spread.news_core_multiplier": "重要经济指标 ±2 分钟的点差放大倍率。",
    "spread.news_edge_multiplier": "指标前后 ±15 分钟的点差放大倍率。",
    "risk.risk_per_trade_ratio": "单笔止损最大风险占账户净值比例；决定程序允许的最大下单量。",
    "risk.margin_per_order_ratio": "单笔订单保证金占账户净值上限。",
    "risk.max_positions": "最多同时持有几笔持仓（Ticket 计），不是手数。",
    "risk.daily_trade_limit": "每天最多开仓几笔；平仓与强平不计入。",
    "risk.daily_loss_limit_ratio": "当日亏损达到该比例后停手（禁止开新仓）。",
    "risk.max_drawdown_ratio": "历史最高账户净值回撤达到该比例后暂停自动交易。",
    "risk.max_units_per_pair": "单品种最大持仓数量（多个 Ticket 合计）。",
    "risk.forbid_add_to_loser": "禁止对浮亏方向同向加仓（逆势加仓），防止越亏越加。",
    "risk.loss_cut_cooldown_minutes": "强平后多少分钟内禁止自动开新仓。",
    "risk.post_loss_cut_resume_ratio": "强平后需要恢復到的保证金维持率（%）才允许自动开仓。",
    "decision.auto_trade": "总开关，默认关闭。建议先用 /外汇 状态、/外汇 试跑 验证后再开。",
    "decision.llm_enabled": "关闭后不调用模型、不自动开新仓；风控/行情/隔夜利息仍运行。",
    "decision.interval_minutes": "麦麦每隔多久做一次新决策；不是行情刷新间隔。",
    "decision.decision_windows": "北京时间窗口，格式 HH:MM-HH:MM；空=全天允许。",
    "decision.max_llm_calls_per_day": "每日模型调用预算（含失败重试）；用完后当天不再自动决策。",
    "decision.decision_task_name": "宿主模型任务名，默认 planner；不懂不要改。",
    "decision.report_task_name": "播报模型任务名，默认 replyer；不懂不要改。",
    "decision.temperature": "决策温度：越低越稳定，越高越随机。",
    "decision.prompt_char_limit": "一次决策 prompt 的最大字符数；太小会截断上下文。",
    "decision.decision_max_tokens": "模型输出交易 JSON 的 token 上限。",
    "decision.report_max_tokens": "模型生成播报文字的 token 上限。",
    "decision.request_timeout_ms": "单次模型调用最长等待；超时会返回 hold（不交易）。",
    "decision.max_drift_pips": "决策价与执行价偏离超过该 pip 数就放弃本单（防追高杀低）。",
    "swap.rollover_time": "纽约当地 17:00 日切；北京冬令时 06:00 / 夏令时 05:00。",
    "swap.triple_weekday": "0=周一…2=周三；周三按 3 日分（T+2 交割惯例）。",
    "swap.triple_enabled": "关闭后所有日切都按 1 日分。",
    "notify.platform": "需与宿主适配器平台一致（P0 支持 qq）。",
    "notify.group_ids": "行情/命令/播报只在这几个群里生效；留空=不响应群聊。",
    "notify.private_enabled": "默认关闭；开启前确认私聊对象可信。",
    "notify.event_reports": "开仓/平仓/强平/利息事件是否立即播报。",
    "notify.merge_messages": "把多段消息合成一张转发卡片，防止刷屏；失败自动回退纯文本。",
    "notify.merge_window_ms": "同一次事件的多段消息等待多久再合并；默认 800 毫秒。",
    "admin.users": "格式 平台:用户ID（如 qq:123456），一行一个；留空=没有管理员，控制命令全部不可用。",
    "influence.enabled": "默认关闭；开启后决策前把最近群聊作为参考材料放进上下文（不评分、不设权重、不保存原文与昵称到磁盘）。",
    "influence.group_ids": "填群号；留空=用「播报与群聊」里配置的群。",
    "influence.limit": "最多带多少条最近消息；太多会挤占行情/规则的提示词空间。",
    "influence.hours": "回看最近多少小时的群聊；太长会把旧话题当现在。",
    "influence.timeout_seconds": "读取群聊最长等多久；超时本轮直接跳过，不阻塞交易循环。",
    "calendar.enabled": "开启后高影响事件公布前后禁止麦麦新开仓（已有持仓照常平仓/止损）；默认关闭。",
    "calendar.blackout_minutes": "事件窗半径：公布前后各多少分钟禁止新开仓；默认 30 分钟。",
    "calendar.block_high_impact_only": "关闭后所有登记事件（含中/低影响）都会禁止开新仓。",
    "calendar.announce_minutes": "事件前多少分钟在群里播报一次「即将公布」；0=不播报。",
    "calendar.events": "静态事件表：时间|币种|名称|影响，例如 2026-11-07T20:30:00|USD|非农|high（时间按北京时间）；留空=没有静态事件，写错的行会被忽略并记日志。",
    "calendar.source": "auto=联网取真实财经日历（失败自动回退静态表）；static=只用静态事件表，完全不联网。",
    "calendar.refresh_minutes": "真实日历刷新间隔（分钟，默认 30）；只在内存缓存，不落盘、不写事件正文到日志。",
    "plugin.enabled": "插件总开关（宿主保留键）。",
}

# 开发者/未启用字段：仍保留在 schema，但设置页不展示
_UI_HIDDEN: set[str] = {
    "plugin.config_version",
    "market.source",
    "market.secondary_enabled",
    "orders.allow_trailing",
    "orders.allow_ifd",
    "storage.history_limit",
    "swap.timezone",
}
# 只读展示字段
_UI_DISABLED: set[str] = {
    "account.currency",
    "plugin.config_version",
}


def _copy_field(field: Any) -> dict[str, Any]:
    return dict(field) if isinstance(field, dict) else {}


def build_config_ui_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """在 SDK 生成的 Schema 上补中文页签、分组、字段标题/提示与隐藏标记。

    - 输入不是 dict（如宿主未提供配置模型返回 ``{}``）时原样返回空 dict；
    - 只包含「输入里存在的字段」；输入里多出来的字段（未来新增）不会被删掉——
      若某个配置节的字段没被任何 UI 分组覆盖，会自动归入「高级」页签的兜底分组。
    """

    if not isinstance(schema, dict):
        return schema
    source_sections = schema.get("sections")
    if not isinstance(source_sections, dict) or not source_sections:
        return {**schema, "sections": {}, "layout": {"type": "tabs", "tabs": []}}
    source_sections = {str(key): value for key, value in source_sections.items() if isinstance(value, dict)}

    sections: dict[str, Any] = {}
    tabs: list[dict[str, Any]] = []
    covered: dict[str, set[str]] = {}

    for tab_id, (tab_title, section_ids) in ((item[0], item[1]) for item in _UI_TABS):
        tabs.append({"id": tab_id, "title": tab_title, "sections": list(section_ids)})
        for section_id in section_ids:
            spec = _UI_SECTIONS[section_id]
            template = source_sections.get(spec.source)
            if template is None:
                continue
            template_fields = template.get("fields")
            if not isinstance(template_fields, dict):
                continue
            fields: dict[str, Any] = {}
            for field_name, label in spec.fields.items():
                field_template = template_fields.get(field_name)
                if not isinstance(field_template, dict):
                    continue
                path = f"{spec.source}.{field_name}"
                entry = _copy_field(field_template)
                hint = _UI_HINTS.get(path) or str(entry.get("description") or entry.get("hint") or "")
                entry.update(
                    name=entry.get("name") or field_name,
                    label=label,
                    hint=hint,
                    description=hint,
                    order=len(fields),
                )
                if path in _UI_HIDDEN:
                    entry["hidden"] = True
                if path in _UI_DISABLED:
                    entry["disabled"] = True
                fields[field_name] = entry
                covered.setdefault(spec.source, set()).add(field_name)
            if not fields:
                continue
            sections[section_id] = {
                **template,
                "name": spec.source,
                "title": spec.title,
                "description": spec.description,
                "collapsed": spec.collapsed,
                "order": len(sections),
                "fields": fields,
            }

    # 兜底：输入里有、但上面没覆盖到的字段，避免配置项凭空消失。
    # - 开发者/未启用字段（_UI_HIDDEN）：塞回该配置节「第一个已生成分组」并标 hidden，
    #   这样设置页不展示，但 Schema 里仍保留键路径；
    # - 真正未知的新字段：收进「高级 → 其他设置」。
    visible_leftovers: dict[str, dict[str, Any]] = {}
    for source, template in source_sections.items():
        template_fields = template.get("fields")
        if not isinstance(template_fields, dict):
            continue
        for name, field in template_fields.items():
            if name in covered.get(source, set()):
                continue
            path = f"{source}.{name}"
            entry = _copy_field(field)
            entry.setdefault("name", name)
            if path in _UI_HIDDEN:
                entry["hidden"] = True
                if path in _UI_DISABLED:
                    entry["disabled"] = True
                entry.setdefault("label", name)
                entry.setdefault("hint", "")
                entry.setdefault("description", "")
                target = next(
                    (sid for sid, section in sections.items() if section.get("name") == source),
                    None,
                )
                if target is not None:
                    entry["order"] = len(sections[target]["fields"])
                    sections[target]["fields"][name] = entry
                else:
                    visible_leftovers.setdefault(source, {})[name] = entry
                continue
            entry.setdefault("label", name)
            entry.setdefault("hint", "")
            entry.setdefault("description", "")
            visible_leftovers.setdefault(source, {})[name] = entry
    if visible_leftovers:
        tab = next((item for item in tabs if item["id"] == "advanced"), None)
        section_fields: dict[str, Any] = {}
        for source, missing in visible_leftovers.items():
            for name, field in missing.items():
                path = f"{source}.{name}"
                field["order"] = len(section_fields)
                if path in _UI_DISABLED:
                    field["disabled"] = True
                section_fields[path] = field
        section_id = "other_settings"
        sections[section_id] = {
            "name": "other",
            "title": "其他设置",
            "description": "未归类或新版本新增的配置项（一般不需要改）。",
            "icon": "settings",
            "collapsed": True,
            "order": len(sections),
            "fields": section_fields,
        }
        if tab is not None:
            tab["sections"].append(section_id)

    return {
        **schema,
        "sections": sections,
        "layout": {"type": "tabs", "tabs": tabs},
    }
