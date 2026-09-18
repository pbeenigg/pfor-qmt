# 结算参数与主要品种交易周报

## 工作台入口

顶部切换Tushare，在「期货资料」多选结算参数、成交持仓排名或主要品种交易周报。结算参数选择具体月份合约，周报选择品种；排名继续支持品种或月份合约。交易所、产品、合约均复用可搜索多选、全选和快捷日期。同步进入原下载队列，记录详情展示全部字段；查询与CSV/Parquet导出只读本地库，不要求Tushare在线。

同步前需要已同步的合约目录与可用账号。任务固定账号和端点，默认账号切换不改变原任务。可从原同步任务保存维护范围，支持取消、恢复、失败重试及只读重新核验。不包含下单、账户收费计算或交易权限。

## 数据口径

`settle`使用fut_settle，单次1600行，达到上限按日期继续细分，单日仍达到上限则明确失败。字段含结算价、交易手续费率/手续费、交割手续费、买卖套保保证金率、买卖投机保证金率、平今仓手续率。NUMERIC/Decimal保留精度，NULL不补零；官方未统一注明费率基准，保留原值并明确“原值”，不自动乘100，也不代表券商对某个账户实际收费。

`weekly_detail`使用fut_weekly_detail，单次4000行，达到上限按上游周编号区间递归细分，单周仍达到上限则失败。该资料是品种交易统计，不是K线周线，也不是库存周报。使用返回的week_date筛选日期，保留原始week，例如20199；查询用覆盖日期范围的周编号年份并适当扩边，不从周编号猜测周日期。支持最多20年区间，覆盖2010年3月以来的请求。

实测上游按周编号字符串比较，20199排序晚于201953；采集分块同时覆盖补零与未补零键，并以week_date过滤结果，避免旧年份前九周漏取。

周报成交量、累计成交量与持仓量为手；amount与cumamt从亿元精确转换成元，original_amount与original_cumamt保存原始亿元。同比/环比保留原始百分数，主力收盘价保留报价单位；保留官方拼写amout_yoy。转换版本为tushare-weekly-detail-v1，查询、文件及核验使用同口径。数据来源：中国证监会，Tushare社区成员CE规划采集。

`holding`继续使用fut_holding及2000行限制，能源中心INE的持仓排名按官方SHFE入口获取并保留SHFE来源交易所，未上榜指标为NULL，不伪造名次。没有重复建立排名模块。

## 存储与状态

schema8在原pfor_qmt schema增加futures_settlements和futures_weekly_details，不改写旧表。结算按来源、合约、交易日去重；周报按来源、交易所、品种、周日期去重。重复回补更新同一条记录，不重复计数；空响应保留旧数据。后台任务、检查点、事件和导出表复用原系统。

周报不逐日产生“缺资料”记录；发布范围或节假日口径尚无充分依据时，已入库记录仍可查询，但覆盖状态为WEEKLY_COVERAGE_UNVERIFIED。返回空表明确为未取得资料，不能凭600/2000积分断言所选区间一定有数据。实际账号探测结果以验证记录为准。

## SDK 示例

```python
client.sync_futures('settle', '2026-09-17', '2026-09-17',
                    exchange='DCE', code='A2611.DCE', account_id='main')
client.sync_futures('weekly_detail', '2020-01-01', '2020-01-31',
                    exchange='SHFE', symbol='CU', account_id='main')
rows = client.futures_records('weekly_detail', '2020-01-01', '2020-01-31',
                              exchange='SHFE', symbol='CU')
client.export_futures('settle', '2026-09-17', '2026-09-17',
                      exchange='DCE', code='A2611.DCE', format='parquet')
```

多选继续使用query_futures、sync_futures_batch、export_futures_batch和selections数组；API仍为/api/v1/futures/records、sync、export。账号检测增加settle和weekly_detail两项，空数据、权限不足和可用分别显示。

依据：[结算参数](https://tushare.pro/document/2?doc_id=141)、[持仓排名](https://tushare.pro/document/2?doc_id=139)、[交易周报](https://tushare.pro/document/2?doc_id=216)。
