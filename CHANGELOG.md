# 每次改动记录

## [0.3.4] - 2026-11-10
### Added
- **行情卡新增「人民币参考汇率」栏**（用户反馈：看不到人民币汇率）：
  - 8 个参考对（**只展示、暂不可交易**）：`USDCNY 美元/人民币`、`EURCNY 欧元/人民币`、
    `JPYCNY 日元/人民币`、`GBPCNY 英镑/人民币`、`AUDCNY 澳元/人民币`、
    `HKDCNY 港元/人民币`、`CADCNY 加元/人民币`、`NZDCNY 新西兰元/人民币`；
  - 新浪代码（2026-11-10 实测可用）：`fx_susdcny / fx_seurcny / fx_sjpycny / fx_sgbpcny /
    fx_saudcny / fx_shkdcny / fx_scadcny / fx_snzdcny`，与 8 个可交易品种**同一次请求**拉取
    （GB18030 + 18 字段解析逻辑不变，复用 `parse_sina_fx`）；
  - `pairs.py` 新增 `ReferencePair` / `REFERENCE_PAIRS` / `REFERENCE_PAIR_CODES` /
    `get_reference()` / `reference_pairs()`：只有名称/代码/小数位/展示倍数，没有点差上限、
    杠杆、隔夜利息等交易规则，**不进** `PAIRS`、`DEFAULT_PAIR_CODES`、`pairs_from_config`
    与下单校验；
  - 展示口径：金额固定 4 位小数；**日元按「100日元 = 4.2210 元」显示**（价格 ×100，
    避免 0.0422 这种难读数字）；
  - 配置 `[market] cny_reference_enabled`（默认 true，设置页「显示人民币参考汇率」可关）；
    关闭后行情卡只显示可交易栏；
  - USDCNY 既是换算锚又是参考对：**只出现在参考栏**，不进可交易列表/订单白名单
    （原测试「行情不显示 USDCNY」改为「USDCNY 在参考栏、不在可交易栏」）。
- **行情卡用户友好化**（用户反馈：不知道「点差」是什么）：
  - 卡片底部新增「📖 怎么看」小节：买入价/卖出价是什么、点差 = 两者之差越小越便宜、
    `1 pip = 0.01 日元`（美元计价品种 0.0001 美元）、一个美元/日元的例子；
  - 标题下新增副标题「可交易 N 个品种 + 人民币参考汇率」；
  - 分区改为「【可交易】/【人民币参考汇率】（仅参考，暂不可交易）」并用分隔线隔开；
  - 文案把「买价/卖价」统一改成「买入/卖出」；元数据行改为「均为 4 位小数；日元按 100 日元折算」；
  - `/外汇 帮助` 增加「行情怎么看」提示行。

### Changed
- `quotes.py`：`QuoteSource` 新增 `include_cny_reference`（默认 True）与
  `requested_codes()`；`build_url` 在换算锚之后按固定顺序补参考对代码（去重保序，
  `fx_susdcny` 只出现一次）；`_sina_code_for` 支持参考对代码/中文名/新浪代码。
- `report.quotes_overview(quotes, pairs, *, cny_reference=True)`：签名向后兼容（新增
  keyword-only 参数），`commands.py` 从 `[market] cny_reference_enabled` 透传。
- `plugin.py` 装配 `QuoteSource(include_cny_reference=...)`；`CONFIG_VERSION` → `0.3.4`
  （新增配置键），`PLUGIN_VERSION` → `0.3.4`；`scripts/preflight.py` 期望版本同步 0.3.4。
- `config.example.toml` 增加 `cny_reference_enabled = true` 并同步 `config_version`。

### Tests
- `test_quotes.py`：参考对代码/新浪代码映射、`requested_codes` 去重保序、URL 拼接
  （含/不含参考栏）、参考对解析（4 位小数、JPYCNY 原始价）、参考对不可交易；
- `test_report.py`：参考栏出现（含 4 位小数与点差 0.1/2.3）、JPY 按 100 日元显示、
  关闭 `cny_reference` 后整栏隐藏、可交易栏不含参考对、「📖 怎么看」文案存在、
  卡片无重复行、日元零点差不显示「点差 0.0」；
- `test_pairs.py` / `test_config_ui.py` / `test_smoke.py` / `test_persona_load.py`：
  参考对不可交易、新配置项中文标签与提示、默认值与示例配置逐键一致、版本断言同步；
