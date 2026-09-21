# API 与数据口径

## 名称与历史汇总

POST /history/summary在groups之外返回instrument_names，按请求source从证券目录映射代码到名称；没有目录记录时不返回猜测名称。POST /futures/summary和分页资料查询对mapping/settle返回同源instrument_names，映射代码、对应月份代码和结算合约代码均可用于名称展示。原始rows和导出字段不变。

## 资料明细筛选

POST /futures/records、/futures/summary、/futures/export支持顶层dimensions对象：warehouse资料允许warehouse、unit；holding资料允许broker。字段值为最多1000项的字符串或null列表，不用逗号分隔，保留原始名称。不同字段取交集，同字段多值取并集；空列表不加限制。例：`"dimensions":{"warehouse":["甲,仓库"],"unit":["吨"]}`。

POST /futures/filter-options接受与资料查询相同的来源、对象和日期范围，返回options、total、snapshot、evaluated_at。选项来自该范围的完整入库记录，不按传入dimensions缩减；超过1000个不同值明确拒绝。列表、统计及导出复用同一筛选，导出任务及口径JSON保留dimensions。

/futures/sync和维护范围不接受非空dimensions，防止把资料查询条件当成上游采集限制。SDK新增futures_filter_options；futures_records有dimensions时使用POST，query_futures、futures_summary、export_futures及export_futures_batch均可传同一对象。

## 分块与事件筛选

GET /jobs/{id}/units新增states、quality_states、search、error_code可选参数；状态支持列表或逗号分隔，search检索分块请求与问题说明。返回total、limit、offset和next_offset，数量与记录在同一只读快照计算，按unit_index稳定排序。省略筛选保留原查询范围；SDK job_units接受同名关键字参数。

POST /events/query新增search，匹配事件说明或代码；job_id接受完整ID或至少8位前缀。GET /jobs/{id}/events始终固定该任务，支持同样的search、levels、code、start/end筛选及before游标，SDK job_events接受这些关键字参数。事件页展示本批数量，不把当前页当作全部结果。

## 回收站

POST /console/datasets/{id}/delete、/restore及/console/maintenance/{id}/delete、/restore要求当前revision。POST /jobs/{id}/delete、/restore接受空对象；只允许已结束任务回收。响应含deleted_at，恢复后为空。配置删除与恢复均停用自动更新；活动引用、维护依赖、过期版本会拒绝操作。已删除记录仍可追溯，但不能执行采集或重试。

/datasets/query、/maintenance/query、/jobs/query和/operations/summary新增trash=active|deleted|all，默认active。执行统计与任务列表共用筛选；数据质量统计保留已删除任务的未解决证据。GET /datasets和/maintenance默认排除回收站项，避免采集时误选。SDK提供delete_dataset/restore_dataset、delete_maintenance/restore_maintenance、delete_job/restore_job；前两组要求revision。

## 任务列表筛选

POST /jobs/query与/operations/summary共用search、states、kinds、sources、start/end、account_ids及origins筛选。search匹配任务ID、错误、成员代码、账号ID和当前关联数据集/维护名称；日期指上海时区的任务创建日期。account_ids和origins接受列表或逗号分隔值；origins包括manual、scheduled、maintenance（执行一次）、retry、repair、verify。

/jobs/query新增只读origin、dataset_name、maintenance_name字段。名称来自当前配置，可能为NULL；成员、日期、账号绑定仍以任务payload为准。列表默认创建时间倒序，支持白名单排序和50/100/200条分页，不回传chunks大字段。

## 维护执行与固定绑定

POST /console/maintenance接受job_id时复制原任务成员、周期、账号和端点；payload仅有dataset_id时才读取数据集当前配置。已有固定范围传回成员及端点时，编辑/复制保持原绑定。新计划默认停用，启用前校验能力。

POST /console/maintenance/{id}/preview接受可选start/end，自定义范围优先，否则按同源已保存交易日历回读；返回五分钟有效preview_key及实际范围，不创建任务。/run确认时再次检查权限和绑定，重复同一preview_key返回原任务。POST /console/datasets/{id}可显式传rebind_account=true更新所选账号端点；普通名称修改或停用不重绑。停用的维护范围可显式选择账号并重新绑定，原已排队任务不变。

## 执行、质量与运维

