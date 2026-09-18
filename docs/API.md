# API 与数据口径

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

目录任务kind为catalog，使用独立串行队列和schema级锁，不受历史任务排队影响；原生调用阻塞时仍需等待桥端响应。新版桥每批最多100个合约资料；可用的金融/ETF期权专用详情最多8个并发补齐，商品期权未返回的字段保持缺失。旧资料接口按16条批次回退。资料或板块快照与检查点同事务保存。网络失败最多重试3次，失败/取消重试继续检查点，partial重试全部读取。缺少名称或板块成员时保留旧资料并列入result.missing，不构造名称；不删除旧证券，不触发K线下载。`/status.catalog_worker`报告队列错误。

分类kind为future、option、stock、index、fund、bond，ETF为fund的subtype=etf，旧kind=etf查询仍可用。行业概念为独立板块及成员快照，不作为证券。期货期权代码保留大小写和组合符号，例如`cu2610.SF`、`HO2609-C-2500.IF`、`10010971.SHO`、`SP a2611&a2701.DF`。文本代码列表使用逗号分隔，推荐SDK数组；不能将期货代码全部转换为大写。

新增`xtdata.get_instrument_details(stock_list)`（最多100个）、`get_sector_tree()`及`get_option_detail_data(stock_code)`。旧桥无法提供新增能力时明确报错；单条普通证券资料可回退原接口。`/source/test`的catalog_version=2表示已加载扩展桥，不表示市场数据已可用。

## 时间、数值与覆盖

日期为 `YYYY-MM-DD`，查询包含结束日。行情时间以Asia/Shanghai解析；日线统一到交易日零点，分钟保留原始行情时间。QMT毫秒epoch与YYYYMMDD/YYYYMMDDHHMMSS格式显式解析。新增open_interest、settlement、previous_settlement和trading_day可空字段，原生settle映射结算价，openInterest映射持仓量。

夜盘按终端明确提供的tradingDay/tradingDate归属，保留实际行情时间；未提供则trading_day为空、按上海自然日查询且覆盖待核验，不推断周末或假期归属。日线使用终端日线标签。历史查询与导出采用相同日期口径。期货、期权及债券不请求股票复权因子；调度按数据集各市场实际成员取得日历，仍在交易日17:00回补最近五个已结束交易日。

所有历史来源是 `postgresql`，adjustment为none。价格、量额以十进制字符串传给HTTP/SDK，空值是null。CSV为UTF-8 BOM、空字段代表NULL；Parquet由PyArrow生成，数值列string避免截断，NULL仍为null。UI图表转换为JS Number只用于显示，表格和导出不丢失数据库精度。

任务状态partial表示已处理但仍有未确认空段、缺口或不可取得的因子。分钟内覆盖需要对终端时段进一步验证，不能把已有行数等同于完整数据。业务完成以读回校验与入库为准，不使用订阅号或下载请求返回值作为成功依据。

历史和日历同时为空时任务失败并停止后续分块，当前检查点不推进；仅行情为空但日历可读仍为partial。failed/cancelled下载重试从检查点继续，partial重试从0开始。调度不能取得日历或最近五个交易日时，在 `/status` 的worker字段说明原因，不猜测交易日或自动登录。

文件导出使用独立队列，QMT下载或日历请求阻塞时仍能查询、导出已入库数据。每个schema最多一个下载任务和一个导出任务同时执行，各队列内部按创建时间处理。服务重启后导出重新生成整个文件，不拼接旧的临时文件。
