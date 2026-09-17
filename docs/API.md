# API 与数据口径

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
| POST | /securities/sync | members数组、kind=stock/index/etf，最多100证券 |
| GET | /catalog | 已保存目录数量、板块和最近目录任务 |
| GET | /catalog/securities | search、kind、limit=1..200、offset；返回rows、total、next_offset，支持完整目录分页 |
| POST | /catalog/resolve | members代码数组；批量读取已保存名称，不请求QMT |
| POST | /catalog/sync | 可选kinds数组=index/stock/etf；创建持久化目录任务，已有活动同步时返回原任务 |
| GET | /sectors | 实时读取QMT板块目录 |
| GET | /indices | 映射和最新成分快照 |
| POST | /indices/refresh | code、sector，可选name；优先使用目录中的指数名称，保存观察时点快照 |
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

发送 `{"action":"unwatch"}` 停止行情订阅，WebSocket保留用于任务推送。订阅成功或停止后返回 `{"event":"watch","codes":[...]}`，空数组表示已停止。切换证券先退订原订阅；退订失败返回error及仍保留的codes，不声称停止成功。旧订阅的迟到回调及排队行情在切换、停止、桥重连后丢弃，任务事件不受影响。无效命令返回error并保持连接。

诊断返回 `checks` 数组，每项含name、state、message，适用时含rows和time。state为 `ok/empty/error/unverified`；桥失败则后续项未验证。`history_readable=true` 只表示有可读日线和区间内交易日历，不承诺行情在线、数据完整或下载通过。诊断不触发下载，不保存资料或历史数据。

配置QMT目录后，增加terminal_history项：从当日datasource日志末尾最多2MiB提取最近历史请求的证券、周期、时间与received数组，不返回原始日志、服务器地址或账户信息。仅全零结果标为空，其他接收记录仍待回读校验；过去请求不能证明当前连接状态。

目录任务kind为catalog，使用独立串行队列和schema级锁，不受历史任务排队影响；已有QMT原生调用阻塞时仍需等待桥端响应。每批最多16个证券、8个并发只读RPC，资料与检查点在同一事务保存。网络失败最多重试3次，失败/取消重试继续检查点，partial重试全部读取。缺少名称时保留旧资料并列入result.missing，不构造名称；不删除旧证券，不触发K线下载。`/status.catalog_worker`报告队列错误，目录可脱离QMT查询。

## 时间、数值与覆盖

日期为 `YYYY-MM-DD`，查询包含结束日。行情时间以Asia/Shanghai解析；日线统一到交易日零点，分钟保留原始行情时间。QMT毫秒epoch与YYYYMMDD/YYYYMMDDHHMMSS格式显式解析。

所有历史来源是 `postgresql`，adjustment为none。价格、量额以十进制字符串传给HTTP/SDK，空值是null。CSV为UTF-8 BOM、空字段代表NULL；Parquet由PyArrow生成，数值列string避免截断，NULL仍为null。UI图表转换为JS Number只用于显示，表格和导出不丢失数据库精度。

任务状态partial表示已处理但仍有未确认空段、缺口或不可取得的因子。分钟内覆盖需要对终端时段进一步验证，不能把已有行数等同于完整数据。业务完成以读回校验与入库为准，不使用订阅号或下载请求返回值作为成功依据。

历史和日历同时为空时任务失败并停止后续分块，当前检查点不推进；仅行情为空但日历可读仍为partial。failed/cancelled下载重试从检查点继续，partial重试从0开始。调度不能取得日历或最近五个交易日时，在 `/status` 的worker字段说明原因，不猜测交易日或自动登录。

文件导出使用独立队列，QMT下载或日历请求阻塞时仍能查询、导出已入库数据。每个schema最多一个下载任务和一个导出任务同时执行，各队列内部按创建时间处理。服务重启后导出重新生成整个文件，不拼接旧的临时文件。