GET /status新增worker_issues数组，包含source、lane、scope_kind/scope_id、name、market、code、reason和action（非范围故障不含scope字段）。原worker等文本字段仍保留兼容；工作台用结构化字段渲染折叠摘要与逐范围详情，不解析拼接文本。

任务状态：queued、running、retrying、succeeded、partial、failed、blocked、cancelled。partial是部分完成；blocked是权限、能力或连接前置条件需要处理。`result.quality_summary`分别统计verified、pending_verification、not_published、not_applicable、missing、rejected等分块，不用执行状态替代质量结论。

| 方法 | 路径（/api/v1前缀） | 参数与行为 |
| --- | --- | --- |
| GET | /jobs/{id}/units | limit=1..200、offset；每块request、state、quality_state、issues、row_count和行情写入stats |
| GET | /jobs/{id}/events | limit=1..200、before游标；按事件ID倒序，重试等待、异常与提交事件持久保存 |
| POST | /events/query | job_id、levels、sources、code、start、end、limit、before；级别与来源支持多选 |
| POST | /jobs/{id}/retry | 可选unit_indices（从0起）；返回新任务，parent_id保留关联。旧任务不改写，成功范围不重复采集 |
| POST | /jobs/{id}/verify | 非活动采集或核验任务创建只读verify任务；verification_of关联原任务，活动核验去重，不请求数据源 |
| POST | /jobs/{id}/repair-preview | 已结束核验，可选unit_indices；返回固定来源/账号/端点、rows、total、truncated、unit_indices、preview_key、active_job；只读 |
| POST | /jobs/{id}/repair | preview_key及可选unit_indices；确认明确缺口后创建download，固定原账号端点，重复提交去重，范围变化拒绝 |
| GET | /jobs/{id}/links | limit=1..200、offset；查询原任务、直接重试、核验及缺口补数关联，rows/next_offset |
| GET | /runtime/events | 无需数据库连接；读取本项目固定轮转日志末尾，返回脱敏rows及truncated标志 |
| GET | /health | 队列、分块质量、待处理任务、数据库大小、运行目录磁盘空间、来源/周期最近行情与维护状态 |
| POST | /freshness/query | sources多选、search、limit=1..200、offset；返回已启用范围逐对象的新鲜度、目标日、原因与建议动作，健康接口scheduled_freshness为同口径首页 |
| GET/POST | /maintenance | GET列出范围；POST传job_id、name、schedule_time、lookback_days，从原始目录/采集任务保存范围 |
| POST | /maintenance/{id} | 可选enabled布尔值、name、schedule_time(HH:MM)、lookback_days(1..365)；保留固定范围与旧任务，启用检查重复路径 |

事件字段不含Token/DSN。`stats.read`是本次读回记录数，inserted/updated是实际写入数，unchanged_or_older为未变化或旧截至日未覆盖数；仓单与排名仍按正向读回的日期快照更新，不伪报逐条变更统计。旧任务没有新日志及分块记录，原coverage仍可查看，不能据此声称通过新校验。

任务重试只处理失败、被阻塞或明确可补的分块。未知分钟交易时段、夜盘归属、未知资料发布规则不通过重复下载消除。存在不一致OHLC或同时间冲突时整块不入库，独立分块继续；网络与数据库级错误停止本次来源处理。原始任务与关联重试共同提供追溯，未覆盖的缺口仍保留。

维护默认QMT 17:00、Tushare 19:00及最近五交易日回读；目录每天执行，资料按对应交易所日历生成范围。停机后的缺失范围合并入下次任务。自动补数仅针对启用维护或数据集自动更新的任务，最多3轮，最早15分钟、1小时、24小时后；权限、认证、限额与参数错误不进入自动补数。网络单次操作另最多3次重试（共4次请求）。每次调度仍以实际能力为准，不自动使用其他账号或来源。

新鲜度读取只依赖数据库，不请求行情源或自动建任务。SDK为`client.freshness(sources=['tushare'],search='CU',limit=50)`；状态依据维护目标，与原始任务质量分开。缺日历、未知分钟时段及未核验发布规则保留pending_verification，不能用自然日硬判。详见[FRESHNESS.md](FRESHNESS.md)。

## 期货资料与新增周期