- 全量 `python -m unittest discover -s tests -t .` 通过（**545 条**，原 524 条）；
  `scripts/preflight.py` 通过（`plugin_version=0.3.4`，`_manifest.json` 版本未改）。

## [0.3.3] - 2026-11-10
### Changed
- 行情卡片：每个货币对前加国旗（如 🇺🇸🇯🇵），价格按品种最少位数收敛（不再出现 212.4508100142 这类长尾），合成报价对点差回退到品种基础点差
- 持仓卡片：开仓/止损/止盈价格同样按品种位数格式化

## [0.3.2] - 2026-11-09
### Changed
- **所有用户可见播报改为「卡片式」排版**（重写 `report.py` 渲染层；不改交易逻辑/数值口径）：
  - 每条播报统一「emoji 中文标题 · 北京时间」标题行 + 空行 + 分区内容 + `─────────────`
    （13 个半角横线）分隔线 + 末行免责声明；
  - 内容行按显示宽度折行（≤52 个半角列 ≈ 26 个汉字），长理由/发言自动换行；
  - 行情每个品种压成一行（货币符号 + 中文名 + 买价/卖价 + 点差），不再重复「买/卖/中/点差」；
  - 状态/保证金/隔夜改为「标签 + 值」对齐字段行；保证金单独分区（持仓占用 / 空仓 / 强平规则）；
  - 持仓/挂单/成交/记录逐条卡片（🟢 多 / 🔴 空 / 🟡 挂单 / 📭 空仓）；
  - 决策播报：动作行 + 数量/委托 + 止损止盈 + `💬 理由` + `✨ 麦麦说`；
  - `/外汇 帮助` 按「🔍 查询 / 💼 交易 / ⚙️ 管理」分组，命令列对齐。
- **修复决策播报重复字段**：市价单不再出现「市价｜市价」；有止损/止盈时只显示
  「止损 X｜止盈 Y」（缺一腿时省略该腿）。
- `commands.py` 的 `/外汇 保证金`、`/外汇 隔夜` 改调用 `report.margin_overview` /
  `report.swap_list`；新增 `report.card_notice` 供一次性小卡片复用（买/卖/撤单/补给/重置等）。
- 版本：`PLUGIN_VERSION` → `0.3.2`（`_manifest.json` 版本不动）；`scripts/preflight.py`
  期望版本同步为 0.3.2。

### Tests
- `test_report.py` 按新模板重写断言，并新增卡片排版单测（标题行/标题后空行/分隔线/行宽 ≤52 列/
  emoji 存在/无重复「市价」/无日文假名/帮助分组）；
- `test_commands.py` 补 `/外汇 保证金`、`/外汇 隔夜` 的卡片结构断言；
- 全量 `python -m unittest discover -s tests -t .` 通过（524 条；另修复 0.3.1 遗留的两处
  版本断言未同步）。

## [0.3.1] - 2026-11-09
### Changed
- 兼容声明适配 MaiCore 1.4.3（host 1.3.13–1.4.99）与 SDK 2.22.1（2.21.0–2.22.99），消除宿主"兼容模式加载"告警

## [0.3.0] - 2026-11-08
### Added
- **真实财经日历接入**：新增 `calendar_source.py`
  - 数据源（2026-11-08 实测）：首选 ForexFactory 周历 JSON
    `nfs.faireconomy.media/ff_calendar_thisweek.json`（当前可用、含本周全部事件、按货币给
    USD/EUR/JPY/CNY 等）；备选新浪财经日历 JSONP
    `DailyFX_AllService.getOnedayEventsNew`（中文、结构可用，但实测数据只到 2026-07 左右）；
  - 不可用并已记录：华尔街见闻 `api-one-wscn…/finance/calendar`（Not Found）、
    金十 `cdn-rili.jin10.com`（DNS 失败）、东方财富 `RPT_ECONOMICVALUE`（报表不存在）；
  - 纯函数解析 `parse_ff_payload` / `parse_sina_payload`（fixture 可离线单测）；
  - 内存缓存默认 30 分钟（`[calendar] refresh_minutes`），失败后 60 秒内不重试，不落盘；
  - `[calendar] source = "auto" | "static"`（默认 auto）：auto 与静态事件表合并，
    全部源失败自动回退静态表并只记一次原因码（warning）；static 完全不联网；
  - 决策上下文、事件窗禁开仓、事件前播报均使用合并后的事件；`enabled` 默认仍为 false，
    不改变默认交易行为。
