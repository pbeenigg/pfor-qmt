# pfor-qmt

pfor-qmt 是面向 QMT 与 Tushare 的本地行情数据工作台。它把行情目录、历史 K 线、期货资料、下载任务、数据质量、PostgreSQL 存储和 CSV/Parquet 导出放在同一条数据链路中，提供 Web 控制台和 Python SDK。

项目只处理行情与资料，不提供下单、资金、持仓交易操作。QMT 和 Tushare 的数据按来源隔离保存，不自动切源、不混合计算；数据源离线时，已入库数据仍可查询和导出。

## 能力范围

| 模块 | 能力 |
| --- | --- |
| QMT 行情 | 证券目录、实时订阅、日线、1/5 分钟线、指数成分、行业概念和本地行情桥 |
| Tushare 期货 | 合约目录、交易日历、日/周/月线、1/5/15/30/60 分钟线、主力映射、仓单、成交持仓排名、结算参数和交易周报 |
| 数据管理 | PostgreSQL 独立 schema、断点续传、失败重试、质量核验、自动维护和任务事件 |
| 工作台 | 全局数据源、搜索与多选、分页查询、图表统计、任务详情、日志和文件导出 |
| SDK | `DataClient` 访问目录、历史、资料、任务和导出；`xtdata` 提供 QMT 行情桥接口 |

Tushare 分钟数据需要独立接口权限；Tick 数据没有公开 API，本项目不提供虚构的 Tick 下载入口。QMT 的历史数据能力取决于终端登录状态、行情服务器和本地缓存。

## 架构

```text
QMT / Tushare
      │
      ▼
来源适配器 → 统一标准化、校验、检查点 → PostgreSQL（pfor_qmt）
      │                                      │
      ├── Web 控制台 / HTTP API / Python SDK ─┤
      └── 任务、质量、日志、CSV/Parquet 导出
```

行情数据使用 `Decimal/NUMERIC` 保留精度，空值不补零，时间按 `Asia/Shanghai` 解释。任务执行状态和数据质量状态分开记录；“部分完成”“待核验”“未发布”“缺失”和“阻塞”含义不同，详情见[质量核验](docs/QUALITY_VERIFICATION.md)。

## 环境要求

- Windows，Python 3.12；QMT 内嵌脚本保持 Python 3.6 兼容。
- PostgreSQL 14 或更高版本，用于历史库和任务记录。
- QMT 实时/历史采集需要已安装并登录大 QMT；Tushare 历史资料不依赖 QMT 在线。

## 快速启动

```powershell
cd D:\国金\pfor-qmt
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m pip install -e . --no-deps
.venv\Scripts\pfor-qmt key
.venv\Scripts\pfor-qmt serve
```

打开 <http://127.0.0.1:8766>，使用 `key` 命令输出的 API Key 登录。首次使用先在“数据源设置”保存 PostgreSQL DSN，再初始化数据库：

```powershell
.venv\Scripts\pfor-qmt configure --database --qmt-root D:\QMT
.venv\Scripts\pfor-qmt migrate
```

所有本地配置统一保存于根目录 `config.toml`，模板见 [config.example.toml](config.example.toml)。配置优先级为：命令行参数 > `PFOR_QMT_*` 环境变量 > TOML > 默认值。真实配置、Token、密码和运行数据不会提交到 Git。

## 第一次采集

1. 在“数据源设置”选择 QMT 或 Tushare。使用 Tushare 时先新增账号并录入 Token，Token 只写入本地配置，不回显到 API。
2. 在“采集执行”同步证券或期货目录；使用 QMT 前确认终端已登录，使用 Tushare 时选择具体账号。
3. 在“证券目录”搜索代码或名称，创建数据集并选择周期、成员和日期范围。
4. 在“采集执行”创建历史回补任务，确认来源、账号、周期和范围后提交。
5. 在“历史行情”或“期货资料”查询已入库数据，在任务详情查看分块质量；需要文件时从导出入口生成 CSV 或 Parquet。

QMT 首次部署需要退出 QMT，在设置页依次执行“检查”“准备”“启用”，再启动并登录终端。部署器只管理项目自己的 `PFOR_MARKET` 模型和文件，不会操作其他模型。完整步骤见[实机验收](docs/LIVE_ACCEPTANCE.md)。

## 服务管理

```powershell
.\pfor.ps1 start
.\pfor.ps1 status
.\pfor.ps1 restart
.\pfor.ps1 stop
.\pfor.ps1 logs -Tail 100
```

脚本只管理配置路径匹配的 pfor-qmt 服务，不停止 QMT、PostgreSQL 或其他应用。服务重启后会恢复未完成任务；已经发出的终端请求无法撤销。

## Python SDK

```python
from pfor_qmt import DataClient, xtdata

client = DataClient.from_config("config.toml")
catalog_job = client.sync_catalog(["future", "option"], source="qmt")
contracts = client.catalog(kind="future", search="沪铜")
dataset = client.create_dataset("核心合约", ["cu2610.SF"], ["1d"])
job = client.download(dataset["id"], "2026-09-01", "2026-09-14")
print(client.job(job["id"]))
```

`DataClient` 访问认证 HTTP API；`xtdata` 需要本机行情桥在线。SDK、HTTP API、数据库和导出文件共用同一来源、单位和质量口径，接口示例见 [API 文档](docs/API.md)。

## 开发与验证

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m build
```

浏览器测试需要额外安装 Playwright 和 Chromium。数据库测试使用隔离 schema，设置 `PFOR_QMT_TEST_DSN` 后运行；不要让测试连接业务数据库。项目结构见[架构说明](docs/ARCHITECTURE.md)，数据库表、字段和 ER 图见[数据库字典](docs/DATABASE_SCHEMA.md)，配置项见[配置说明](docs/CONFIGURATION.md)。

## 发布

`.github/workflows/python-publish.yml` 在 GitHub Release 发布后构建 wheel 和源码包，同时上传 PyPI 与 Release 附件。发布前同步更新 `pyproject.toml` 的版本号、Git 标签和 Release；首次发布需在 PyPI 配置 GitHub Trusted Publisher。详细发布参数以工作流文件为准。
