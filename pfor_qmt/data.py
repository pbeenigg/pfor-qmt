import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .symbols import normalize_code, derivative_kind

SHANGHAI = ZoneInfo('Asia/Shanghai')
FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount', 'open_interest', 'settlement', 'previous_settlement')


def codes(value):
    if isinstance(value, str):
        pieces = re.split(r'[,，;\r\n]+', value.strip())
        value = []
        for piece in pieces:
            try:
                value.append(normalize_code(piece))
            except ValueError:
                value.extend(piece.split())
    if not isinstance(value, (list, tuple)):
        raise ValueError('证券代码必须是代码数组或逗号分隔文本')
    result = list(dict.fromkeys(normalize_code(item) for item in value))
    if not result or len(result) > 10000:
        raise ValueError('一次选择 1 至 10000 个证券或合约')
    return result


def periods(value):
    result = list(dict.fromkeys(value))
    if not result or set(result) - {'1d', '1m', '5m'}:
        raise ValueError('周期仅支持 1d、1m、5m')
    return result


def day(value):
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def timestamp(value):
    text = str(value)
    if re.fullmatch(r'\d{8}|\d{14}', text):
        return datetime.strptime(text, '%Y%m%d' if len(text) == 8 else '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
    if re.fullmatch(r'\d{13}', text):
        return datetime.fromtimestamp(int(text) / 1000, SHANGHAI)
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI) if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    parsed = datetime.fromisoformat(text)
    return parsed.astimezone(SHANGHAI) if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


def number(value):
    if value is None or str(value) in ('nan', 'NaN', '<NA>', 'NaT', ''):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('行情包含无效数值') from error
    if not result.is_finite():
        raise ValueError('行情包含无限数值')
    return result


def normalize_bars(frame, code, period, start, end):
    if frame is None:
        return []
    if hasattr(frame, 'to_dict'):
        records = frame.to_dict('records')
        indices = list(frame.index)
    elif isinstance(frame, list):
        records, indices = frame, [None] * len(frame)
    else:
        raise ValueError('历史行情必须是 DataFrame 或记录数组')
    result = {}
    for record, index in zip(records, indices):
        when = timestamp(record.get('time', index))
        raw_day = record.get('tradingDay', record.get('tradingDate', record.get('trading_day')))
        if isinstance(raw_day, float) and math.isfinite(raw_day) and raw_day.is_integer():
            raw_day = int(raw_day)
        trading_day = timestamp(raw_day).date() if str(raw_day) not in ('None', '', '0', 'nan', 'NaN', 'NaT', '<NA>') else when.date() if period == '1d' or not derivative_kind(code) else None
        if not start <= (trading_day or when.date()) <= end:
            continue
        if period == '1d':
            when = datetime.combine(trading_day, datetime.min.time(), SHANGHAI)
        aliases = {'open_interest': 'openInterest', 'settlement': 'settlementPrice', 'previous_settlement': 'preSettlementPrice'}
        values = {field: number(record.get(field, record.get(aliases.get(field)))) for field in FIELDS}
        if values['settlement'] is None and 'settle' in record:
            values['settlement'] = number(record['settle'])
        if all(values[key] is None for key in ('open','high','low','close')):
            raise ValueError('行情缺少全部 OHLC 字段')
        if values['high'] is not None and values['low'] is not None and values['high'] < values['low']:
            raise ValueError('行情最高价低于最低价')
        if any(values[key] is not None and values[key] < 0 for key in ('volume','amount')):
            raise ValueError('成交量或成交额为负')
        result[when] = dict(code=code, period=period, time=when, trading_day=trading_day, **values)
    return [result[key] for key in sorted(result)]


def chunks(payload):
    today = datetime.now(SHANGHAI).date()
    end = day(payload.get('end') or today)
    result = []
    for code in codes(payload['members']):
        for period in periods(payload.get('periods', ['1d'])):
            start = day(payload.get('start') or end - timedelta(days=365 if period == '1d' else 90))
            if start > end or end > today:
                raise ValueError('日期范围无效或包含未来日期')
            while start <= end:
                last = min(end, start + timedelta(days=364 if period == '1d' else 6))
                result.append({'code': code, 'period': period, 'start': start.isoformat(), 'end': last.isoformat()})
                start = last + timedelta(days=1)
    return result


def json_default(value):
    if isinstance(value, (Decimal, date, datetime)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    import uuid
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(type(value).__name__)
