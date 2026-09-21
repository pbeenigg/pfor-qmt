# 统一配置

## 运维与追溯

```toml
[operations]
event_retention_days = 90
sample_retention_days = 30
repair_attempts = 3
```

对应环境变量为`PFOR_QMT_EVENT_RETENTION_DAYS`、`PFOR_QMT_SAMPLE_RETENTION_DAYS`、`PFOR_QMT_REPAIR_ATTEMPTS`；沿用CLI、环境、TOML、默认值顺序，修改后重启服务。样本保留期不得超过事件保留期，自动补数上限为3。

清理仅处理事件与样本，不删除行情、任务、分块缺口、检查点或导出文件。数据库无法写入时，后台错误落入`runtime/worker-qmt.jsonl`、`runtime/worker-tushare.jsonl`，按2MiB轮转、保留5份备份，内容脱敏。应用只能监测运行目录所在磁盘；远端或Docker数据库卷的剩余空间须单独监控，不以宿主机读数替代。

工作台“下载任务 → 任务详情 → 设为自动维护”保存固定范围；“运行与日志”查看并停用。保存维护范围不会自动重跑之前的大范围失败任务；原数据集已启用自动更新时拒绝创建重复维护范围。停用不自动取消已有任务。启用范围引用的Tushare账号不允许删除，范围中的账号与端点不受默认账号切换影响。

## Tushare 多账号

数据源设置页管理账号，仍写回同一个config.toml。账号ID不可改名，名称可修改；Token只写入、不回传，输入留空保持原值，勾选清除才删除。不保存Tushare网站密码。

```toml
[tushare]
default_account_id = "research"

[[tushare.accounts]]
id = "research"
name = "期货研究"
endpoint = "https://api.tushare.pro"
token = ""
enabled = true
timeout = 30.0
requests_per_minute = 60
```

每个账号可通过`PFOR_QMT_TUSHARE_RESEARCH_TOKEN`、`_ENDPOINT`、`_TIMEOUT`、`_REQUESTS_PER_MINUTE`覆盖对应值；环境覆盖不写回TOML。账号ID只允许小写字母开头及字母、数字、下划线，最多32位。端点支持相同Tushare JSON协议，不在URL中放凭据，不跟随重定向转发Token。

网页「检测接口权限」分别报告目录、日历、日线、分钟和主力映射，不把认证可用等同于所有接口可用。分钟需要单独授权；不在聊天、Git、日志或导出中提供Token。同Token的账号共享限流，不自动轮换账号。修改Token后重新检测，已失败任务可重试。

数据集保存来源、账号和端点，任务创建时固定这些字段。默认账号变化不改变已有任务。可在数据集列表切换采集账号，已创建任务仍保留原账号。账号被数据集引用时不能删除，活动任务未取消前不能改端点或停用账号。已入库查询和导出无需Token。

项目使用一个本地`config.toml`管理运行参数，`config.example.toml`是不含凭据的受版本控制模板。TOML由tomlkit结构化读写，网页设置与命令行设置写回同一文件，并保留用户注释。

## 加载顺序

配置文件位置：`--config 路径` > `PFOR_QMT_CONFIG` > 当前工作目录下的`config.toml`。

参数优先级：命令行显式覆盖 > 对应环境变量 > TOML值 > 程序默认值。没有传入的命令行参数不会覆盖TOML。相对路径基于配置文件目录，与启动时工作目录无关。

```powershell
# 从任何目录启动同一份配置
& 'D:\国金\pfor-qmt\.venv\Scripts\pfor-qmt.exe' --config 'D:\国金\pfor-qmt\config.toml' serve
# 临时覆盖端口，不改配置文件
.venv\Scripts\pfor-qmt --config config.toml serve --port 8876 --ws-port 8877
```

## 字段映射

