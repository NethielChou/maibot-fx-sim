# P0 模块接口契约（Lead 维护，实现者必读）

> 设计依据：`tmp\forex-design\order-engine-spec.md`、`margin-swap-spec.md`（Rev 1.1）、`inbox\麦麦炒外汇-真实交易环境与工具设计-v0.2.md`。
> 所有金额用 `Decimal` 计算；账本金额用 **int 元（虚拟人民币）**。共享类型见 `models.py`（只读，勿改）。
> v0.3.0 起：账户币种是虚拟人民币（元），JPY/USD 金额分别按 `USDJPY`、`USDCNY` 换算成元；
> 缺汇率时抛 `RATE_UNAVAILABLE`（拒绝开仓 / 挂起估值），绝不用假汇率。

## 文件所有权（禁止越界写别人的文件）

| 归属 | 文件 |
|---|---|
| Lead | `plugin.py` `scheduler.py` `models.py` `config.py` `_manifest.json` `scripts/` `tests/test_smoke.py` `tests/test_integration.py` 文档 |
| 队友 A `fx-market` | `pairs.py` `market_clock.py` `quotes.py` `execution_cost.py` `tests/test_pairs.py` `tests/test_market_clock.py` `tests/test_quotes.py` `tests/test_execution_cost.py` |
| 队友 B `fx-engine` | `orders.py` `accounting.py` `risk.py` `tests/test_orders.py` `tests/test_accounting.py` `tests/test_risk.py` |
| 队友 C `fx-io` | `ledger.py` `decision.py` `report.py` `commands.py` `config_ui.py` `tests/test_ledger.py` `tests/test_decision.py` `tests/test_report.py` `tests/test_commands.py` `tests/test_config_ui.py` |

Bot Python（跑单测）：`F:\3 Bot\MaiBot\MaiBot OneKey\Ghost\Mai-Ghost-Master\MaiBot\.onekey\environments\94a9b46c-452e-4ee9-9a96-ae928f1b2c55\python.exe`
命令（项目根目录）：`<botpython> -m unittest discover -s tests -t . -v`

## 通用约定

- 不使用 `None` 当哨兵（配置除外）；价格/金额比较用 `Decimal`。
- 所有函数尽量纯函数；只有 `ledger.py` / `quotes.py` 触达 I/O。
- 时间一律 `datetime`（带 tzinfo，UTC 或交易所时区）；日切用 `market_clock` 判断。
- 异常：自定义 `ValueError`/`RuntimeError`，消息带原因码（如 `INVALID_UNITS`）。
- 单测不联网：quotes 用 fixture 文本；ledger 用 `tempfile`。

## `pairs.py`（A）

```python
PAIRS: dict[str, PairSpec]
DEFAULT_PAIR_CODES: list[str]            # 8 个
def get_pair(code: str) -> PairSpec       # 未知 → KeyError
def resolve_pair(text: str) -> PairSpec | None   # 接受代码/中文名（如 "美元日元"/"USDJPY"）
def pair_codes_from_config(configured: list[str]) -> list[str]  # 空=默认；过滤未知
def usdjpy_mid(quotes: dict[str, QuoteSnapshot]) -> Decimal | None
def usdcny_mid(quotes: dict[str, QuoteSnapshot]) -> Decimal | None   # 人民币换算锚（不是可交易品种）
# v0.3.4：人民币参考汇率（只展示、不可交易；不进 PAIRS / 配置白名单 / 下单校验）
REFERENCE_PIP: Decimal                                   # 0.001，参考栏点差显示单位
REFERENCE_PAIRS: dict[str, ReferencePair]                # 8 个（USDCNY/EURCNY/JPYCNY/…）
REFERENCE_PAIR_CODES: list[str]
def reference_pairs() -> list[ReferencePair]
def get_reference(code: str) -> ReferencePair | None      # 代码/中文名/新浪代码
```

ReferencePair：`code / name_cn / sina_code / digits=4 / scale（JPYCNY=100）/ pip=REFERENCE_PIP`；
**没有** tick、点差上限、杠杆、隔夜利息、数量上限（只有渲染需要的字段）。

