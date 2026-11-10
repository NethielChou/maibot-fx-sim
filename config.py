"""配置模型：麦麦炒外汇（Rev 1.2 口径：虚拟人民币计价 / 10 倍 / 1万单位 / 1,000 货币步长）。

约定：
- 所有字段必须有默认值（宿主 Runner 首次加载时以此生成 config.toml）。
- 不使用 None 当哨兵；TOML 无 null。
- 结构变更时递增 ``CONFIG_VERSION``。
"""

from maibot_sdk import Field, PluginConfigBase

CONFIG_VERSION = "0.3.4"


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件（宿主保留键）")
    config_version: str = Field(default=CONFIG_VERSION, description="配置版本")


class AccountConfig(PluginConfigBase):
    __ui_label__ = "账户"
    __ui_icon__ = "wallet"
    __ui_order__ = 10

    currency: str = Field(default="CNY", description="账户币种；虚拟人民币（元）计价，固定不变")
    initial_balance: float = Field(default=100_000.0, ge=0.0, description="虚拟起始资金（元）")
    leverage: int = Field(default=10, ge=1, le=25, description="杠杆倍数（1–25，默认 10；上限沿用零售 FX 惯例 25 倍）")
    unit_contracts: int = Field(default=10_000, ge=1_000, le=100_000, description="1万单位 = 10,000 货币单位")
    min_units: int = Field(default=1_000, ge=1_000, le=10_000, description="最小下单数量（1,000 货币 = 0.1 万）")
    commission_per_10k: float = Field(default=0.0, ge=0.0, description="每 1万单位单边手续费（元，默认点差内含）")
    allow_hedging: bool = Field(default=False, description="是否允许双向持仓（同货币对反向加仓）；P0 默认关")
    loss_cut_ratio: float = Field(default=50.0, ge=10.0, le=100.0, description="强制平仓线（保证金维持率 %）")
    margin_call_ratio: float = Field(default=100.0, ge=50.0, le=300.0, description="保证金维持率告警线/禁开新仓线（%）")
    liquidation_mode: str = Field(default="all", description="强平模式：all=一次性全部平仓（日式主流）/ sequential=逐平")
    swap_enabled: bool = Field(default=True, description="是否计收/计付隔夜利息")


class MarketConfig(PluginConfigBase):
    __ui_label__ = "行情"
    __ui_icon__ = "activity"
    __ui_order__ = 20

    source: str = Field(default="sina", description="行情源；P0 仅新浪 fx_*")
    pairs: list[str] = Field(
        default_factory=list, description="品种白名单；留空=内置 8 个货币对。可填 USDJPY/EURJPY 等代码"
    )
    quote_poll_seconds: int = Field(default=15, ge=5, le=300, description="行情轮询间隔（秒）")
    quote_timeout_seconds: int = Field(default=8, ge=1, le=30, description="单次行情请求超时（秒）")
    stale_quote_seconds: int = Field(default=60, ge=10, le=600, description="行情时间戳超过该秒数视为过期")
    quote_jitter_ratio: float = Field(default=0.15, ge=0.0, le=0.5, description="轮询抖动（0.15=±15%，防 WAF）")
    secondary_enabled: bool = Field(default=False, description="备源自动接管；P0 关闭")
    weekend_valuation: bool = Field(default=True, description="周末用最后有效价估值（禁止成交）")
    usdcny_enabled: bool = Field(
        default=True,
        description="行情里顺带拉取 USDCNY（在岸人民币换算锚，不是可交易品种；关闭后无法把金额换算成元）",
    )
    cny_reference_enabled: bool = Field(
        default=True,
        description=(
            "行情卡显示「人民币参考汇率」栏（USDCNY/EURCNY/JPYCNY 等 8 个参考对，只展示、不可交易；"
            "关闭后只显示可交易品种）"
        ),
    )


class SpreadConfig(PluginConfigBase):
    __ui_label__ = "点差"
    __ui_icon__ = "percent"
    __ui_order__ = 25

    max_multiplier: float = Field(default=5.0, ge=1.0, le=10.0, description="点差/滑点总放大倍率上限")
    rollover_multiplier: float = Field(default=3.0, ge=1.0, le=10.0, description="换日（NY 16:45-17:15）倍率")
    monday_open_multiplier: float = Field(default=2.5, ge=1.0, le=10.0, description="周初开盘 1 小时倍率")
    news_core_multiplier: float = Field(default=3.0, ge=1.0, le=10.0, description="指标核心（±2 分钟）倍率")
    news_edge_multiplier: float = Field(default=2.0, ge=1.0, le=10.0, description="指标前後（±15 分钟）倍率")
    market_max_slip_pips: float = Field(default=3.0, ge=0.1, le=50.0, description="市价滑点超过该值拒单（pip）")


