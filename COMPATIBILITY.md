# 兼容矩阵

## 自动维护闭环

不增加schema版本，继续使用schema7。POST /maintenance/{id}新增可选名称、时间、回读天数，原enabled开关仍兼容；SDK新增update_maintenance。重复启用不重置起始日，数据集与关联维护计划禁止双重启用。新调度按范围/交易所/结束日生成键，旧日期键根据原任务实际范围继承，不重写旧任务。全局事件根据context.source保留Tushare来源，权限阻塞与恢复可追踪；界面使用同一编辑表单，暂停状态不因编辑而改变。

## 数据核验（schema7）

jobs新增verify类型，复用现有coverage/job_units/job_events。POST /jobs/{id}/verify与DataClient.verify创建独立只读核验；不改行情、不重写原任务、不改变采集账号绑定。GET /runtime/events不依赖数据库。新鲜度的周/月及目录状态使用实际日历与分块证据，允许not_published/rejected。自动补数基于整个原任务链计算剩余范围和总尝试次数；采集与核验使用coverage-v2规则。详细依据与未验证项见docs/QUALITY_VERIFICATION.md。

## 新鲜度与恢复验证（2026-09-18）

新增POST /freshness/query和DataClient.freshness；GET /health增加scheduled_freshness分页对象。仅评估启用的数据集与维护计划，返回逐对象目标日期、实际日期、状态、原因及动作，不覆盖原任务状态或质量。无schema迁移、无数据源请求，旧历史与导出接口不变；分钟及未核验发布规则不冒充已验证。备份恢复工具仅创建无网络临时容器，校验后清理，不向业务数据库恢复。

## 执行与数据质量（2026-09-18）

schema6保留原行情、目录、资料与任务记录，新增job_units、job_events和maintenance_plans。迁移将旧成功状态completed转换为succeeded；/jobs/query仍接受completed筛选，但响应统一为succeeded。新增retrying、blocked；partial显示部分完成，不再表示统一的待核验。

POST /jobs/{id}/retry改为返回新的关联任务，parent_id指向原任务，原状态不改写。可传unit_indices只重试指定分块；已成功范围和仅缺交易时段规则的分钟范围不重复采集。相同活动重试去重，不同范围明确拒绝；客户端需使用响应中的新id。旧覆盖记录保留，旧任务不会被伪造为已通过新校验。

新增GET /jobs/{id}/units、/jobs/{id}/events，POST /events/query，GET /health及GET/POST /maintenance、POST /maintenance/{id}。SDK对应job_units、job_events、query_events、health、maintenance_plans、create_maintenance、set_maintenance。现有任务表和队列复用，来源/账号/端点不变，不升级QMT模型。

数据质量以分块记录，不把任务完成、校验通过与全市场连续性等同。仓单/排名空日因缺少发布证据保持待核验；分钟时段与夜盘归属未确认时保持待核验。维护范围需要从原始采集任务显式保存，默认五交易日回读；权限问题不做网络或业务自动重试。

## 批量筛选与任务（2026-09-18）

不新增迁移，保持schema5。目录GET筛选兼容单值并接受逗号多值，新增POST /catalog/select保证同一快照全选，超过10000项明确失败。Tushare目录同步新增可选exchanges，不传仍为六所。

新增/history/query、/downloads/batch、/exports/batch、/jobs/query和相应DataClient方法；旧单对象接口形状保留。批量回补按各数据集周期交集，单任务/downloads仍要求严格子集；来源必须一致，各自固定账号与端点。批量请求先全部校验再原子创建任务，不修改既有任务。

资料records/sync/export新增selections：同类型查询、多类型分任务同步和导出。旧单目标仍返回单任务，新selections形式返回jobs数组。每个分块持久化目标，旧检查点仍可恢复；不增加并行源请求、轮换账号或第二套数据表。历史批量结果逐行保留来源、周期、转换版本；图表不混合多个序列。

Web新增搜索多选、跨页全选、快捷日期、结构化详情及任务筛选分页。数据来源、采集账号、单指数映射与诊断仍按原单选语义。分钟无权限仍禁用采集，不影响已有历史导出。回归及真实数据验证见docs/VERIFICATION.md。

## 期货资料与周期扩展（2026-09-18）

schema5扩展现有bars和trading_dates，并在同一pfor_qmt schema增加仓单、会员持仓明细表；没有第二套行情库、账号或任务系统。迁移前后137,687条既有行情的原字段哈希一致，原下载任务及检查点未改写。完整回归239项通过，真实小样本和文件一致性见docs/VERIFICATION.md。