PairSpec 数据（Rev 1.1）：USDJPY/EURJPY/GBPJPY/AUDJPY（digits 3, tick 0.001, pip 0.01, raw+computed 见下）、TRYJPY/MXNJPY/ZARJPY（digits 4, tick 0.0001, pip 0.01）、EURUSD（digits 5, tick 0.00001, pip 0.0001）。
`source_kind`: USDJPY/EURUSD = `raw`；其余 = `computed`。
基准点差（pip）：USDJPY 0.3 / EURJPY 0.7 / GBPJPY 1.0 / AUDJPY 0.7 / TRYJPY 0.8 / MXNJPY 0.6 / ZARJPY 0.6 / EURUSD 0.5。
点差上限：USDJPY 2.0 / EURJPY 2.0 / GBPJPY 3.0 / AUDJPY 2.0 / TRYJPY 4.0 / MXNJPY 3.0 / ZARJPY 3.0 / EURUSD 2.0。
swap（日元/1万/日，long, short；源市场数据，结算时再按 `jpy_per_cny` 换算成元）：USDJPY (+160,-210) EURJPY (+80,-130) GBPJPY (+185,-250) AUDJPY (+85,-135) TRYJPY (+33,-60) MXNJPY (+18,-38) ZARJPY (+16,-35) EURUSD (-101,+55)。
max_units：主流 100_000；TRY/MXN/ZAR 200_000。sina_code：`fx_susdjpy` 等（TRYJPY=`fx_stryjpy`）。

## `fxconv.py`（B，v0.3.0）

```python
class FxRates:                                  # 两条真实汇率锚，都必须 > 0
    usdjpy_mid: Decimal
    usdcny_mid: Decimal
    @property cny_per_usd -> Decimal            # = usdcny_mid
    @property jpy_per_cny -> Decimal            # = usdjpy_mid / usdcny_mid
    def jpy_to_cny(amount) -> Decimal
    def usd_to_cny(amount) -> Decimal
    def account_rate(spec: PairSpec, price) -> Decimal   # 1 单位基础货币的元值
    def pip_value(spec: PairSpec, units: int = 1) -> Decimal
def rates_from_quotes(quotes) -> FxRates | None
class RateUnavailable(ValueError)               # 原因码 RATE_UNAVAILABLE
def require_rates(rates) -> FxRates
```
- JPY 报价：`元 = 日元 ÷ jpy_per_cny`；USD 报价（EURUSD）：`元 = 美元 × cny_per_usd`。
- `rates_from_quotes` 只认本 tick 的 `USDJPY` + `USDCNY`；任一缺失返回 `None`（不沿用旧汇率）。

## `market_clock.py`（A）

```python
def now_utc() -> datetime
def is_market_open(ts: datetime) -> bool          # 周日17:00 NY ~ 周五17:00 NY
def market_status(ts: datetime) -> str            # "open"/"closed"
def next_rollover(ts: datetime) -> datetime       # 下一个 NY 17:00
def is_rollover_window(ts: datetime, minutes: int = 15) -> bool
def is_monday_open_window(ts: datetime, hours: int = 1) -> bool
def ny_date(ts: datetime) -> str                  # 日切归属 YYYY-MM-DD
def current_rollover_date(ts: datetime) -> str
```
时区一律 `zoneinfo("America/New_York")`；北京显示由调用方转 `Asia/Shanghai`。

## `quotes.py`（A）

```python
class QuoteSource:
    def __init__(
        self,
        timeout_seconds: int = 8,
        include_usdcny: bool = True,
        include_cny_reference: bool = True,
    ) -> None
    def requested_codes(self, codes: list[str]) -> list[str]
    def build_url(self, codes: list[str]) -> str
    async def fetch(self, codes: list[str], now: datetime, seq: int) -> dict[str, QuoteSnapshot]
def parse_sina_fx(text: str, recv_ts: datetime, seq: int, codes: list[str]) -> dict[str, QuoteSnapshot]
def is_fresh(quote: QuoteSnapshot, now: datetime, stale_seconds: int) -> bool
```
- 新浪请求：`https://hq.sinajs.cn/list=fx_susdjpy,...`，头 `Referer: https://finance.sina.com.cn`，GB18030 解码；用 `urllib.request` 放 `asyncio.to_thread`；超时 8s；失败抛 `RuntimeError`。
- `include_usdcny=True`（默认，对应 `[market] usdcny_enabled`）时同一次请求顺带拉 `fx_susdcny`，
  解析结果多一个 `USDCNY` 快照（人民币换算锚，**不是**可交易品种）。
- `include_cny_reference=True`（默认，对应 `[market] cny_reference_enabled`，v0.3.4）时同一次请求
  再顺带 8 个人民币参考对（`fx_susdcny / fx_seurcny / fx_sjpycny / fx_sgbpcny / fx_saudcny /
  fx_shkdcny / fx_scadcny / fx_snzdcny`）；`requested_codes()` 负责「品种 + 换算锚 + 参考对」
  去重保序，`build_url()` 按同顺序拼代码。参考对**只展示、不可交易**（见 `pairs.REFERENCE_PAIRS`）。
