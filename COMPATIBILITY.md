# 兼容矩阵

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
