# 验证记录

日期：2026-09-16。主程序环境：Windows、CPython 3.12.10。下列结果来自本项目，不使用原cfquant测试数量充当本项目结果。

## 离线与隔离环境通过

- 完整测试：`78 passed in 12.66s`，0失败、0跳过。设置了隔离测试DSN和浏览器开关。随后中文密码处理和请求超时参数校验调整的相关回归为`27 passed in 3.47s`。
- PostgreSQL：独立临时`postgres:14`容器，本机端口55436；每个测试创建独立`pfor_qmt_test_*` schema并清理。未使用其他应用数据库。
- 上游：固定协议相同输入对比、提取行情方法参数/结果对比、迁移纯JSON回归测试；特殊数值、缺失值、DataFrame和大整数。
- 行情边界：SDK/hub/桥端拒绝交易与超范围操作；历史读取关闭订阅与补值；旧下载回退全部证券；内部TypeError不误判签名缺失；首包、归属、退订失败保留。
- Windows真实命名管道：本机hub、模拟QMT桥、SDK收发，客户端断线重连，绕过SDK的交易请求在hub拒绝。这不是实际QMT终端。
- 数据与任务：迁移幂等、schema边界、精确NUMERIC、NULL、重复入库修订、取消后不写入、检查点续跑、空表partial、下载拒绝不读取旧缓存冒充成功、网络3次重试、能力错误不重试、调度17:00与去重、来源互斥、指数数据集固定成员。
- 导出：CSV UTF-8 BOM、Parquet精确十进制文本、NULL与口径JSON、QMT离线时导出、数据一致性；5002行跨5000行分页的两种格式均验证无丢失。
- HTTP/SDK：认证、无凭据披露、外部Origin拒绝、网页登录Cookie、中文密码、可选密码启停、数据集/任务/取消/重试往返；hub尊重180秒下载请求超时。
- WebSocket：一次性票据、失效票据拒绝、任务事件推送。
- 部署：合成QMT目录不覆盖其他模型，同名未托管模型拒绝、运行中拒绝、XML并发变更拒绝、重复启用无重复节点；内嵌代码Python3.6语法解析，定时回调无参数及原生定时器ID处理。

## 浏览器通过

Chromium真实浏览器，1440×980桌面、390×844手机、768×1024平板。覆盖登录/退出、历史查询、K线canvas非空像素、数据集创建、CSV后台导出与下载，以及五个页面的视口与横向溢出检查。无JavaScript页面错误。

截图位于本地`output/playwright/`，不提交，不进入发布包。截图中的K线来自隔离测试库的合成数据，未注入用户运行数据库。

## 构建通过

`python -m build`生成wheel和sdist；`tools/verify_packages.py`检查迁移SQL、QMT入口、本地ECharts/Lucide、MIT/Apache/BSD/ISC相关许可与NOTICE。包中无runtime、log、exports、output、本地连接配置或虚拟环境。`pip check`通过，JavaScript语法检查通过。

Python依赖固定在`requirements.lock`，构建依赖固定在pyproject.toml；前端资源版本和SHA-256保存在`docs/assets-manifest.json`。

## 实机未验证

尚未提供用户PostgreSQL连接和QMT根目录。因此没有对用户真实数据库执行迁移，没有修改真实QMT模型或触发行情下载，也没有账户交易行为。

真实Tick、真实日/分钟线、复权因子、权限、单位与时间标签、终端RZRK导入及自动运行、实际17:00周期、长期运行和大规模回补性能均未验收。分钟内覆盖保留待核验状态。执行次序见`LIVE_ACCEPTANCE.md`，先指数/股票/ETF小样本，再扩大数据量。

## 当前失败

最后一轮自动化检查没有失败。实机项目为未验证，不记作通过或失败。