| 接口或能力 | 处理 | 验证状态与差异 |
| --- | --- | --- |
| /futures/options、sync、records、export；DataClient同名资料方法 | 新增 | 独立日历、映射、仓单和成交持仓排名；同步复用download队列，导出复用export队列，账号及端点固定 |
| fut_trade_cal | 扩展 | 保存开市、休市及前交易日；六所各7天实测通过；旧/calendar仍仅返回开市日，闭市日不参与调度 |
| fut_weekly_monthly | 新增 | 1w/1mo，仅Tushare；金额万元转元，time为周五/月末标签，as_of_date为上游计算截至日，不是历史时点快照；8周、2月实测通过 |
| ft_mins | 扩展 | 1/5/15/30/60分钟；模拟频率及8000行截断边界通过，真实账号仍权限拒绝；不拼接连续分钟 |
| fut_wsr | 新增 | 保留产品名称、仓库、年度、等级等维度及原始单位；SHFE/CU的铜与铜(BC)分别保存，DCE/A与SHFE/CU实测21及42条 |
| fut_holding | 新增 | 保留会员和可空数值，不伪造名次；DCE/A2611实测29条，INE按SHFE入口返回空，不能视为该市场已验收 |
| fut_mapping | 扩展 | 单独同步、查询、导出及月份合约跳转；A.DCE实测4条，重复同日冲突明确失败 |
| CSV / Parquet | 扩展 | 资料按自身字段和单位导出；仅周/月行情额外增加as_of_date、source_fields，旧日/分钟字段形状保持；真实全字段比对通过 |
| Tick | 未接入 | 官方无API、独立CSV交付；只展示能力状态，不构造不存在的接口或未经样本确认的导入格式 |

资料当前手动同步，K线数据集沿用可配置19:00调度。QMT仍仅1d/1m/5m，xtdata接口及终端模型不变；所有旧默认source=qmt语义保持。

## Tushare 扩展（已实现）

旧API省略source仍选择qmt，xtdata保持QMT语义。新增tushare来源共用目录、任务、历史库及导出；多账号仅用于认证，不增加行情副本维度。历史API保留source=postgresql表示存储层，行source和新增provider表示提供方。旧QMT数据、配置与终端模型不自动改写。

schema版本4将K线唯一键扩展为合约ID/来源/周期/时间；Tushare期货金额统一为元，成交与持仓量为手，旧QMT原始单位明确标记。分钟交易日未知仍为空。2026-09-18完整回归208项通过；实机六市场目录11,275条、豆一2611日线34条及CSV/Parquet一致性通过，分钟接口返回权限拒绝。结果见docs/VERIFICATION.md。

兼容大商所含下划线的月均价主力/连续代码。`/downloads`和SDK下载新增可选周期子集，省略时仍使用数据集全部周期；不改变数据集、定时调度或原失败任务。下载失败保留并展示已入库行数，未授权分钟不会被跳过后标为成功；原检查点可在授权后续跑。

基准为 cfquant `5baa4daf8dab01fb415afdd45a72cd254cea042f`。这是明确裁剪后的行情产品，不宣称整个 cfquant API 兼容。