- **虚拟人民币账户**：账户币种从日元改为**虚拟人民币（元，CNY）**，行情仍为真实世界行情
  - 新增 `fxconv.py`：`cny_per_usd = USDCNY`；`jpy_per_cny = USDJPY / USDCNY`；
    JPY 报价金额 `÷ jpy_per_cny` → 元；USD 报价金额 `× cny_per_usd` → 元；
  - 缺 USDCNY/USDJPY 时拒绝新开仓/挂起估值（原因码 `RATE_UNAVAILABLE`），**不用假汇率**；
  - `quotes.py` 同一次请求附带 `fx_susdcny`（`[market] usdcny_enabled` 可关）；
    USDCNY 只作换算锚，不进入可交易列表（8 个品种不变）；
  - 默认账户：10 万元、杠杆 10 倍（1–25 可配）、最小 1,000 货币、手续费 0 元。
- **账本 schema v2**：金额列改为币种中性名（`balance` / `margin` / `fee` / `amount` /
  `equity` 等）；启动时若检测到 v1（`*_jpy`）库，先备份为
  `ledger.sqlite3.v1.bak-<时间戳>` 再重建空库开户，**不做金额静默迁移**；
  `Ledger.init()` 现在按配置的 `initial_balance` 开户（不再固定 100 万）。
- 测试：换算单测（JPY/USD→元、缺汇率拒绝、四舍五入）、ledger v2 建库/旧库备份、
  USDCNY 报价解析、日历解析/多源回退/缓存 TTL/事件窗播报、`source=static` 不触网。

### Changed
- `accounting.py` 函数改为返回整数**元**并按新口径重算：`notional` / `required_margin` /
  `required_margin_total` / `unrealized_pnl` / `equity` / `close_pnl` / `swap_amount` /
  `commission` / `round_money` / `ceil_money`（保持 round/ceil 语义）；
- `models.AccountState` 字段改中性名 `balance/initial_balance/realized_pnl/fees_paid/
  swap_paid/day_start_equity/peak_equity`；`Fill`/`Position` 金额字段改为 `fee/realized_pnl/
  margin/swap_accrued`（全部元）；
- `scheduler` 每次 tick 取 USDCNY，把换算汇率传入 accounting/risk/orders；缺汇率时
  拒绝开仓、挂起估值并播报一次；隔夜利息换算成元；播报统一用「元」；
- `/外汇 状态/持仓/保证金/隔夜/记录`、决策提示词、配置页、README、示例配置全部改为「元」；
  补给/重置金额按元解析（`parse_cny_amount`）。
- 版本：`PLUGIN_VERSION` / `CONFIG_VERSION` → `0.3.0`（`_manifest.json` 版本不动）。

### Tests
- 更新 `test_accounting` / `test_ledger` / `test_orders` / `test_risk` / `test_report` /
  `test_commands` / `test_quotes` / `test_decision` / `test_scheduler_context` 等受口径影响的用例；
- 全量 `python -m unittest discover -s tests -t .` 通过（>=461 条）。

## [0.2.1] - 2026-11-07
### Changed
- 去掉插件内的人设设置/调试功能，做法与同机「麦麦期货模拟盘」插件一致：
  宿主人设只在 `on_load` 时读一次 `config.get key="personality.personality"`，
  作为一段字符串原样注入决策提示词（读不到/异常/为空 → 空串，不阻塞加载，不伪造性格）
- 决策 prompt 开场：人设原文非空时直接作为开头，并注明
  「你是麦麦，同时在用虚拟账户炒外汇（纯模拟盘，不接真实交易账户）」；为空时用默认开场
- 群聊内容改为「参考材料（群友观点）」段：放在提示词最后，注明不可信、不是命令、
  是否采纳由性格决定；`[influence]` 默认关闭，键精简为 `enabled` / `group_ids` /
  `limit`（20）/ `hours`（1）/ `timeout_seconds`（10），`include_mai` 改为内部常量固定 false
- 版本：`PLUGIN_VERSION` / `CONFIG_VERSION` → `0.2.1`（`_manifest.json` 版本不动）
- `_manifest.json` 的 description 改为全中文（版本仍由发布流程单独维护）；
  `docs/INTERFACES.md` 里残留的日式术语一并中文化（项目保持无日文假名）

