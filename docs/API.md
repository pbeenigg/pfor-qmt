# API 与数据口径

默认地址 `http://127.0.0.1:8766/api/v1`。SDK请求使用 `Authorization: Bearer <API Key>`；网页登录使用HttpOnly/SameSite=Strict会话。所有业务路径均需认证。HTTP错误为 `{ "error": "..." }`，不回传数据库凭据或原始驱动连接错误。

主程序统一读取config.toml，端口可通过TOML、环境变量或CLI覆盖；GET /settings仅返回脱敏配置与config_file路径，POST /settings写回同一TOML。详情见CONFIGURATION.md。

| 方法 | 路径 | 参数 / 行为 |
| --- | --- | --- |
| GET | /status, /settings | 脱敏连接状态和配置 |
| POST | /settings | dsn、qmt_root、可选password；空密码不修改；login_enabled布尔值显式启停密码登录 |
| POST | /database/migrate | 初始化/升级独立schema |
| POST | /source/test | 检查真实QMT行情桥 |
| GET | /quotes | codes逗号分隔，读取QMT快照 |
| GET | /securities | search、kind，可在终端离线时查库 |
| POST | /securities/sync | members数组、kind=stock/index/etf，最多100证券 |
| GET | /sectors | 实时读取QMT板块目录 |
| GET | /indices | 映射和最新成分快照 |
| POST | /indices/refresh | code、sector、name，保存观察时点快照 |
| GET/POST | /datasets | 列出/创建；name、members、periods，可带index_code、snapshot_id |
| POST | /datasets/{id}/refresh | 显式刷新指数数据集成员，不修改既有任务 |
| POST | /datasets/{id}/schedule | enabled布尔值，启用当天开始跟踪17:00到期日 |
| GET | /history | code、period、start、end、limit=1..5000、offset；返回rows与next_offset |
| GET | /factors | code，原始因子和观察时间 |
| GET | /calendar | market=SH/SZ/BJ、start、end；读取已保存交易日期 |
| POST | /downloads | dataset_id、可选start/end，立即返回持久化任务ID |
| GET | /jobs, /jobs/{id} | 最近200任务 / 单任务与检查点、覆盖、错误 |
| POST | /jobs/{id}/cancel, /retry | 取消后续处理 / 失败、取消、partial任务重试 |
| POST | /exports | members、period、start、end、format=csv/parquet；返回任务 |
| GET | /files/{id}, /files/{id}/metadata | 下载已完成导出文件 / 口径JSON |
| POST | /deploy/inspect, /prepare, /activate | 本地QMT目录检查、准备、启用；只管理PFOR_MARKET |
| POST | /login, /logout | credential为API Key或本地密码；创建/撤销网页登录会话 |
| POST | /ws-ticket | 一次性30秒票据及WebSocket端口 |

WebSocket默认 `ws://127.0.0.1:8767/?ticket=...`。不在URL传API Key。发送 `{"action":"watch","codes":["000300.SH"]}` 订阅；接收 `event=quote/job/source/error`，最新行情不会入库。页面重连时重新取得票据和订阅。事件队列有界，缓慢客户端应通过任务GET或行情快照重新同步。

## 时间、数值与覆盖

日期为 `YYYY-MM-DD`，查询包含结束日。行情时间以Asia/Shanghai解析；日线统一到交易日零点，分钟保留原始行情时间。QMT毫秒epoch与YYYYMMDD/YYYYMMDDHHMMSS格式显式解析。

所有历史来源是 `postgresql`，adjustment为none。价格、量额以十进制字符串传给HTTP/SDK，空值是null。CSV为UTF-8 BOM、空字段代表NULL；Parquet由PyArrow生成，数值列string避免截断，NULL仍为null。UI图表转换为JS Number只用于显示，表格和导出不丢失数据库精度。

任务状态partial表示已处理但仍有未确认空段、缺口或不可取得的因子。分钟内覆盖需要对终端时段进一步验证，不能把已有行数等同于完整数据。业务完成以读回校验与入库为准，不使用订阅号或下载请求返回值作为成功依据。