- 字段：idx0 时间 HH:MM:SS、1 bid、2 ask、3 昨收、5 开、6 高、7 低、8 最新、17 日期；`quote_ts` 由 日期+时间 组合（无时区→按 `Asia/Shanghai` 解释再转 UTC；数据日期非当日则由调用方判异常）。
- 计算品种：bid/ask 可能相等，仍原样保存（点差由 execution_cost 合成）。

## `calendar_source.py`（A，v0.3.0）

```python
def parse_ff_payload(text: str) -> list[CalendarEvent]      # ForexFactory 周历 JSON（纯函数）
def parse_sina_payload(text: str) -> list[CalendarEvent]    # 新浪 JSONP（纯函数）
def translate_name(title) -> str                            # 常见指标 → 中文，未收录原样
async def fetch_events(timeout=8, *, now=None, fetcher=None) -> tuple[list[CalendarEvent], str]
class CalendarFeed:                                         # 内存缓存（默认 30 分钟，不落盘）
    async def get_events(self, *, force=False) -> tuple[list[CalendarEvent], str]
    def clear(self) -> None
def merge_events(*groups) -> list[CalendarEvent]            # 按 (时间,货币,名称) 去重排序
```
- 多源顺序：ForexFactory（`nfs.faireconomy.media/ff_calendar_thisweek.json`）→ 新浪 JSONP；
  全部失败返回 `([], "CALENDAR_ALL_SOURCES_FAILED: ff:…,sina:…")`；失败后 60 秒内不重试。
- 事件统一换算成北京时间；影响级别 high/medium/low；新浪 `***`/`**`/`*` 映射为高/中/低。
- 缓存只在内存；`[calendar] source=auto|static`、`refresh_minutes` 由 scheduler 读取。

## `execution_cost.py`（A）

```python
def quote_usable(spec: PairSpec, quote: QuoteSnapshot | None, now: datetime,
                 stale_quote_seconds: int, stale_fill_seconds: int) -> tuple[bool, str]  # (ok, reason)
def effective_spread_pips(spec: PairSpec, quote: QuoteSnapshot, now: datetime,
                          cfg, market_clock) -> Decimal
def apply_spread(quote: QuoteSnapshot, spec: PairSpec, spread_pips: Decimal) -> tuple[Decimal, Decimal]
def slippage_pips(order_type: str, spec: PairSpec, cfg, rng: random.Random | None = None) -> Decimal
```
- 直源：`基准点差 = max(源点差, spec.base_spread_pips)`；计算品种：`spec.base_spread_pips`；再乘时间窗（rollover/monday/news，总倍率 ≤ cfg.spread.max_multiplier）。
- EURUSD：`quote_usable` 必须同时检查 `USDJPY` 的可用性（调用方传 dict；函数签名可改为 `quote_usable(spec, quotes: dict, ...)`，最终以实际实现为准并写进 docstring）。
- `apply_spread`：以 mid 为中心按 tick 对齐生成 bid/ask（计算品种）；直源返回源 bid/ask（若源点差 < 基准，按基准加宽）。
- 滑点：market 0.2 pip、stop 0.5、rocut 1.5（强制平仓由调用方标记），加确定性 jitter（`random.Random(seed)`）。

## `orders.py`（B）

```python
class OrderEngine:
    def __init__(self, pairs: dict[str, PairSpec], cfg) -> None
    def restore(self, orders: list[Order]) -> None
    @property
    def pending(self) -> list[Order]
    def submit(self, req: OrderRequest, quotes: dict[str, QuoteSnapshot], now: datetime,
               account: AccountState, positions: list[Position],
               rates: FxRates | None) -> Order        # 含校验/拒单原因码；market 也先返回 Order 由 process_tick 成交
    def process_tick(self, quotes, now, account, positions, rates) -> FillResult
    def cancel(self, order_id: str, now: datetime) -> tuple[bool, str]
    def modify(self, order_id: str, *, limit_price=None, trigger_price=None,
               expires_at=None, now: datetime) -> tuple[bool, str]
```
- 优先级 Rev 1.1：S1 已触发止损/追踪止损 → S2 已触发限价 → 重算保证金维持率（仍 <50% 才强平）→ S4 新开止损 → S5 新开限价；同阶段 order_id 升序。
- 成交价规则见 order-engine-spec §4.2；买用 ask、卖用 bid；指値不劣化；逆指値转市价+逆向滑点；跳空记 `gap_pips`。
- OCO：P0 支持"持仓止损/止盈 OCO 一对"，一条成交另一条 `cancelled`。
- 校验：单位 1,000 整数倍、价格 tick 对齐、保证金（回调 `accounting.required_margin`）、单笔风险（`risk.size_by_risk`）、行情可用（`execution_cost.quote_usable`）、点差上限、事件窗静默（P0 关闭）；缺汇率直接 `RATE_UNAVAILABLE`。
- 不写 DB：引擎只返回 `FillResult`；由 scheduler 调 ledger 落库。

