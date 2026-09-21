"""Explicit capability boundary shared by SDK, hub and QMT."""
import re
from .symbols import normalize_code, MARKETS

PERIODS = ("1d", "1m", "5m")
ACTIONS = frozenset("xtdata." + method for method in (
    "get_full_tick", "get_market_data_ex", "get_local_data", "get_instrument_detail",
    "get_sector_list", "get_stock_list_in_sector", "get_trading_dates", "get_divid_factors",
    "download_history_data", "download_history_data2", "subscribe_quote", "subscribe_whole_quote",
    "unsubscribe_quote",
    "get_instrument_details", "get_sector_tree", "get_option_detail_data",
    "get_main_contract", "get_trading_calendar", "get_main_contract_history", "download_main_contract_history",
)) | frozenset(("pfor.ping", "pfor.status"))


def validate(action, params=None):
    if action not in ACTIONS:
        raise ValueError("Unsupported market action: %s" % action)
    params = dict(params or {})
    if action in ('xtdata.get_main_contract','xtdata.get_main_contract_history','xtdata.download_main_contract_history'):
        from .symbols import FUTURE_MARKETS
        code=normalize_code(params.get('stock_code',''))
        if code.rsplit('.',1)[1] not in FUTURE_MARKETS or not re.fullmatch(r'[A-Za-z]+00\.[A-Z]+',code):
            raise ValueError('请选择品种00形式的QMT主力连续合约')
        params['stock_code']=code
    if action=='xtdata.get_trading_calendar':
        from .symbols import FUTURE_MARKETS
        if params.get('market') not in FUTURE_MARKETS: raise ValueError('无效期货交易所')
    if action in ('xtdata.get_trading_calendar','xtdata.get_main_contract_history','xtdata.download_main_contract_history'):
        from datetime import datetime
        values=[]
        for name in ('start_time','end_time'):
            value=params.get(name)
            if not isinstance(value,str) or not re.fullmatch(r'\d{8}(?:\d{6})?',value): raise ValueError('资料日期需为YYYYMMDD或YYYYMMDDHHMMSS')
            values.append(datetime.strptime(value,'%Y%m%d' if len(value)==8 else '%Y%m%d%H%M%S'))
        if values[0]>values[1] or (values[1]-values[0]).days>3660: raise ValueError('资料范围需为有效的十年内区间')
    period = params.get("period")
    allowed = PERIODS + (("tick",) if action == "xtdata.subscribe_quote" else ())
    if period is not None and period not in allowed:
        raise ValueError("Unsupported period: %s" % period)
    if params.get("dividend_type", "none") != "none" or params.get("divid_type", "none") != "none":
        raise ValueError("Only unadjusted market data is supported")
    if params.get("real_timetag", -1) != -1:
        raise ValueError("Only current index constituents are supported")
    for key in ('stock_code', 'stockcode', 'stock_list', 'code_list'):
        value = params.get(key)
        if not value:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        normalized = [code if action == 'xtdata.subscribe_whole_quote' and code in MARKETS else normalize_code(code) for code in values]
        params[key] = normalized if isinstance(value, (list, tuple)) else normalized[0]
    if action == 'xtdata.get_instrument_details' and (not isinstance(params.get('stock_list'), (list, tuple)) or not 1 <= len(params['stock_list']) <= 100):
        raise ValueError('Instrument detail batches require 1 to 100 codes')
    return params