schema8增加`resource=settle`（exchange+code具体月份合约）及`weekly_detail`（exchange+symbol品种）。仍使用原资料查询、批量同步、导出、维护与核验接口。结算费率保留原值；周报金额转元并保留原始亿元和周编号，按week_date查询，不当作逐日资料判断覆盖。详情见[结算与周报说明](SETTLEMENT_WEEKLY.md)。

Tushare周期增加`1w`、`1mo`、`15m`、`30m`、`60m`；QMT仍只接受原`1d/1m/5m`。连续合约可下载日/周/月，分钟仅支持具体月份合约。周/月线按查询日期所在周/月的标签读取，`time`为上游周五/月末标签（可能晚于当前日期），`trading_day=null`，`as_of_date`为上游计算截至日；截至日不等于历史查询日期，不能作为历史时点快照。`source_fields`保留上游字段及原始万元金额，标准`amount`仍为元。较旧截至日不能覆盖较新结果。

| 方法 | 路径（/api/v1前缀） | 参数与行为 |
| --- | --- | --- |
| GET | /futures/options | source=tushare、exchange；返回目录中的产品和主力/连续合约选择项 |
| POST | /futures/sync | source=tushare、resource、start/end、account_id；返回统一download任务，固定账号及端点 |
| GET | /futures/records | 相同资料筛选，limit=1..5000、offset；返回rows、fields、units、next_offset，只查询数据库 |
| POST | /futures/export | 相同资料筛选与format=csv/parquet；无需账号，复用后台export任务及文件下载接口 |

`resource`为`calendar/mapping/warehouse/holding`。日历传`exchange=SHFE/DCE/CFFEX/CZCE/INE/GFEX`；映射传`code=CU.SHF`等目录已收录主力/连续代码；仓单传`exchange+symbol`产品代码，持仓传产品或具体月份合约主体（如`A2611`）。资料同步最长单任务十年；日历允许未来日期，其他资料不允许。所有来源显式为tushare，拒绝借QMT来源访问这些接口。

日历记录保存休市日和`pretrade_date`；旧`/calendar`只返回开市日，保持兼容。仓单按上游产品名称、仓库编号/名称、年度、等级、品牌、产地、折算标志及单位区分（SHFE的CU可能同时返回“铜”和“铜(BC)”）；数量不跨单位或产品名称相加。仓单与会员资料仅替换实际完整读回的日快照，空响应不擦除旧数据。持仓保留NULL，不把会员记录合成未提供的名次；INE使用官方SHFE入口，返回交易所保留SHFE。资料量达到上限时按日期继续细分，最小日仍达到上限则失败，不确认截断结果。

```python
client.sync_futures("calendar", "2026-09-01", "2026-09-30", exchange="DCE")
client.sync_futures("warehouse", "2026-09-17", "2026-09-17", exchange="DCE", symbol="A")
client.sync_futures("holding", "2026-09-17", "2026-09-17", exchange="DCE", symbol="A2611")
client.sync_futures("mapping", "2026-09-01", "2026-09-17", code="A.DCE")
rows = client.futures_records("warehouse", "2026-09-17", "2026-09-17", exchange="DCE", symbol="A")
client.export_futures("warehouse", "2026-09-17", "2026-09-17", exchange="DCE", symbol="A", format="parquet")
```

`/sources`公布来源周期、资料类型和Tick无API状态；账号检测新增weekly、monthly、warehouse、holding、tick状态。积分门槛不替代实测权限，分钟被拒绝不会阻止周/月线及其他资料。

## Tushare 与来源选择

旧接口省略`source`仍为`qmt`。目录、历史、日历、导出支持`source=tushare`，返回相同字段结构，代码保留来源原码，例如`CU2610.SHF`；QMT代码`cu2610.SF`不被替换。合约资料返回稳定`instrument_id`，资料足够时同一月份合约可关联相同ID；连续序列按来源独立。