## `accounting.py`（B）

```python
def notional(spec, units: int, price: Decimal, rates: FxRates | None) -> Decimal
def required_margin(spec, units: int, price: Decimal, rates: FxRates | None, leverage: int) -> int  # ceil 到元
def required_margin_total(positions, quotes, pairs, rates: FxRates | None, leverage) -> int  # 同品种 max(多,空)
def unrealized_pnl(pos: Position, quote: QuoteSnapshot, spec, rates: FxRates | None) -> int
def equity(account, positions, quotes, pairs, rates: FxRates | None) -> int
def maintenance_ratio(equity: int, margin_total: int) -> Decimal | None   # %，无持仓 None
def loss_cut_triggered(equity: int, margin_total: int, ratio: Decimal) -> bool
def swap_amount(pos: Position, spec: PairSpec, days: int, rates: FxRates | None) -> int
def close_pnl(pos: Position, exit_price: Decimal, spec, rates: FxRates | None) -> int
def commission(units: int, commission_per_10k) -> int
def round_money(value) -> int; def ceil_money(value) -> int
```
- 金额一律 **整数元**；`ROUND_HALF_UP`（保证金/手续费 `ROUND_CEILING`）。
- 估值口径：多头用 bid、空头用 ask；`equity = balance + Σ浮动盈亏`。
- 强平判定：`equity × 100 < margin_total × ratio`（整数交叉乘法，避免浮点）。
- 双向持仓取大边：`ceil(max(Σlong units, Σshort units) × 当前对元汇率 / 杠杆)`。
- 缺 USDCNY/USDJPY 时上述换算函数抛 `RateUnavailable`（`RATE_UNAVAILABLE`）。

## `risk.py`（B）

```python
def size_by_risk(account, spec, entry_price, stop_price, rates: FxRates | None, cfg) -> int  # 返回 1,000 整数倍，含 0
def check_open(req: OrderRequest, spec, account, positions, quotes, rates: FxRates | None, cfg,
               now, daily_loss_hit: bool, drawdown_hit: bool) -> tuple[bool, str]
def daily_loss_hit(account, equity: int, cfg) -> bool
def drawdown_hit(account, equity: int, cfg) -> bool
def allow_add_to_loser(position: Position, req: OrderRequest, spec, quote, rates: FxRates | None, cfg) -> tuple[bool, str]
```
- 1 pip 元值：JPY 货币对 = `pip ÷ jpy_per_cny`；USD 货币对（EURUSD）= `pip × cny_per_usd`。
- 缺汇率时 `check_open` 直接返回 `RATE_UNAVAILABLE`（不估算、不放行）。

## `ledger.py`（C）

```python
class Ledger:
    def __init__(self, db_path: str | Path)     # 检测到 v1 旧库会先备份 *.v1.bak-<时间戳> 再重建
    def init(self, initial_balance: int = 100_000) -> None; def close(self) -> None
    def load_account(self) -> AccountState; def save_account(self, acc: AccountState) -> None
    def load_positions(self) -> list[Position]; def upsert_position(self, pos: Position) -> None
    def delete_position(self, position_id: str) -> None
    def insert_trade(self, fill: Fill) -> str          # 返回 trade_id（=order_id）
    def list_trades(self, limit: int = 50) -> list[dict]
    def save_order(self, order: Order) -> None; def load_orders_pending(self) -> list[Order]
    def append_order_event(self, order_id: str, event: str, detail: str = "") -> None
    def insert_swap(self, rollover_date: str, pair: str, side: str, units: int, amount: int, days: int) -> None
    def save_equity(self, ts: datetime, equity: int) -> None
    def save_decision(self, decision_id: str, source: str, payload: str, outcome: str) -> None
    def load_meta(self, key: str) -> str | None; def save_meta(self, key: str, value: str) -> None
    def check_accounting(self, account, positions, pairs, quotes, rates) -> list[str]
    def reset(self, initial_balance: int) -> None
```
表：account / positions / trades / orders / order_events / swaps / equity / decisions / meta（`orders` 用 `(order_id PK, client_order_id UNIQUE, payload_json)` 简化亦可，但要能 load_pending）。
`SCHEMA_VERSION = 2`：金额列用币种中性名（`balance`/`initial_balance`/`margin`/`fee`/`amount`/`equity` 等）。
`check_accounting` 返回不一致项字符串列表（空=一致）。

