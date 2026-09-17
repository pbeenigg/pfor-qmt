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

from .data import SHANGHAI, number, normalize_bars, day
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
        for row in rows:
            code = source_code(row.get('ts_code'), 'tushare')
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
        rows = self.request('fut_trade_cal', {'exchange': exchange, 'start_date': day(start).strftime('%Y%m%d'), 'end_date': day(end).strftime('%Y%m%d')}, 'exchange,cal_date,is_open')
        if not rows or any(row.get('exchange') != exchange for row in rows):
            raise SourceError(exchange + ' 期货日历为空或市场不匹配', 'incomplete')
        values = {datetime.strptime(str(row['cal_date']), '%Y%m%d').date(): row['is_open'] for row in rows}
        current = day(start)
        while current <= day(end):
            if current not in values or values[current] not in (0, 1):
                raise SourceError(exchange + ' 期货日历覆盖不完整', 'incomplete')
            current += timedelta(days=1)
        return sorted(date for date, opened in values.items() if opened == 1 and day(start) <= date <= day(end))

    def bounded(self, api, code, start, end, period=None):
        minute = api == 'ft_mins'
        params = {'ts_code': code, 'start_date': start.strftime('%Y-%m-%d %H:%M:%S' if minute else '%Y%m%d'),
                  'end_date': end.strftime('%Y-%m-%d %H:%M:%S' if minute else '%Y%m%d')}
        if minute:
            params['freq'] = {'1m': '1min', '5m': '5min'}[period]
        rows = self.request(api, params)
        cap = 8000 if minute else 2000
        if len(rows) < cap:
            return rows
        step = timedelta(seconds=1) if minute else timedelta(days=1)
        if end - start < step:
            raise SourceError(api + ': 最小区间仍达到返回上限', 'incomplete')
        middle = start + ((end - start) // step // 2) * step
        return self.bounded(api, code, start, middle, period) + self.bounded(api, code, middle + step, end, period)

    def history(self, code, period, start, end, subtype='contract'):
        if period != '1d' and subtype != 'contract':
            raise SourceError('连续合约分钟线尚未接入，请选择具体月份合约', 'unsupported')
        first = datetime.combine(day(start), datetime.min.time(), SHANGHAI)
        last = datetime.combine(day(end), datetime.max.time().replace(microsecond=0), SHANGHAI)
        rows = self.bounded('fut_daily' if period == '1d' else 'ft_mins', code, first, last, period)
        records = []
        for row in rows:
            if row.get('ts_code') != code:
                raise SourceError('历史行情合约代码与请求不一致', 'invalid_response')
            amount = number(row.get('amount'))
            if amount is not None and period == '1d':
                sign, digits, exponent = amount.as_tuple()
                amount = Decimal((sign, digits, exponent + 4))
            records.append(dict(time=row.get('trade_date') if period == '1d' else row.get('trade_time'),
                                trading_day=row.get('trade_date') if period == '1d' else None,
                                **{key: row.get(key) for key in ('open', 'high', 'low', 'close')},
                                volume=row.get('vol'), amount=amount,
                                open_interest=row.get('oi'), settlement=row.get('settle'), previous_settlement=row.get('pre_settle')))
        return normalize_bars(records, code, period, day(start), day(end), 'tushare')

    def mapping(self, code, start, end):
        first = datetime.combine(day(start), datetime.min.time())
        last = datetime.combine(day(end), datetime.min.time())
        rows = self.bounded('fut_mapping', code, first, last)
        for row in rows:
            if row.get('ts_code') != code:
                raise SourceError('主力映射合约与请求不一致', 'invalid_response')
            row['mapping_ts_code'] = source_code(row['mapping_ts_code'], 'tushare')
            row['trade_date'] = datetime.strptime(str(row['trade_date']), '%Y%m%d').date()
        return rows

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
        return {'provider': 'tushare', 'account_id': self.account['id'], 'capabilities': states,
                'realtime': False, 'continuous_minutes': False}