| 方法 | 路径（/api/v1前缀） | 参数及行为 |
| --- | --- | --- |
| GET | /sources | 来源能力、脱敏账号、最近权限检测 |
| GET/POST | /sources/tushare/accounts | 查询/新增/更新账号；id、name、endpoint、token、enabled、timeout、requests_per_minute；default设为默认，clear_token显式清除 |
| POST | /sources/tushare/accounts/{id}/test | 分别检测目录、日历、日线、分钟、映射；空结果不标数据可用 |
| POST | /sources/tushare/accounts/{id}/delete | 拒绝删除被数据集或活动任务引用的账号 |
| POST | /catalog/sync | source、account_id、kinds=["future"]；任务ID和检查点 |
| GET | /catalog/securities | 新增source、active=true筛选；search也匹配品种代码 |
| POST | /datasets | 新增source、account_id、schedule_time；Tushare默认19:00，QMT17:00 |
| POST | /datasets/{id}/account | account_id；仅改变未来任务，已有任务保持原账号及端点 |
| GET | /contract-mappings | code、source、start、end；返回已保存的每日主力/连续映射，最多2000条，长范围按日期分段 |
| GET | /history、/calendar | source明确选择提供方，不自动混合 |
| POST | /exports | source选择已入库来源，不要求账号在线 |

Tushare仅普通月份合约支持1m/5m；主力/连续仅日线与每日映射。SDK `DataClient`的catalog、instrument、history、export增加source参数；sync_catalog增加account_id；新增sources、tushare_accounts、save_tushare_account、test_tushare_account、calendar、contract_mappings。`xtdata`与实时WebSocket watch仍限QMT。

`POST /downloads`可传`periods`非空数组，必须是数据集周期的子集；省略时保持原行为，使用数据集全部周期。来源、账号及端点仍取自数据集，不受顶部所选账号影响。例如`client.download(dataset_id, "2026-09-14", "2026-09-17", periods=["1d"])`可在分钟未授权时单独回补日线；不修改数据集定义和自动更新周期。

下载任务每个分块的`result.rows`与行情、覆盖记录及检查点同事务保存；失败、取消或恢复均保留已入库数量，旧任务缺少统计时从覆盖记录计算。该数量表示已保存分块的累计行数，不是新增去重记录数，也不表示整个任务成功。Tushare目录进度为六市场各两类共12批，`result.rows`为已保存的Tushare目录总数。支持`L_F.DCE`等含下划线的月均价主力/连续代码，仍保留来源独立身份。

历史返回顶层`source=postgresql`表示存储层，`provider`与每行`source`表示提供方。`normalization_version=tushare-futures-v1`使用合约报价单位、手、元；日线万元按十进制精确换算。既有QMT为qmt-raw-v1，保留原始数值和单位标记。CSV/Parquet口径说明携带上述元数据。

日线按交易日查询；分钟未提供交易日时保留trading_day=null，按上海自然时间筛选并标记夜盘归属、分钟内完整性待核验。主力映射缺少某交易日时不会自行沿用前一合约。Tushare日历不可用时不借用QMT或其他交易所日历。

默认地址 `http://127.0.0.1:8766/api/v1`。SDK请求使用 `Authorization: Bearer <API Key>`；网页登录使用HttpOnly/SameSite=Strict会话。所有业务路径均需认证。HTTP错误为 `{ "error": "..." }`，不回传数据库凭据或原始驱动连接错误。

主程序统一读取config.toml，端口可通过TOML、环境变量或CLI覆盖；GET /settings仅返回脱敏配置与config_file路径，POST /settings写回同一TOML。详情见CONFIGURATION.md。

`GET /status` 的worker字段保留行情下载/调度错误，新增export_worker表示独立导出队列错误。空字符串表示没有已报告的后台错误，不代表真实行情验收通过；任务页分别显示两类状态。