## `decision.py`（C）

```python
def build_prompt(account, positions, quotes, pairs, recent_trades, cfg) -> str
def parse_intent(text: str, pairs, rates=None) -> DecisionIntent   # 剥离 ```json```、宽容数字字符串；非法抛 ValueError
class CallBudget:  # day: str, max_calls: int; can_call() -> bool; consume() -> None; remaining -> int
async def decide(ctx, account, positions, quotes, pairs, recent_trades, cfg, budget: CallBudget) -> DecisionIntent
```
- 调用 `ctx.llm.generate(task=cfg.decision.decision_task_name, messages=[{"role":"user","content":prompt}], ...)`；超时/失败返回 `DecisionIntent(action="hold", reason="LLM_FAILED")`。
- 执行前漂移校验由 scheduler 负责（≥ cfg.decision.max_drift_pips 拒单）。

## `report.py`（C）

纯函数（返回 str，末尾统一附 `DISCLAIMER`）：

- 卡片式排版（v0.3.2 起）：标题行 `emoji 中文标题 · MM-DD HH:MM`、标题下空行、
  分区横线 `CARD_DIVIDER`（13 个半角横线）、行宽 `CARD_WIDTH`（52 个半角列 ≈ 26 汉字，
  长文本自动折行），末行固定 `DISCLAIMER`；纯文本，不用 markdown 表格/代码块。
- 命令类：`status(account, positions, quotes, pairs, rates, *, pending, leverage, market_open,
  auto_trade, paused)`、`positions_list`、`pending_list`、
  `quotes_overview(quotes, pairs=None, *, cny_reference=True)`（v0.3.4 三段：可交易栏
  「符号 + 中文名 + 买入 / 卖出 + 点差」/ 人民币参考汇率栏（8 个参考对，4 位小数，
  JPYCNY 按 100 日元折算）/ `HOWTO_LINES`「📖 怎么看」）、
  `margin_overview(account, positions, quotes, pairs, rates,
  *, leverage)`、`swap_list(records, pairs)`、`record_list(trades)`、`help_text()`；
- 事件类：`fill_report(fill, spec)`、`order_submitted(order)`、`order_rejected(id, reason)`、
  `order_suspended(id, reason)`、`decision_report(intent)`、`loss_cut_report(...)`、
  `swap_report(total, days)`、`rate_unavailable_text()`、`event_soon_text(event)`；
- 工具：`card_notice(heading, lines, *, footer, when, stamp)`、`display_width(text)`、
  `fmt_money(int)`、`fmt_signed_money(int)`、`append_disclaimer(text)`。

文案中文（无日文假名），带"纯本地虚拟游戏 · 模拟盘，不构成投资建议"。

## `commands.py`（C）

```python
@dataclass
class CommandServices:  # Lead 在 plugin.py 里构造
    cfg; account; positions(); quotes(); pairs; ledger; engine; actions  # actions 回调 dict
def parse_command(text: str) -> tuple[str, list[str]] | None
def is_admin(user_id: str, platform: str, admins: list[str]) -> bool
async def handle(services, *, platform: str, user_id: str, group_id: str | None,
                 text: str) -> str | None   # None=不该响应（非白名单群/非命令）
```
子命令：状态/持仓/挂单/行情/记录/保证金/隔夜/帮助（所有人）；试跑/暂停/恢复/买/卖/平/全平/撤单/重置/补给（管理员）。参数解析要能容错（品种中文/代码、万通貨、限价/止损）。

## `config_ui.py`（C）

`def build_config_ui_schema(schema) -> schema`：中文页签（常用/交易/高级）、隐藏开发者字段；参考 `projects\maibot-plugin-mai-futures-sim\config_ui.py` 的写法（可读，勿改）。

## 交付要求

- 每模块 docstring 写职责与关键公式；类型注解齐全；异常带原因码。
- 每个模块至少 8–15 条 unittest（覆盖正常/边界/异常）；**所有测试必须能离线跑**。
- 完成后运行 `<botpython> -m unittest discover -s tests -t . -v`，把通过数/失败数发给 Lead；只修自己文件，不修他人文件。
- 严禁写 `F:\3 Bot\...` 下任何文件（部署由 Lead 做）。
