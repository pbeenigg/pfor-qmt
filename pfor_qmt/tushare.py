"""Tushare's documented HTTP protocol and futures-only adapter."""
import hashlib
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .data import SHANGHAI, number, normalize_bars, day, timestamp, period_label, MINUTE_PERIODS, AGGREGATE_PERIODS
from .identifiers import TS_EXCHANGES, source_code, source_market


class SourceError(ValueError):
    def __init__(self, message, category='source'):
        super().__init__(message)
        self.category = category


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SourceError('Tushare端点发生重定向，请直接配置最终API地址', 'configuration')


class RateLimiter:
    _lock = threading.Lock()
    _buckets = {}

    @classmethod
    def acquire(cls, token, limit, check):
        key = hashlib.sha256(token.encode()).digest()
        while True:
            check()
            with cls._lock:
                bucket = cls._buckets.setdefault(key, {'calls': deque(), 'limit': limit})
                bucket['limit'] = min(bucket['limit'], limit)
                now = time.monotonic()
                while bucket['calls'] and bucket['calls'][0] <= now - 60:
                    bucket['calls'].popleft()
                if len(bucket['calls']) < bucket['limit']:
                    bucket['calls'].append(now)
                    return
            time.sleep(0.1)


class TushareSource:
    provider = 'tushare'

    def __init__(self, account, check=None):
        self.account = dict(account)
        self.check = check or (lambda: None)

    def request(self, api, params, fields=''):
        self.check()
        RateLimiter.acquire(self.account['token'], self.account['requests_per_minute'], self.check)
        body = json.dumps({'api_name': api, 'token': self.account['token'], 'params': params, 'fields': fields}).encode()
        request = Request(self.account['endpoint'], data=body, headers={'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.account['timeout']) as response:
                result = json.load(response, parse_float=Decimal)
        except HTTPError as error:
            if error.code == 429:
                raise SourceError('Tushare请求达到限额，请降低频率后重试', 'rate_limit') from None
            if error.code >= 500:
                raise ConnectionError('Tushare服务暂不可用') from None
            raise SourceError('Tushare HTTP请求失败，请检查端点与认证', 'authentication') from None
        except (URLError, TimeoutError, OSError):
            raise ConnectionError('Tushare网络连接失败') from None
        except (ValueError, UnicodeError):
            raise SourceError('Tushare响应不是有效JSON', 'invalid_response') from None
        self.check()
        if not isinstance(result, dict):
            raise SourceError('Tushare返回格式无效', 'invalid_response')
        if result.get('code') != 0:
            message = str(result.get('msg', ''))
            category = 'rate_limit' if any(word in message for word in ('频率', '每分钟', '每小时', '每天', '访问次数')) else 'permission' if result.get('code') == 2002 or '权限' in message or '积分' in message else 'authentication' if 'token' in message.lower() else 'source'
            labels = {'rate_limit': '请求达到限额，请降低频率后重试', 'permission': '接口权限不足，请核对账号权限', 'authentication': '认证失败，请检查Token', 'source': '接口请求失败，请核对参数和端点'}
            raise SourceError(api + ': ' + labels[category], category)
        data = result.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('fields'), list) or not isinstance(data.get('items'), list):
            raise SourceError(api + ': 响应缺少字段或记录数组', 'invalid_response')
        columns = data['fields']
        if any(not isinstance(key, str) for key in columns) or len(set(columns)) != len(columns):
            raise SourceError(api + ': 响应字段重复或无效', 'invalid_response')
        rows = []
        for row in data['items']:
            if not isinstance(row, list) or len(row) != len(columns):
                raise SourceError(api + ': 响应记录与字段不匹配', 'invalid_response')
            rows.append(dict(zip(columns, row)))
        return rows

    def catalog(self, exchange, contract_type):
        rows = self.request('fut_basic', {'exchange': exchange, 'fut_type': contract_type},
                            'ts_code,symbol,exchange,name,fut_code,multiplier,trade_unit,per_unit,quote_unit,quote_unit_desc,list_date,delist_date,d_month,trade_time_desc')
        if not rows or len(rows) >= 10000:
            raise SourceError('合约目录为空或达到10000条上限，不能确认完整性', 'incomplete')
        result = []
        for position, row in enumerate(rows, 1):
            try:
                code = source_code(row.get('ts_code'), 'tushare')
            except ValueError:
                raise SourceError(f'{exchange} / 类型 {contract_type} / 第 {position} 条合约代码无效', 'invalid_response') from None
            if source_market(code, 'tushare') != TS_EXCHANGES[exchange] or row.get('exchange') != exchange or not row.get('name'):
                raise SourceError('合约资料的代码、市场或名称不完整', 'invalid_response')
            month = str(row.get('d_month') or '')
            metadata = {'product': row.get('fut_code'), 'delivery_month': month if len(month) == 6 and month.isdigit() else None,
                        'listed': row.get('list_date'), 'expiry': row.get('delist_date'),
                        'multiplier': row.get('multiplier'), 'per_unit': row.get('per_unit'),
                        'quote_unit': row.get('quote_unit'), 'trade_unit': row.get('trade_unit'),
                        'trade_time_desc': row.get('trade_time_desc')}
            result.append(dict(code=code, name=row['name'], kind='future', subtype='contract' if contract_type == '1' else 'continuous', detail=row, metadata=metadata))
        return result

    def calendar(self, market, start, end):
        exchange = next(key for key, value in TS_EXCHANGES.items() if value == market)
        return [row['day'] for row in self.calendar_records(exchange,start,end) if row['is_open']]

    def calendar_records(self, exchange, start, end):
        rows = self.request('fut_trade_cal', {'exchange': exchange, 'start_date': day(start).strftime('%Y%m%d'), 'end_date': day(end).strftime('%Y%m%d')}, 'exchange,cal_date,is_open,pretrade_date')
        if not rows or any(row.get('exchange') != exchange for row in rows):
            raise SourceError(exchange + ' 期货日历为空或市场不匹配', 'incomplete')
        values = {datetime.strptime(str(row['cal_date']), '%Y%m%d').date(): row for row in rows}
        if len(values) != len(rows):
            raise SourceError(exchange + ' 期货日历包含重复日期', 'invalid_response')
        current = day(start)
        while current <= day(end):
            if current not in values or values[current]['is_open'] not in (0, 1):
                raise SourceError(exchange + ' 期货日历覆盖不完整', 'incomplete')
            current += timedelta(days=1)
        return [dict(source='tushare',market=TS_EXCHANGES[exchange],day=date,is_open=bool(row['is_open']),
                     pretrade_date=timestamp(row['pretrade_date']).date() if row.get('pretrade_date') else None)
                for date,row in sorted(values.items()) if day(start) <= date <= day(end)]

    def bounded(self, api, code, start, end, period=None, extra=None):
        minute = api == 'ft_mins'
        params = {**(extra or {}), 'start_date': start.strftime('%Y-%m-%d %H:%M:%S' if minute else '%Y%m%d'),
                  'end_date': end.strftime('%Y-%m-%d %H:%M:%S' if minute else '%Y%m%d')}
        if code:
            params['ts_code'] = code
        if minute:
            params['freq'] = period[:-1] + 'min'
        if api == 'fut_weekly_monthly':
            params['freq'] = {'1w':'week','1mo':'month'}[period]
        fields = ''
        if api in ('fut_wsr','fut_holding'):
            from .futures import REPORTS
            fields = ','.join(key for key in REPORTS['warehouse' if api=='fut_wsr' else 'holding']['fields'] if key != 'source')
        rows = self.request(api, params, fields)
        cap = {'ft_mins':8000,'fut_weekly_monthly':6000,'fut_wsr':1000}.get(api,2000)
        if len(rows) < cap:
            return rows
        step = timedelta(seconds=1) if minute else timedelta(days=1)
        if end - start < step:
            raise SourceError(api + ': 最小区间仍达到返回上限', 'incomplete')
        middle = start + ((end - start) // step // 2) * step
        return self.bounded(api, code, start, middle, period, extra) + self.bounded(api, code, middle + step, end, period, extra)

    def history(self, code, period, start, end, subtype='contract'):
        if period in MINUTE_PERIODS and subtype != 'contract':
            raise SourceError('连续合约分钟线尚未接入，请选择具体月份合约', 'unsupported')
        aggregate = period in AGGREGATE_PERIODS
        start, end = period_label(start,period), period_label(end,period)
        first = datetime.combine(start, datetime.min.time(), SHANGHAI)
        last = datetime.combine(end, datetime.max.time().replace(microsecond=0), SHANGHAI)
        api = 'fut_weekly_monthly' if aggregate else 'fut_daily' if period == '1d' else 'ft_mins'
        rows = self.bounded(api, code, first, last, period)
        records = []
        summaries = {}
        for row in rows:
            if row.get('ts_code') != code:
                raise SourceError('历史行情合约代码与请求不一致', 'invalid_response')
            amount = number(row.get('amount'))
            if amount is not None and period not in MINUTE_PERIODS:
                sign, digits, exponent = amount.as_tuple()
                amount = Decimal((sign, digits, exponent + 4))
            if aggregate:
                if row.get('freq') != {'1w':'week','1mo':'month'}[period] or not row.get('end_date'):
                    raise SourceError('周/月线缺少正确周期或计算截至日期', 'invalid_response')
                label = timestamp(row['trade_date'])
                as_of = timestamp(row['end_date']).date()
                if as_of > datetime.now(SHANGHAI).date():
                    raise SourceError('周/月线计算截至日期位于未来', 'invalid_response')
                previous = summaries.get(label)
                if previous and previous['as_of_date'] >= as_of:
                    continue
                summaries[label] = dict(as_of_date=as_of,source_fields=row)
            records.append(dict(time=row.get('trade_date') if period not in MINUTE_PERIODS else row.get('trade_time'),
                                trading_day=row.get('trade_date') if period == '1d' else None,
                                **{key: row.get(key) for key in ('open', 'high', 'low', 'close')},
                                volume=row.get('vol'), amount=amount,
                                open_interest=row.get('oi'), settlement=row.get('settle'), previous_settlement=row.get('pre_settle')))
        normalized = normalize_bars(records, code, period, start, end, 'tushare')
        if aggregate:
            for row in normalized:
                row.update(summaries[row['time']])
        return normalized

    def report(self, payload, start, end):
        from .futures import normalize_report
        resource = payload['resource']
        if resource == 'calendar':
            return self.calendar_records(payload['exchange'],start,end)
        if resource == 'mapping':
            return [dict(source='tushare',code=row['ts_code'],trading_day=row['trade_date'],member_code=row['mapping_ts_code'])
                    for row in self.mapping(payload['code'],start,end)]
        exchange = 'SHFE' if resource == 'holding' and payload['exchange']=='INE' else payload['exchange']
        records = self.bounded('fut_wsr' if resource=='warehouse' else 'fut_holding',None,
                               datetime.combine(day(start),datetime.min.time()),datetime.combine(day(end),datetime.min.time()),
                               extra=dict(exchange=exchange,symbol=payload['symbol']))
        return normalize_report(resource,records,exchange,payload['symbol'],day(start),day(end))

    def mapping(self, code, start, end):
        first = datetime.combine(day(start), datetime.min.time())
        last = datetime.combine(day(end), datetime.min.time())
        rows = self.bounded('fut_mapping', code, first, last)
        unique = {}
        for row in rows:
            if row.get('ts_code') != code:
                raise SourceError('主力映射合约与请求不一致', 'invalid_response')
            row['mapping_ts_code'] = source_code(row['mapping_ts_code'], 'tushare')
            row['trade_date'] = datetime.strptime(str(row['trade_date']), '%Y%m%d').date()
            if source_market(row['mapping_ts_code'],'tushare') != source_market(code,'tushare') or not day(start) <= row['trade_date'] <= day(end):
                raise SourceError('主力映射返回跨市场或范围外记录','invalid_response')
            if row['trade_date'] in unique and unique[row['trade_date']]['mapping_ts_code'] != row['mapping_ts_code']:
                raise SourceError('同一交易日出现冲突的主力映射','invalid_response')
            unique[row['trade_date']] = row
        return [unique[date] for date in sorted(unique)]

    def probe(self):
        today = datetime.now(SHANGHAI).date()
        states = {}
        sample = None
        operations = {
            'catalog': lambda: self.catalog('SHFE', '1'),
            'calendar': lambda: self.calendar('SF', today - timedelta(days=14), today),
        }
        for name, operation in operations.items():
            try:
                rows = operation()
                states[name] = {'state': 'available', 'rows': len(rows)}
                if name == 'catalog':
                    sample = next((r for r in rows if str(r['metadata']['expiry'] or '') >= today.strftime('%Y%m%d') and str(r['metadata']['listed'] or '') < (today - timedelta(days=14)).strftime('%Y%m%d')), None)
            except (SourceError, ConnectionError) as error:
                states[name] = {'state': getattr(error, 'category', 'network'), 'message': str(error)}
        for name, api, period in [('daily', 'fut_daily', '1d'), ('minutes', 'ft_mins', '1m'), ('mapping', 'fut_mapping', None)]:
            try:
                if not sample:
                    states[name] = {'state': 'unverified', 'message': '没有有效月份合约样本'}
                    continue
                code = sample['code'] if name != 'mapping' else 'CU.SHF'
                first = datetime.combine(today - timedelta(days=14), datetime.min.time())
                last = datetime.combine(today, datetime.min.time())
                rows = self.bounded(api, code, first, last, period)
                states[name] = {'state': 'available' if rows else 'empty', 'rows': len(rows)}
            except (SourceError, ConnectionError) as error:
                states[name] = {'state': getattr(error, 'category', 'network'), 'message': str(error)}
        for name, operation in {
            'weekly': lambda: self.history(sample['code'],'1w',today-timedelta(days=35),today),
            'monthly': lambda: self.history(sample['code'],'1mo',today-timedelta(days=65),today),
            'warehouse': lambda: self.report(dict(resource='warehouse',exchange='SHFE',symbol='CU'),today-timedelta(days=7),today),
            'holding': lambda: self.report(dict(resource='holding',exchange='SHFE',symbol='CU'),today-timedelta(days=7),today),
        }.items():
            try:
                if name in ('weekly','monthly') and not sample:
                    states[name] = {'state':'unverified','message':'没有有效合约样本'}
                    continue
                rows = operation()
                states[name] = {'state':'available' if rows else 'empty','rows':len(rows)}
            except (ValueError,ConnectionError) as error:
                states[name] = {'state':getattr(error,'category','network' if isinstance(error,ConnectionError) else 'invalid_response'),'message':str(error)}
        states['tick'] = {'state':'unsupported','message':'官方无API，需单独获取CSV文件；不属于积分接口'}
        return {'provider': 'tushare', 'account_id': self.account['id'], 'capabilities': states,
                'realtime': False, 'continuous_minutes': False}
