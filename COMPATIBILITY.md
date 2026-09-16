# 兼容矩阵

基准为 cfquant `5baa4daf8dab01fb415afdd45a72cd254cea042f`。这是明确裁剪后的行情产品，不宣称整个 cfquant API 兼容。

| 接口或能力 | 处理 | 验证状态与差异 |
| --- | --- | --- |
| 协议序列化、DataFrame、缺失值 | 保留 | 固定上游协议相同输入对比、迁移测试通过 |
| Windows 管道 RPC | 保留并隔离名称 | 本机真实管道收发、客户端重连、错误返回测试通过；hub改为按请求超时清理，避免60秒默认值提前结束180秒下载 |
| get_full_tick | 保留 | 参数及派发检查；真实 Tick 尚待 QMT |
| get_market_data_ex | 保留行情子集 | 1d/1m/5m，不复权；默认 fill_data=False，与上游默认存在差异 |
| get_local_data | 修改 | 本地读取优先第九参数 subscribe=False；不补行情，不接受复权别名绕过 |
| get_instrument_detail | 裁剪回退 | 保留原生新旧名称；无可用能力时明确失败，不构造猜测资料 |
| get_sector_list / get_stock_list_in_sector | 保留当前数据 | 板块树遍历与当前成分；历史 timetag 被拒绝 |
| get_trading_dates / get_divid_factors | 保留 | 保存原始因子及观察时间；真实终端能力待验证 |
| download_history_data / data2 | 修改回退 | 旧接口逐证券；返回只代表请求结束，不代表入库成功；回调仅在同步 RPC 有效期内接收 |
| subscribe_quote / whole_quote | 改写纯行情实现 | 原生参数、首包、退订失败保留、客户端归属测试通过；订阅 ID 为不透明字符串 |
| 订阅恢复 | 修改 | 网页重连后重建订阅；直接 SDK 使用者需自行重订阅，不在回调线程同步 RPC |
| 交易、账户、资金持仓 | 移除 | SDK 无接口；hub/桥端/HTTP 请求拒绝测试通过 |
| 多源、高级/LITE、Level2、千档、财务、公式、历史权重 | 移除 | 不迁移对应入口或初始化逻辑 |
| DataClient、/api/v1、数据库与任务 | 新增 | PostgreSQL 隔离测试；HTTP/SDK/文件一致性测试 |
| WebSocket | 新增 | 独立本机8767，一次性30秒票据，任务与行情推送 |
| CSV / Parquet | 新增 | CSV BOM；空值保留；Parquet 数值使用十进制文本 string，避免任意 NUMERIC 精度损失 |
| QMT 模型部署 | 重写隔离边界 | 合成终端目录验证不改其他模型、同名冲突阻止、并发变更阻止；真实导入运行未验证 |

国内六位 SH/SZ/BJ 证券范围检查不等于保证每个代码类别，证券类别由同步资料时指定。终端权限、数据覆盖、字段单位、交易时间戳与 RZRK 实机导入仍需小样本验收。

分钟任务记录未确认的分钟内覆盖情况，因此可呈现「待核验」；空表永远不标记业务成功。历史库存原始数据，图表使用 JS 数值显示，表格、API 与导出保留精确十进制文本。
