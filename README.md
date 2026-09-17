# pfor-qmt

面向国内指数、A 股、场内 ETF 的本地行情工作台。基于 [cfquant](https://github.com/95ge/cfquant) 固定提交派生，保留 MIT 许可证；不是上游官方产品。

提供大 QMT 命名管道行情桥、Python SDK、PostgreSQL 历史库、下载任务、CSV/Parquet 导出和原生 Web 工作台。第一期仅支持不复权日线、1/5 分钟线和当前指数成分，不包含任何交易接口。

## 快速启动

主程序需要 Windows、Python 3.12；历史库需要 PostgreSQL 14+。QMT 内嵌组件保持 Python 3.6 语法兼容，独立部署，不向 QMT 安装主程序依赖。

```powershell
cd D:\国金\pfor-qmt
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m pip install -e . --no-deps
.venv\Scripts\pfor-qmt key
.venv\Scripts\pfor-qmt serve
```

打开 http://127.0.0.1:8766 ，使用 `key` 输出的 API Key 登录。WebSocket 使用本机 8767。端口占用时使用 `serve --port 8876 --ws-port 8877`。同一台机器同一 Windows 会话只运行一个服务。

在「数据源设置」保存 PostgreSQL DSN 并初始化历史库，或使用以下命令。密码仅通过隐藏输入写入本地文件，不放入命令行历史。

```powershell
.venv\Scripts\pfor-qmt configure --database --qmt-root D:\QMT
.venv\Scripts\pfor-qmt migrate
```

统一配置入口是项目根目录 `config.toml`，模板见 [config.example.toml](config.example.toml)。数据库、端口、运行目录、QMT行情连接、API Key及登录密码哈希均由这个文件管理；网页保存也写回此文件，保留注释。

优先级：**显式命令行参数 > `PFOR_QMT_*` 环境变量 > TOML > 默认值**。文件路径通过 `--config` 或 `PFOR_QMT_CONFIG` 指定，默认当前目录的`config.toml`；相对运行目录和QMT路径基于配置文件所在目录解析。环境变量和命令行覆盖只在当前进程生效，不会写回文件，也不会修改系统环境变量。

首次找不到TOML时，会迁移原`runtime/settings.local.json`中的连接、API Key和密码哈希，原JSON保留为备份，后续不再读取。真实`config.toml`已被Git和构建包排除，不要分享或放在公共目录。手工修改后重启服务；修改行情管道配置后需重新准备自有QMT模型并重启终端。

```powershell
.venv\Scripts\pfor-qmt --config D:\国金\pfor-qmt\config.toml serve
```

完整配置项和环境变量对应关系见 [配置说明](docs/CONFIGURATION.md)。

## 接入 QMT

1. 在设置页检查大 QMT 根目录，退出 QMT 后点击「准备」。
2. 启动并登录 QMT，等待 `PFOR_MARKET` 模型导入，然后退出。
3. 点击「启用」，再次启动并登录 QMT，点击「测试行情连接」。
4. 如果终端没有可唯一识别的模型账户绑定，先在 QMT 为 `PFOR_MARKET` 手工选择账户并保存，再退出并启用。该绑定仅满足终端模型运行要求，不开放交易。

部署只管理 `pfor_qmt_managed/`、`PFOR_MARKET.py`、同名导入包及同名模型节点，修改 XML 前保存备份。不会清理、停用、替换 cfquant 模型，也不会自动退出或登录 QMT。

“行情桥已连接”不代表行情服务器已登录。在设置页点击「检查历史数据」，分别查看快照时间、近30天本地日线和交易日历。历史和日历均为空时，先在QMT手动检查行情连接和日K线；不要直接扩大回补。只读命令为 `.venv\Scripts\python.exe -X utf8 tools/live_acceptance.py`。

## 初次使用

在行情页同步选定证券资料；指数页加载终端板块列表，为指数代码建立明确的板块映射，刷新当前成分并创建数据集。历史库页可直接用证券列表建数据集。分钟线需显式勾选。

行情页订阅后显示证券数量。修改代码并再次订阅可切换证券；方形停止按钮退订行情并保留最后显示值，任务推送保持连接。断线时显示等待恢复，连接恢复后重订阅；退出登录会关闭连接，重新登录后需再次订阅。

下载任务页选择数据集后回补；留空日期时日线默认一年、分钟线90天。已创建任务保存成员与日期分块，不受之后成员变化影响。按数据集启用交易日17:00更新；进程必须运行，关机期间到期任务在恢复后补建。

文件导出独立于行情下载队列，QMT请求等待或失败不会阻塞已有数据导出。任务页分别显示行情调度和文件导出的后台错误；下载仍限制为单源单任务。

数据库保存 QMT 原始、不复权 OHLC、成交量和成交额，不推断或换算终端单位；缺失值保留空值。当前成分快照只表示观察时点，不代表历史生效日期。历史库查询和导出可以在 QMT 离线时使用。

先验证指数、股票、ETF的短区间日线，再扩大到分钟线及更多证券。[实机验收步骤](docs/LIVE_ACCEPTANCE.md)提供小样本工具，可核对重复回补、SDK分页、数据库及两种文件格式；显式使用 `--download` 才会创建任务和写入真实数据。

## Python SDK

```python
from pfor_qmt import DataClient, xtdata

client = DataClient.from_config("config.toml")
xtdata.configure_from_file("config.toml")
dataset = client.create_dataset("核心指数", ["000300.SH", "000905.SH"], ["1d"])
job = client.download(dataset["id"], "2026-09-01", "2026-09-14")
print(client.job(job["id"]))
page = client.history("000300.SH", limit=500)
tick = xtdata.get_full_tick(["000300.SH"])
```

`DataClient` 通过认证 HTTP 接口访问历史库，数值以精确十进制字符串返回。`xtdata` 是本机管道直连接口，需要行情桥在线。订阅回调不得同步调用管道 RPC，应把数据转交队列；自行使用 `xtdata` 的客户端在重连后需要重新订阅。Web 工作台自动恢复订阅。

## 开发与验证

```powershell
.venv\Scripts\python -m pip install -e '.[dev]'
.venv\Scripts\python -m pytest -q
# 仅设置为隔离测试数据库，测试会创建并删除 pfor_qmt_test_* schema
$env:PFOR_QMT_TEST_DSN = 'postgresql://user:password@127.0.0.1:55436/pfor_test'
.venv\Scripts\python -m playwright install chromium
$env:PFOR_QMT_BROWSER_TEST = '1'
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m build
```

测试没有覆盖率百分比门槛；必须通过交易拒绝、命名隔离、重复入库、任务恢复、调度、导出和界面回归。无测试数据库或浏览器开关时，相应测试会显式跳过，不能据此宣称全部验收通过。

项目结构、接口与状态见 [执行规范](AGENTS.md)、[架构](docs/ARCHITECTURE.md)、[API](docs/API.md)、[兼容矩阵](COMPATIBILITY.md)、[验证记录](docs/VERIFICATION.md)。