| 方法 | 路径 | 参数 / 行为 |
| --- | --- | --- |
| GET | /status, /settings | 脱敏连接状态和配置 |
| POST | /settings | dsn、qmt_root、可选password；空密码不修改；login_enabled布尔值显式启停密码登录 |
| POST | /database/migrate | 初始化/升级独立schema |
| POST | /source/test | 只检查行情桥ping，不代表行情服务器已登录 |
| POST | /source/diagnostics | 可选code，默认000300.SH；只读检查桥、快照、近30天本地日线与交易日历 |
| GET | /quotes | codes逗号分隔，读取QMT快照 |
| GET | /securities | search、kind，可在终端离线时查库 |
| POST | /securities/sync | members数组、kind=future/option/stock/index/fund/bond；兼容etf，最多100证券 |
| GET | /catalog | 已保存目录数量、板块和最近目录任务 |
| GET | /catalog/securities | search、kind、market、subtype、limit=1..200、offset；返回rows、total、next_offset，支持完整目录分页 |
| GET | /catalog/detail | code；读取已保存原生详情、分类及合约元数据，支持QMT离线 |
| POST | /catalog/resolve | members代码数组；批量读取已保存名称，不请求QMT |
| POST | /catalog/sync | 可选kinds数组=future/option/stock/index/fund/bond/board，默认全部，期货期权优先；兼容etf子类。相同范围活动任务去重，不会将新类别请求误报为已排队 |
| GET | /boards | search、category=industry/concept、limit、offset；保存的板块目录分页 |
| GET | /boards/members | name；最新观察快照及成员数组 |
| POST | /boards/refresh | name；从QMT刷新当前成员快照，不改写已有数据集 |
| GET | /sectors | 实时读取QMT板块目录 |
| GET | /indices | 映射和最新成分快照 |
| POST | /indices/refresh | code、sector，可选name；优先使用目录中的指数名称，保存观察时点快照 |
| GET/POST | /datasets | 列出/创建；name、members、periods，可带index_code与snapshot_id，或board_name与board_snapshot_id；成员必须匹配快照 |
| POST | /datasets/{id}/refresh | 显式刷新指数或行业概念数据集成员，不修改既有任务 |
| POST | /datasets/{id}/schedule | enabled布尔值，启用当天开始跟踪17:00到期日 |
| GET | /history | code、period、start、end、limit=1..5000、offset；返回rows与next_offset |
| GET | /factors | code，原始因子和观察时间 |
| GET | /calendar | market=SH/SZ/BJ/SHO/SZO/IF/SF/DF/ZF/INE/GF、start、end；读取已保存交易日期 |
| POST | /downloads | dataset_id、可选start/end，立即返回持久化任务ID |
| GET | /jobs, /jobs/{id} | 最近200任务 / 单任务与检查点、覆盖、错误 |
| POST | /jobs/{id}/cancel, /retry | 取消后续处理 / 失败、取消、partial任务重试 |
| POST | /exports | members、period、start、end、format=csv/parquet；返回任务 |
| GET | /files/{id}, /files/{id}/metadata | 下载已完成导出文件 / 口径JSON |
| POST | /deploy/inspect, /prepare, /activate | 本地QMT目录检查、准备、启用；只管理PFOR_MARKET |
| POST | /login, /logout | credential为API Key或本地密码；创建/撤销网页登录会话 |
| POST | /ws-ticket | 一次性30秒票据及WebSocket端口 |

WebSocket默认 `ws://127.0.0.1:8767/?ticket=...`。不在URL传API Key。发送 `{"action":"watch","codes":["000300.SH"]}` 订阅；接收 `event=quote/job/source/error`，最新行情不会入库。页面重连时重新取得票据和订阅。事件队列有界，缓慢客户端应通过任务GET或行情快照重新同步。

发送 `{"action":"unwatch"}` 停止行情订阅，WebSocket保留用于任务推送。订阅成功或停止后返回 `{"event":"watch","codes":[...]}`，空数组表示已停止。切换证券先退订原订阅；退订失败返回error及仍保留的codes，不声称停止成功。旧订阅的迟到回调及排队行情在切换、停止、桥重连后丢弃，任务事件不受影响。无效命令返回error并保持连接。

诊断返回 `checks` 数组，每项含name、state、message，适用时含rows和time。state为 `ok/empty/error/unverified`；桥失败则后续项未验证。`history_readable=true` 只表示有可读日线和区间内交易日历，不承诺行情在线、数据完整或下载通过。诊断不触发下载，不保存资料或历史数据。

配置QMT目录后，增加terminal_history项：从当日datasource日志末尾最多2MiB提取最近历史请求的证券、周期、时间与received数组，不返回原始日志、服务器地址或账户信息。仅全零结果标为空，其他接收记录仍待回读校验；过去请求不能证明当前连接状态。

目录任务kind为catalog，使用独立串行队列和schema级锁，不受历史任务排队影响；原生调用阻塞时仍需等待桥端响应。新版桥每批最多100个合约资料；可用的金融/ETF期权专用详情最多8个并发补齐，商品期权未返回的字段保持缺失。旧资料接口按16条批次回退。资料或板块快照、分块记录与检查点同事务保存。网络失败最多重试3次；重启沿检查点恢复，手动重试创建仅含未完成范围的关联任务。缺少名称或板块成员时保留旧资料并列入result.missing，不构造名称；不删除旧证券，不触发K线下载。`/status.catalog_worker`报告队列错误。

