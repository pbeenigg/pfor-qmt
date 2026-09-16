# 源码来源与移植记录

- 来源：https://github.com/95ge/cfquant
- 固定提交：`5baa4daf8dab01fb415afdd45a72cd254cea042f`
- MIT，Copyright (c) 2026 tao；完整原始许可证保存在根目录 `LICENSE`。
- 通过 `git show <commit>:<path>` 读取受版本控制文件，没有复制源工作区、忽略文件、凭据、数据库或运行数据。

## 路径映射

| 上游 | 派生项目 | 原因 |
| --- | --- | --- |
| cfquant/protocol.py | pfor_qmt/protocol.py | 保留序列化与 DataFrame 协议 |
| cfquant/pipe_transport.py | pfor_qmt/pipe_transport.py | 保留 Windows 管道与双连接恢复 |
| cfquant/pipe_client.py | pfor_qmt/pipe_client.py | 保留 RPC，补充远端错误类型用于重试分类 |
| cfquant/pipe_hub.py | pfor_qmt/pipe_hub.py | 保留转发、超时和状态管理，由 MarketHub 收紧白名单 |
| cfquant/logging_i18n.py | pfor_qmt/logging_i18n.py | 管道日志依赖；字符串不代表开放对应能力 |
| cfquant/qmt_strategy_package.py | pfor_qmt/qmt_strategy_package.py | 保留 RZRK 格式，仅修改模型名规则和介绍 |
| cfquant/tx_trade_bridge.py 行情方法 | pfor_qmt/qmt_methods.py | AST 提取纯行情方法，不继承交易类 |
| cfquant/level2.py 的 quote_* | pfor_qmt/quote.py | 仅提取通用回调格式化，不提取 Level2 功能 |
| cfquant/tests/test_json_serialization.py | tests/test_upstream_json.py | 仅迁移纯协议数值测试，移除交易与 LITE 测试依赖 |
| cfquant/protocol.py | tests/reference/protocol.py | 固定原始参考，用相同输入比对 |

精确原始文件 SHA-256、选取方法和命名替换见 `docs/upstream-manifest.json` 与 `docs/test-upstream-manifest.json`。生成工具位于 `tools/`；重新执行会重建相应文件，应在干净分支检查差异，不能覆盖未经移植记录登记的本地修改。

## 有意保留的标记

`cfquant:`、`cfpipe:`、`protocol=cfquant`、`__cf_type__` 与内部 `Cfquant*` 类名属于已有传输格式。它们不用于模型发现、部署归属或清理。运行隔离使用 `pfor_qmt_pipe_hub`、`pfor_qmt.market.request`、`PFOR_MARKET`、`PFOR_QMT_*` 环境变量及独立 schema。

## 新增与改写

`market_bridge.py` 新建无交易继承的派发器，依据上游 `normal_bridge.py` 的原生订阅参数、首包与退订行为适配；上游 `_call_variants` 被覆盖为先绑定签名，避免误吞内部 TypeError。旧下载接口回退覆盖所有证券。

`deploy.py` 参考上游 `qmt_strategy_deploy.py` 的导入队列与模型 XML 字段，重新限定唯一自有模型，保留备份和并发检查，不复制上游清理逻辑或完整模型模板。

`MarketHub` 根据每次请求的timeout清理等待项，避免上游默认60秒维护阈值先于180秒下载调用结束。

数据库、任务、SDK DataClient、HTTP/WebSocket、Web 工作台为本项目新增。界面没有复制上游产品标识。后续升级采用选择性移植、补充来源与兼容矩阵、执行回归测试；不直接覆盖整个包。
