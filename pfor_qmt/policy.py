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
)) | frozenset(("pfor.ping", "pfor.status"))


def validate(action, params=None):
    if action not in ACTIONS:
        raise ValueError("Unsupported market action: %s" % action)
    params = dict(params or {})
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