分类kind为future、option、stock、index、fund、bond，ETF为fund的subtype=etf，旧kind=etf查询仍可用。行业概念为独立板块及成员快照，不作为证券。期货期权代码保留大小写和组合符号，例如`cu2610.SF`、`HO2609-C-2500.IF`、`10010971.SHO`、`SP a2611&a2701.DF`。文本代码列表使用逗号分隔，推荐SDK数组；不能将期货代码全部转换为大写。

新增`xtdata.get_instrument_details(stock_list)`（最多100个）、`get_sector_tree()`及`get_option_detail_data(stock_code)`。旧桥无法提供新增能力时明确报错；单条普通证券资料可回退原接口。`/source/test`的catalog_version=2表示已加载扩展桥，不表示市场数据已可用。

## 多选与批量接口（2026-09-18）

旧单对象接口、默认source=qmt及返回形状保持。以下POST接口使用JSON数组；GET目录的kind、market、subtype和资料选项exchange接受逗号分隔多值。来源保持单选。

| 方法 | 路径 | 参数 / 行为 |
| --- | --- | --- |
| POST | /catalog/select | 同目录筛选，可用数组；同一数据库快照返回全部匹配rows及total，超过10000项拒绝，绝不截断 |
| POST | /catalog/sync | Tushare新增exchanges数组，省略仍六所；活动任务只接受相同或子集范围 |
| POST | /history/query | members、periods、start、end、source、limit/offset；按code/period/time稳定分页，含逐行normalization_version、顶层normalization_versions及单位 |
| POST | /downloads/batch | dataset_ids（1..1000）、periods、source、可选start/end；每个数据集取周期交集，无交集或任一校验失败整批拒绝；返回jobs |
| POST | /exports/batch | members、periods、start/end、source、format；每个周期一个原export任务及文件，返回jobs |
| POST | /futures/records | selections（同一种resource）、start/end、limit/offset；多目标合并分页，字段与单位不变 |
| POST | /futures/sync、/futures/export | selections、start/end，采集另需account_id，导出另需format；按resource分组创建原任务，返回jobs、selection_count |
| POST | /jobs/query | states、kinds、sources数组或逗号字符串；search匹配ID/错误，可选start/end按上海创建日期；limit=1..200、offset，返回rows/total/next_offset |

资料selections元素为`{"resource":"calendar","exchange":"DCE"}`、`{"resource":"warehouse","exchange":"SHFE","symbol":"CU"}`、`{"resource":"holding","exchange":"DCE","symbol":"A2611"}`或`{"resource":"mapping","code":"A.DCE"}`。对象去重，日期只取顶层；查询禁止混合资料类型，同步和导出支持多类型。一次最多10000对象、1000任务和100000分块，HTTP另有请求体限制，超限明确报错。全部目标预校验通过后，在同一事务创建任务；已知权限不足不创建部分任务。

SDK新增select_catalog、query_history、query_futures、download_batch、export_batch、sync_futures_batch、export_futures_batch、query_jobs；sync_catalog增加exchanges。

```python
rows = client.query_history(
    ["A2611.DCE", "CU2610.SHF"], ["1d", "1w", "1mo"],
    "2026-08-01", "2026-09-17", source="tushare", limit=300)
reports = client.query_futures([
    {"resource": "warehouse", "exchange": "DCE", "symbol": "A"},
    {"resource": "warehouse", "exchange": "SHFE", "symbol": "CU"},
], "2026-09-17", "2026-09-17")
```

网页快捷范围按上海日期计算并包含今天；近三天含今天及前两天，一周含前六天，月/年按日历回退并钳制月末，不代表相应交易日数量。周/月查询仍按周期标签和计算截至日口径。采集账号不可用不会阻止已入库数据查询与导出。

## 时间、数值与覆盖

### QMT日历与主力映射

2026-09-21原生适配：当前月份代码未带后缀时按请求所属市场补齐；原始值仍保留。近月连续别名的品种关系需终端ProductID证据。能力检测新增limited（仅已观察交易日）与bridge_outdated（桥缺动作）；终端缺原生接口仍unsupported，但不再建议重复更新同版模型。