| TOML字段 | 环境变量 | 默认值 |
| --- | --- | --- |
| app.runtime_dir | PFOR_QMT_RUNTIME_DIR | runtime |
| server.host | PFOR_QMT_HOST | 127.0.0.1 |
| server.port | PFOR_QMT_PORT | 8766 |
| server.ws_port | PFOR_QMT_WS_PORT | 8767 |
| database.dsn | PFOR_QMT_DATABASE_URL | 空，未配置 |
| qmt.root | PFOR_QMT_QMT_ROOT | 空，未配置 |
| qmt.pipe_name | PFOR_QMT_PIPE_NAME | 本机pfor_qmt_pipe_hub管道 |
| qmt.request_channel | PFOR_QMT_REQUEST_CHANNEL | pfor_qmt.market.request |
| qmt.timeout | PFOR_QMT_TIMEOUT | 15秒 |
| qmt.connect_timeout_ms | PFOR_QMT_PIPE_CONNECT_TIMEOUT_MS | 1500毫秒 |
| qmt.heartbeat_seconds | PFOR_QMT_PIPE_HEARTBEAT_SECONDS | 10秒 |
| hub.pending_timeout | PFOR_QMT_PIPE_HUB_PENDING_TIMEOUT | 60秒，请求自身timeout优先 |
| hub.heartbeat_timeout | PFOR_QMT_PIPE_HUB_QMT_HEARTBEAT_TIMEOUT | 30秒 |
| hub.maintenance_interval | PFOR_QMT_PIPE_HUB_MAINTENANCE_INTERVAL | 2秒 |
| security.api_key | PFOR_QMT_API_KEY | 首次随机生成 |
| security.login_hash | 无，网页管理 | 空，关闭密码登录 |

HTTP和WebSocket默认绑定127.0.0.1；需要外部访问时设置`server.host = "0.0.0.0"`，同时开放HTTP和WebSocket端口并配置HTTPS。schema固定为pfor_qmt，不开放绕过schema隔离的配置。端口必须有效且不同，未知字段和错误类型直接报错，不能静默忽略拼写错误。这里是应用配置层，不向Windows全局环境写变量，也不提供任意环境变量执行入口。

上游保留的低层诊断环境变量（日志、状态文件、进程实例标识等）仍可临时使用，但不作为业务配置。测试仅使用PFOR_QMT_TEST_DSN及PFOR_QMT_BROWSER_TEST，绝不自动取业务DSN执行测试。

## 数据库

数据库错误不能统一解释为磁盘不足。ConnectionTimeout表示连接超时；42P01表示缺表，42703表示缺字段，28P01表示认证失败，53300表示连接数上限，53100才是PostgreSQL报告空间不足。当前版本按错误类型显示诊断及建议，不输出驱动原始连接信息。

短暂建连失败最多尝试3次，限业务SQL执行之前；不会自动重放写入事务。恢复后从关联重试任务继续未完成范围，旧任务保留原错误。清空数据不能修复网络超时；只有缺表或字段时才应核对版本并执行数据库迁移。

表注释与字段字典参见[数据库结构与ER图](DATABASE_SCHEMA.md)。schema12仅增加注释，不改变业务表结构。

连接格式为`postgresql://USER:PASSWORD@localhost:5432/pfor_qmt`。URI中真实密码若含`@`、`:`等特殊字符，应进行百分号编码；用户消息中的Markdown转义反斜杠不属于连接字符串。

本次接入本机PostgreSQL16时发现指定数据库尚不存在，已创建`pfor_qmt`并在同名schema运行迁移。业务表未填充测试行情。其他数据库和schema不作迁移或清理。

配置中的postgres管理账号方便本地初始化，但权限较高；长期运行建议创建仅有本项目所需权限的专用角色。当前未擅自创建或修改数据库账户。

## 保存、迁移与重启

找不到TOML时，首次读取所选运行目录下旧`settings.local.json`，迁移dsn、qmt_root、api_key和login_hash。原JSON不删除、不再作为后续配置源。既有TOML优先，不与旧JSON持续合并。

网页登录和API Key不明文回传。保存文件不把环境覆盖值写回；环境变量接管DSN时，网页拒绝改变该DSN。手工修改后重启应用；如果网页检测到磁盘文件被外部编辑，将拒绝覆盖，要求重启加载。

配置文件在本机含数据库凭据及API Key，必须限制文件访问，不上传Git或发布包。构建只包含示例，不包含config.toml及*.local.toml。原JSON备份仍含旧凭据，同样需要保护。

SDK使用`DataClient.from_config(path)`、`xtdata.configure_from_file(path)`加载同一入口。QMT内嵌Python3.6不直接读取TOML，也不接收数据库凭据或API Key；部署器只把行情管道、通道和超时参数写入PFOR_MARKET。修改这些参数后重新准备自有模型并重启QMT。