class OrdersConfig(PluginConfigBase):
    __ui_label__ = "订单"
    __ui_icon__ = "list"
    __ui_order__ = 30

    max_pending: int = Field(default=20, ge=1, le=50, description="有效挂单上限")
    default_tif: str = Field(default="GTC", description="默认有效期：GTC / DAY / GTD")
    allow_limit: bool = Field(default=True, description="允许限价（限价）")
    allow_stop: bool = Field(default=True, description="允许止损（止损）")
    allow_trailing: bool = Field(default=False, description="允许追踪止损（P1，P0 默认关）")
    allow_ifd: bool = Field(default=False, description="允许 IFD/IFO 子单（P1，P0 默认关）")
    quote_stale_fill_seconds: int = Field(default=5, ge=1, le=60, description="行情超过该秒数禁止撮合（挂起）")


class RiskConfig(PluginConfigBase):
    __ui_label__ = "风控"
    __ui_icon__ = "shield"
    __ui_order__ = 40

    risk_per_trade_ratio: float = Field(default=0.02, ge=0.001, le=0.1, description="单笔最大止损风险（占账户净值）")
    margin_per_order_ratio: float = Field(default=0.15, ge=0.01, le=1.0, description="单笔保证金占用上限（占账户净值）")
    max_positions: int = Field(default=6, ge=1, le=20, description="最大同时持仓数（Ticket 计）")
    max_units_per_pair: int = Field(default=200_000, ge=1_000, le=2_000_000, description="单品种最大持仓（货币）")
    daily_trade_limit: int = Field(default=6, ge=0, le=50, description="每日开仓笔数上限")
    daily_loss_limit_ratio: float = Field(default=0.05, ge=0.01, le=1.0, description="当日亏损超过該比例停手")
    max_drawdown_ratio: float = Field(default=0.20, ge=0.05, le=1.0, description="历史最高账户净值回撤超过该比例暂停自动交易")
    forbid_add_to_loser: bool = Field(default=True, description="禁止对浮亏品种同向加仓（逆势加仓）")
    loss_cut_cooldown_minutes: int = Field(default=10, ge=0, le=120, description="强制平仓后冷却分钟")
    post_loss_cut_resume_ratio: float = Field(default=100.0, ge=50.0, le=300.0, description="强平后恢复开仓所需保证金维持率 %")


class DecisionConfig(PluginConfigBase):
    __ui_label__ = "麦麦决策"
    __ui_icon__ = "brain"
    __ui_order__ = 50

    auto_trade: bool = Field(default=False, description="自动交易总开关（默认关，测试后再开）")
    llm_enabled: bool = Field(default=True, description="是否允许调用 LLM 决策")
    interval_minutes: int = Field(default=30, ge=5, le=240, description="决策周期（分钟）")
    decision_windows: list[str] = Field(
        default_factory=lambda: ["08:00-24:00"], description="主动开仓时间窗（北京时间，左闭右开）"
    )
    max_llm_calls_per_day: int = Field(default=30, ge=1, le=200, description="每日 LLM 调用预算（含重试与播报）")
    prompt_char_limit: int = Field(default=8000, ge=1000, le=20000, description="决策提示词字符上限")
    decision_max_tokens: int = Field(default=1200, ge=256, le=4000, description="决策输出 token 上限")
    report_max_tokens: int = Field(default=300, ge=64, le=1000, description="播报输出 token 上限")
    temperature: float = Field(default=0.7, ge=0.0, le=1.5, description="决策温度")
    decision_task_name: str = Field(default="planner", description="决策使用的宿主模型任务名")
    report_task_name: str = Field(default="replyer", description="播报使用的宿主模型任务名")
    request_timeout_ms: int = Field(default=120000, ge=5000, le=300000, description="LLM 请求超时（毫秒）")
    max_drift_pips: float = Field(default=3.0, ge=0.1, le=50.0, description="决策快照价与执行价偏离超过该值拒单（pip）")


