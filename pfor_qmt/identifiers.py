"""Provider identifiers and evidence-based futures identities."""
import re

from .symbols import normalize_code, market_of

TS_MARKETS = {'CFX': 'IF', 'SHF': 'SF', 'DCE': 'DF', 'ZCE': 'ZF', 'INE': 'INE', 'GFE': 'GF'}
TS_EXCHANGES = {'CFFEX': 'IF', 'SHFE': 'SF', 'DCE': 'DF', 'CZCE': 'ZF', 'INE': 'INE', 'GFEX': 'GF'}


def provider_name(source):
    if source not in ('qmt', 'tushare'):
        raise ValueError('不支持的数据来源')
    return source


def source_code(value, source='qmt'):
    provider_name(source)
    if source == 'qmt':
        return normalize_code(value)
    if not isinstance(value, str):
        raise ValueError('无效Tushare合约代码')
    value = value.strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,30}\.(CFX|SHF|DCE|ZCE|INE|GFE)', value):
        raise ValueError('无效Tushare期货合约代码')
    return value


def source_market(code, source='qmt'):
    return TS_MARKETS[source_code(code, source).rsplit('.', 1)[1]] if source == 'tushare' else market_of(code)


def identity_key(code, source, kind, subtype, metadata):
    # Only month contracts with a full documented month may share an identity.
    product = str(metadata.get('product') or '').upper()
    month = str(metadata.get('delivery_month') or '')
    if kind == 'future' and subtype in ('', 'contract') and product and re.fullmatch(r'\d{6}', month):
        return 'future:' + source_market(code, source) + ':' + product + ':' + month
    return source + ':' + code
