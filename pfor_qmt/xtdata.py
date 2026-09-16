"""Selected xtdata-style entrypoints, scoped to unadjusted market data."""
from .client import get_client, configure
from .protocol import new_id

_subscriptions = {}


def configure_from_file(config_path=None):
    """Load the host's central TOML configuration for a standalone SDK process."""
    from .settings import Settings
    configure(**Settings(config_path=config_path).pipe)


def get_full_tick(code_list):
    return get_client().request("xtdata.get_full_tick", {"code_list": list(code_list)})


def get_market_data_ex(field_list=None, stock_list=None, period="1d", start_time="", end_time="", count=-1, dividend_type="none", fill_data=False):
    return get_client().request("xtdata.get_market_data_ex", dict(field_list=field_list or [], stock_list=stock_list or [], period=period, start_time=start_time, end_time=end_time, count=count, dividend_type=dividend_type, fill_data=fill_data))


def get_local_data(field_list=None, stock_list=None, period="1d", start_time="", end_time="", count=-1):
    return get_client().request("xtdata.get_local_data", dict(field_list=field_list or [], stock_list=stock_list or [], period=period, start_time=start_time, end_time=end_time, count=count, dividend_type="none", fill_data=False))


def get_instrument_detail(stock_code, iscomplete=False):
    return get_client().request("xtdata.get_instrument_detail", dict(stock_code=stock_code, iscomplete=iscomplete))


def get_sector_list():
    return get_client().request("xtdata.get_sector_list")


def get_stock_list_in_sector(sector_name, real_timetag=-1):
    return get_client().request("xtdata.get_stock_list_in_sector", dict(sector_name=sector_name, real_timetag=real_timetag))


def get_trading_dates(stockcode="000001.SH", start_date="", end_date="", count=-1, period="1d"):
    return get_client().request("xtdata.get_trading_dates", dict(stockcode=stockcode, start_date=start_date, end_date=end_date, count=count, period=period))


def get_divid_factors(stock_code):
    return get_client().request("xtdata.get_divid_factors", dict(stock_code=stock_code))


def download_history_data(stock_code, period, start_time="", end_time="", incrementally=None):
    return get_client().request("xtdata.download_history_data", dict(stock_code=stock_code, period=period, start_time=start_time, end_time=end_time, incrementally=incrementally), timeout=180)


def download_history_data2(stock_list, period, start_time="", end_time="", callback=None, incrementally=None):
    client = get_client()
    event = "download:" + new_id("event")
    if callback:
        client.add_callback(event, callback)
    try:
        return client.request("xtdata.download_history_data2", dict(stock_list=list(stock_list), period=period, start_time=start_time, end_time=end_time, incrementally=incrementally, callback_event=event if callback else None), timeout=180)
    finally:
        if callback:
            client.remove_callback(event, callback)


def _subscribe(action, params, callback):
    if callback is not None and not callable(callback):
        raise TypeError("callback must be callable")
    client = get_client()
    event = "quote:" + new_id("subscription")
    params["callback_event"] = event
    if callback:
        client.add_callback(event, callback)
    try:
        result = client.request(action, params)
        seq = result["subscribe_id"]
        _subscriptions[seq] = (client, event, callback)
        return seq
    except Exception:
        if callback:
            client.remove_callback(event, callback)
        raise


def subscribe_quote(stock_code, period="1d", start_time="", end_time="", count=0, callback=None):
    return _subscribe("xtdata.subscribe_quote", dict(stock_code=stock_code, period=period, start_time=start_time, end_time=end_time, count=count), callback)


def subscribe_whole_quote(code_list, callback=None):
    return _subscribe("xtdata.subscribe_whole_quote", dict(code_list=list(code_list)), callback)


def unsubscribe_quote(seq):
    item = _subscriptions.get(seq)
    client = item[0] if item else get_client()
    result = client.request("xtdata.unsubscribe_quote", {"subscribe_id": seq})
    if result is False or isinstance(result, (int, float)) and result < 0:
        return result
    if item:
        if item[2]:
            client.remove_callback(item[1], item[2])
        del _subscriptions[seq]
    return result