class SwapConfig(PluginConfigBase):
    __ui_label__ = "隔夜利息"
    __ui_icon__ = "moon"
    __ui_order__ = 60

    rollover_time: str = Field(default="17:00", description="日切时刻（纽约当地时间 HH:MM）")
    timezone: str = Field(default="America/New_York", description="日切时区（IANA）")
    triple_weekday: int = Field(default=2, ge=0, le=6, description="三倍隔夜利息星期（Python weekday，2=周三）")
    triple_enabled: bool = Field(default=True, description="是否启用周三 3 日分")


class NotifyConfig(PluginConfigBase):
    __ui_label__ = "播报"
    __ui_icon__ = "message-circle"
    __ui_order__ = 70

    platform: str = Field(default="qq", description="平台")
    group_ids: list[str] = Field(default_factory=list, description="允许响应/播报的群号白名单")
    private_enabled: bool = Field(default=False, description="是否允许私聊")
    event_reports: bool = Field(default=True, description="是否播报开平仓/强平/利息事件")
    merge_messages: bool = Field(default=True, description="合并转发（防刷屏）")
    merge_window_ms: int = Field(default=800, ge=0, le=5000, description="自动事件合并窗口（毫秒）")


class AdminConfig(PluginConfigBase):
    __ui_label__ = "管理员"
    __ui_icon__ = "user-check"
    __ui_order__ = 80

    users: list[str] = Field(default_factory=list, description="管理员白名单，格式 平台:用户ID，如 qq:123456")


class CalendarConfig(PluginConfigBase):
    __ui_label__ = "经济指标"
    __ui_icon__ = "calendar"
    __ui_order__ = 85

    enabled: bool = Field(default=False, description="经济指标事件窗：高影响事件前后禁止开新仓（默认关闭）")
    source: str = Field(
        default="auto",
        description="事件来源：auto=真实数据源（失败自动回退静态表）/ static=只用下面的静态事件表，不联网",
    )
    refresh_minutes: int = Field(default=30, ge=5, le=1440, description="真实数据源刷新间隔（分钟，仅内存缓存，不落盘）")
    blackout_minutes: int = Field(default=30, ge=0, le=120, description="事件窗前后各多少分钟禁止开新仓（默认 30 分钟）")
    block_high_impact_only: bool = Field(default=True, description="只禁止高影响事件；关闭=所有登记事件都禁止开新仓")
    announce_minutes: int = Field(default=60, ge=0, le=240, description="事件前多少分钟播报一次「即将公布」（0=不播报）")
    events: list[str] = Field(
        default_factory=list,
        description="静态事件表：每行 时间|币种|名称|影响（时间按北京时间），如 2026-11-07T20:30:00|USD|非农|high；留空=没有事件（不内置示例表）",
    )


class InfluenceConfig(PluginConfigBase):
    """群聊上下文：决策前把最近群聊作为**参考材料**放进提示词（不评分、不设权重、不落盘）。"""

    __ui_label__ = "群聊上下文"
    __ui_icon__ = "users"
    __ui_order__ = 46

    enabled: bool = Field(
        default=False,
        description="决策前把最近群聊作为参考材料放进上下文（默认关闭；不评分、不设权重、不落盘）",
    )
    group_ids: list[str] = Field(
        default_factory=list, description="读取哪个群的聊天记录；留空=使用播报群名单"
    )
    limit: int = Field(default=20, ge=1, le=200, description="最多带多少条最近消息")
    hours: float = Field(default=1.0, ge=0.1, le=48.0, description="回看最近多少小时的群聊")
    timeout_seconds: int = Field(
        default=10, ge=1, le=60, description="读取群聊超时（秒）；超时本轮跳过，不阻塞交易"
    )


class StorageConfig(PluginConfigBase):
    __ui_label__ = "存储"
    __ui_icon__ = "database"
    __ui_order__ = 90

    history_limit: int = Field(default=200, ge=10, le=2000, description="内存保留的最近成交条数")


class MaiFxConfig(PluginConfigBase):
    """麦麦炒外汇完整配置（宿主按此生成 config.toml）。"""

    __ui_label__ = "麦麦炒外汇"

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    spread: SpreadConfig = Field(default_factory=SpreadConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    influence: InfluenceConfig = Field(default_factory=InfluenceConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    swap: SwapConfig = Field(default_factory=SwapConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