独立日历未提供时，从同源证券目录按代码排序选取该市场最多三个连续合约，用period=1d读取K线日期并集，不下载行情。证据保存在trading_dates.source_fields，含样本代码与原始日期；仅入库实际观察到的开市日，未返回日期保持未知，任务为partial。空响应、权限拒绝、连接错误不伪造日历；历史映射仍不能用当前快照补齐。

`GET /sources` 的QMT项新增 `reports` 和 `reference_capabilities`；`POST /sources/qmt/references/test` 接受 `resource=calendar|mapping`、`mapping_mode=current|history` 和可选31天以内的检测日期。该检测只读终端缓存，不发下载、不写业务资料；历史空响应不等于无权限。能力按样本返回状态、时间、原因，服务重启后需重新检测。

现有 `/futures/options`、`/futures/records`、`/futures/summary`、`/futures/sync`、`/futures/export` 对calendar/mapping接受 `source=qmt`。QMT映射默认current，Tushare默认history；同一任务/查询不能混来源或模式。QMT采集不绑定Tushare账号。SDK新增 `test_qmt_references`，现有资料查询、sync_futures及导出增加可选source，旧Tushare默认保留。

```python
client.sync_futures('mapping', source='qmt', code='a00.DF', mapping_mode='current')
client.sync_futures('calendar', '2026-09-14', '2026-09-20', source='qmt', exchange='DCE')
client.futures_records('mapping', '2026-09-14', '2026-09-20', source='qmt', code='a00.DF', mapping_mode='history')
```

current查询和导出返回所选合约最新保存快照，不按业务日期筛选；采集始终记录执行时状态，历史日期不会触发历史回填。observed_at为采集时间，trading_day仅采用终端明确字段，可为NULL或晚于采集自然日。原任务核验读取原快照，不读后续新快照冒充原结果。

日历记录的evidence为calendar（独立日历）、calendar_open（未来已返回开市日）、bar_observation（K线日期观察）或legacy（旧记录）。只有独立日历明确覆盖的过去区间可补充休市；未来未确认日期不填休市，空响应不产生全休市日历。旧 `/calendar` 仍只返回已保存开市日列表，完整资料及证据请用 `/futures/records`。

日期为 `YYYY-MM-DD`，查询包含结束日。行情时间以Asia/Shanghai解析；日线统一到交易日零点，分钟保留原始行情时间。QMT毫秒epoch与YYYYMMDD/YYYYMMDDHHMMSS格式显式解析。新增open_interest、settlement、previous_settlement和trading_day可空字段，原生settle映射结算价，openInterest映射持仓量。

夜盘按终端明确提供的tradingDay/tradingDate归属，保留实际行情时间；未提供则trading_day为空、按上海自然日查询且覆盖待核验，不推断周末或假期归属。日线使用终端日线标签。历史查询与导出采用相同日期口径。期货、期权及债券不请求股票复权因子；调度按数据集各市场实际成员取得日历，仍在交易日17:00回补最近五个已结束交易日。

所有历史来源是 `postgresql`，adjustment为none。价格、量额以十进制字符串传给HTTP/SDK，空值是null。CSV为UTF-8 BOM、空字段代表NULL；Parquet由PyArrow生成，数值列string避免截断，NULL仍为null。UI图表转换为JS Number只用于显示，表格和导出不丢失数据库精度。

任务状态partial表示已处理但仍有未确认空段、缺口或不可取得的因子。分钟内覆盖需要对终端时段进一步验证，不能把已有行数等同于完整数据。业务完成以读回校验与入库为准，不使用订阅号或下载请求返回值作为成功依据。

QMT历史和日历同时为空时报告SOURCE_NOT_READY并停止后续分块，当前检查点不推进；无可用结果时blocked，已有可用结果时partial。仅行情为空但日历可读时记录缺口，并按所有分块结果汇总状态。手动重试创建关联任务，只处理未完成或明确可补范围，保留原任务证据。调度不能取得日历或最近五个交易日时，在 `/status` 的worker字段说明原因，不猜测交易日或自动登录。

文件导出使用独立队列，QMT下载或日历请求阻塞时仍能查询、导出已入库数据。每个schema的每个来源最多一个下载任务，另有一个共享导出队列，各队列内部按创建时间处理。服务重启后导出重新生成整个文件，不拼接旧的临时文件。