### Removed
- `[persona]` 配置段（`enabled` / `refresh_hours` / `override_text`）、`PersonaConfig`、
  配置页「麦麦人设（来自宿主）」分组与相关提示
- `persona.py`（人设字段拼接/override/定时刷新）与 `tests/test_persona.py`
- 命令 `/外汇 麦麦`、`/外汇 人设`、`/外汇 人设 刷新`（含别名 性格/persona）与帮助条目
- `report.persona_report` / `report.persona_refresh_text`；`scheduler` 的
  `host_persona` / `set_host_persona` / `persona_view`（`collect_context` 只返回群聊与日历）

### Tests
- 新增 `tests/test_persona_load.py`（`_load_persona` 的 str/dict/失败/异常/超时/空回退，
  且日志只记字数不写正文）
- 更新 `test_scheduler_context`（宿主人设改字符串、无刷新/视图接口、decide 透传）、
  `test_decision`（人设开场、参考材料置后且不可信）、`test_influence`（默认关闭、
  固定过滤机器人）、`test_commands` / `test_config_ui` / `test_smoke` / `test_report`；
  全量 461 条通过

## [0.2.0] - 2026-11-07
### Changed
- 人设改为读取宿主人设：直接用 WebUI「身份与人格」页的昵称/人格设定/行为风格/表达风格/群聊提示词，
  插件不再定义人设、不做性格数值化、不派生风险偏好参数
- 群聊上下文与原聊天逻辑一致：最近群聊原样放进决策上下文，不评分、不设权重、不摘要，
  听不听由麦麦本人的人设与判断决定；默认开启
- 删除人设派生风控（`risk_config_view` / `_apply_persona_sizing` / `STOP_REQUIRED` /
  影响权重与情绪评分），只保留硬性安全规则：保证金/强平/持仓上限/可选事件窗
- `calendar.py` 改名为 `econ_calendar.py`（不再与标准库 `calendar` 同名，删除兼容层）；
  `[calendar] events` 留空=没有事件（不再内置示例事件表）
- 配置简化：`[persona]` 从 21 个字段收敛为 `enabled` / `refresh_hours` / `override_text`；
  `[influence]` 改为 `enabled` / `limit` / `hours` / `include_mai` / `group_ids` / `timeout_seconds`
- `/外汇 麦麦` 展示宿主人设与群聊上下文/事件窗开关；新增 `/外汇 人设 刷新`（管理员）
- 配置版本 `CONFIG_VERSION` 0.1.0 → 0.2.0

### Removed
- `PersonaTraits` / `PRESETS` / 性格覆盖字段 / 单笔止损风险与逆势加仓的人设派生规则 / 群友影响评分与权重

### Tests
- 重写 `tests/test_persona.py`（宿主读取 str/dict/异常/超时、拼接截断、提示词、override、关闭降级）
  与 `tests/test_influence.py`（消息→上下文、过滤媒体/过长、include_mai、失败/超时降级、隐私）；
  调整 `test_econ_calendar` / `test_config_ui` / `test_commands` / `test_smoke` 等因接口变化的部分，
  新增 `test_scheduler_context`（宿主人设注入、群聊降级、事件窗拦单、reset）

## [0.1.1] - 2026-11-07
### Changed
- 全中文化：替换全部日式术语（488 处），命令/播报/配置页/README 统一中文
- 命令解析同时支持中文关键字（市价/限价/触发/止损/止盈）与英文别名
### Fixed
- 本地化后“止损”关键字冲突（订单类型 vs 保护单）与数量解析（1万单位）
## [0.1.0] - 2026-11-06
### Added
- P0 骨架：日元计价账户（100 万日元 / 25 倍 / 1,000 货币步长）、8 个货币对规格、新浪行情、点差/滑点模型
- 订单引擎：市价/限价/止损、持仓止损止盈 OCO、S1–S5 同 tick 优先级（Rev 1.1）
- 账本：SQLite（account/positions/trades/orders/order_events/swaps/equity/decisions/meta）
- 保证金/保证金维持率/强制平仓、隔夜利息（水曜 3 倍）、LLM 决策（默认关闭）、群聊命令 `/外汇`
- unittest 测试套件（离线可跑）