| 接口或能力 | 处理 | 验证状态与差异 |
| --- | --- | --- |
| 协议序列化、DataFrame、缺失值 | 保留 | 固定上游协议相同输入对比、迁移测试通过 |
| Windows 管道 RPC | 保留并隔离名称 | 本机真实管道收发、客户端重连、错误返回测试通过；hub改为按请求超时清理，避免60秒默认值提前结束180秒下载 |
| get_full_tick | 保留 | 参数及派发检查；实机三证券盘中快照可读，订阅推送单独验证 |
| get_market_data_ex | 保留行情子集 | 1d/1m/5m，不复权；默认 fill_data=False，与上游默认存在差异 |
| get_local_data | 修改 | 本地读取优先第九参数 subscribe=False；不补行情，不接受复权别名绕过 |
| get_instrument_detail | 裁剪回退 | 保留原生新旧名称；无可用能力时明确失败，不构造猜测资料 |
| get_sector_list / get_stock_list_in_sector | 保留当前数据 | 板块树遍历与当前成分；历史 timetag 被拒绝 |
| get_trading_dates / get_divid_factors | 保留 | 保存原始因子及观察时间；真实终端能力待验证 |
| download_history_data / data2 | 修改回退 | 旧接口逐证券；返回只代表请求结束，不代表入库成功；回调仅在同步 RPC 有效期内接收 |
| subscribe_quote / whole_quote | 改写纯行情实现 | 原生参数、首包、退订失败保留、客户端归属测试通过；实机Tick与指定三证券推送及退订通过短观察窗口；订阅 ID 为不透明字符串 |
| 订阅恢复 | 修改 | 网页重连后重建订阅；直接 SDK 使用者需自行重订阅，不在回调线程同步 RPC |
| 交易、账户、资金持仓 | 移除 | SDK 无接口；hub/桥端/HTTP 请求拒绝测试通过 |
| 多源、高级/LITE、Level2、千档、财务、公式、历史权重 | 移除 | 不迁移对应入口或初始化逻辑 |
| DataClient、/api/v1、数据库与任务 | 新增 | PostgreSQL 隔离测试；HTTP/SDK/文件一致性测试 |
| /source/diagnostics | 新增 | 认证只读检查；桥、快照、本地日线与日历分离，缓存读取不等于下载验收 |
| /catalog/* / DataClient.catalog / sync_catalog | 新增 | 完整目录后台同步、真实名称、分页、类别筛选、断点恢复；原/securities列表接口保留原有1000条上限 |
| jobs.kind=catalog / schema版本2 | 新增 | 独立目录队列；同步按批次事务保存资料与检查点；旧下载/导出任务不改写 |
| 七类目录 / schema版本3 | 扩展 | future/option/stock/index/fund/bond；行业概念独立快照。原etf迁为fund+subtype=etf，旧etf筛选和同步参数保留兼容 |
| get_instrument_details / get_sector_tree | 新增 | 最多100合约批量原生资料、保留板块祖先路径；新版桥catalog_version=2；旧桥仅普通证券资料可逐一回退 |
| get_option_detail_data | 新增行情能力 | 仅派发原生详情；不可用时明确失败，不猜测字段；不涉及任何期权交易接口 |
| /boards/* / /catalog/detail | 新增 | 板块分页、成员快照、数据集固定成员与显式刷新；合约详情可离线查询 |
| 衍生品代码、夜盘、结算与持仓量 | 扩展 | 保留大小写、组合空格和SHO/SZO/IF/SF/DF/ZF/INE/GF后缀；新增字段为可空，CSV/Parquet和历史API增加列；未知交易日保留待核验 |
| 多市场日历调度 | 修改 | 不再固定上证日历，按数据集各市场成员读取；任一市场日历未就绪时不创建不完整任务 |
| /source/diagnostics terminal_history | 新增 | 仅提取终端当日历史日志的允许字段；过去错误不推断当前登录状态 |
| 空数据与调度失败 | 修改 | 历史和日历双空时失败且检查点不推进；只行情为空仍partial；日历异常或不足五日显式提示 |
| WebSocket | 新增 | 独立本机8767，一次性30秒票据，任务与行情推送 |
| WebSocket unwatch / watch确认 | 新增 | 可停止行情但保留任务推送；切换及重连丢弃旧回调；退订失败保留原订阅，非法命令不再断开连接 |
| CSV / Parquet | 新增 | CSV BOM；空值保留；Parquet 数值使用十进制文本 string，避免任意 NUMERIC 精度损失 |
| 独立导出队列 / status.export_worker | 新增 | 导出不等待QMT下载/调度，独立锁和类型恢复；跨类型完成顺序不再保证全局先进先出 |
| QMT 模型部署 | 重写隔离边界 | 合成终端验证隔离、冲突及并发拒绝；进程输出UTF-8且精确匹配中文路径；实机自有桥在运行，完整重装流程待验收 |

目录分类使用QMT明确的资产板块及交易所合约编码规则；不从证券名称猜测类别。SDK、hub和桥均保留相同的国内代码边界与行情动作白名单。终端权限、数据覆盖、字段单位、交易时间戳与新增品种仍需实机小样本验收。

2026-09-16实机历史请求返回空表，终端日志为 `200001 / connection is not login`；日线、交易日历、分钟覆盖和实时推送均未据此标记通过。三证券重复回补及DB/SDK/文件一致性已在隔离测试验证，真实执行步骤见docs/LIVE_ACCEPTANCE.md。

2026-09-17新增实机Tick/指定三证券全推行情与网页持续更新通过；短区间日线下载仍失败，当前历史请求未给出明确错误码，连接或权限待终端侧核实。未修改桥端API以猜测或绕过此故障。

同日七类扩展：新版行情桥已连接，79,208条证券/合约资料与QMT及API分页核对一致；沪铜、金融期权、商品期权样本收到快照及订阅回调。ETF期权样本仅资料可读，报价为空；历史日线及日历仍为空。完整自动化168项通过，详细部署文件差异及实机范围见docs/VERIFICATION.md和docs/LIVE_ACCEPTANCE.md。

分钟任务记录未确认的分钟内覆盖情况，因此可呈现「待核验」；空表永远不标记业务成功。历史库存原始数据，图表使用 JS 数值显示，表格、API 与导出保留精确十进制文本。
