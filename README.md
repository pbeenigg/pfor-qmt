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

默认本地配置为 `runtime/settings.local.json`，不要提交、分享或放在公共目录。也支持 `PFOR_QMT_DATABASE_URL` 环境变量，优先于配置文件；使用环境变量时更换连接需要修改环境后重启。

## 接入 QMT

1. 在设置页检查大 QMT 根目录，退出 QMT 后点击「准备」。
2. 启动并登录 QMT，等待 `PFOR_MARKET` 模型导入，然后退出。
3. 点击「启用」，再次启动并登录 QMT，点击「测试行情连接」。
4. 如果终端没有可唯一识别的模型账户绑定，先在 QMT 为 `PFOR_MARKET` 手工选择账户并保存，再退出并启用。该绑定仅满足终端模型运行要求，不开放交易。

部署只管理 `pfor_qmt_managed/`、`PFOR_MARKET.py`、同名导入包及同名模型节点，修改 XML 前保存备份。不会清理、停用、替换 cfquant 模型，也不会自动退出或登录 QMT。

## 初次使用

在行情页同步选定证券资料；指数页加载终端板块列表，为指数代码建立明确的板块映射，刷新当前成分并创建数据集。历史库页可直接用证券列表建数据集。分钟线需显式勾选。

下载任务页选择数据集后回补；留空日期时日线默认一年、分钟线90天。已创建任务保存成员与日期分块，不受之后成员变化影响。按数据集启用交易日17:00更新；进程必须运行，关机期间到期任务在恢复后补建。

数据库保存 QMT 原始、不复权 OHLC、成交量和成交额，不推断或换算终端单位；缺失值保留空值。当前成分快照只表示观察时点，不代表历史生效日期。历史库查询和导出可以在 QMT 离线时使用。

## Python SDK

```python
import json
from pathlib import Path
from pfor_qmt import DataClient, xtdata

key = json.loads(Path("runtime/settings.local.json").read_text("utf-8"))["api_key"]
client = DataClient(api_key=key)
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
