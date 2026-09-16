import ast
import importlib.util
import queue
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from pfor_qmt import protocol, xtdata
from pfor_qmt.client import MarketClient, CfquantError
from pfor_qmt.config import get_config
from pfor_qmt.deploy import EMBEDDED
from pfor_qmt.hub import MarketHub
from pfor_qmt.market_bridge import MarketBridge
from pfor_qmt.qmt_methods import QmtMethods


class Transport:
    def __init__(self):
        self.Q, self.messages = queue.Queue(), []
        self.connection_generation = 1

    def push(self, kind, raw, client):
        self.messages.append(protocol.loads_message(raw))
        return {'code': 0}

    def close(self):
        pass


@pytest.mark.parametrize('action', ['xttrader.order_stock','xttrader.query_stock_asset','xtdata.get_financial_data','xtdata.subscribe_l2thousand','eval'])
def test_forbidden_at_sdk_hub_and_bridge(action):
    with pytest.raises(ValueError):
        MarketClient().request(action)
    with pytest.raises(ValueError):
        MarketBridge(object(), tx=Transport()).dispatch(action)
    hub = MarketHub(show=False)
    errors = []
    hub._send_error = lambda conn, request, message: errors.append(message)
    hub._handle_api_request(object(), {'payload':protocol.pack_request(action, request_id='reject')})
    assert errors and not hub.pending


@pytest.mark.parametrize('params', [{'period':'l2quote'}, {'period':'tick'}, {'dividend_type':'front'}, {'real_timetag':123}])
def test_restricted_parameters(params):
    with pytest.raises(ValueError):
        MarketBridge(object(), tx=Transport()).dispatch('xtdata.get_market_data_ex',params)


def test_local_data_disables_subscription_and_fill():
    calls = []
    bridge = MarketBridge(SimpleNamespace(get_market_data_ex=lambda *args: calls.append(args) or {'ok':True}), tx=Transport())
    assert bridge.dispatch('xtdata.get_local_data', {'stock_list':['000300.SH'],'period':'1d','fill_data':False}) == {'ok':True}
    assert calls[0][-3:] == ('none',False,False)


@pytest.mark.parametrize('method,params', [
    ('_get_market_data_ex',{'stock_list':['000300.SH'],'period':'1m','count':12,'fill_data':False}),
    ('_get_local_data',{'stock_list':['000300.SH','000001.SZ'],'period':'5m','fill_data':False}),
    ('_get_stock_list_in_sector',{'sector_name':'offline-index','real_timetag':-1}),
])
def test_same_qmt_inputs_match_extracted_upstream_methods(method,params):
    calls = []
    context = SimpleNamespace(get_market_data_ex=lambda *args: calls.append(args) or {'answer':42},
                              get_stock_list_in_sector=lambda *args: calls.append(args) or ['000001.SZ'])
    upstream = QmtMethods()
    upstream.context, upstream.globals_dict = context, {}
    expected = getattr(upstream,method)(params)
    expected_calls = list(calls)
    calls.clear()
    bridge = MarketBridge(context,tx=Transport())
    actual = getattr(bridge,method)(params)
    assert expected == actual and calls == expected_calls


def test_old_download_fallback_covers_all_codes():
    calls = []
    bridge = MarketBridge(SimpleNamespace(down_history_data=lambda *args: calls.append(args)), tx=Transport())
    bridge.dispatch('xtdata.download_history_data2', {'stock_list':['000300.SH','000001.SZ','510300.SH'],'period':'1m'})
    assert [args[0] for args in calls] == ['000300.SH','000001.SZ','510300.SH']


def test_internal_typeerror_does_not_fall_back():
    calls = []
    def modern(stocks, period, start, end, callback):
        raise TypeError('inside QMT')
    bridge = MarketBridge(SimpleNamespace(download_history_data2=modern, down_history_data=lambda *args: calls.append(args)),tx=Transport())
    with pytest.raises(TypeError, match='inside QMT'):
        bridge.dispatch('xtdata.download_history_data2', {'stock_list':['000300.SH'],'period':'1d'})
    assert not calls


def test_subscription_first_packet_ownership_unsubscribe_and_reconnect():
    tx = Transport()
    released = []
    callbacks = []
    def subscribe(code, period, adjustment, result_type, callback):
        assert (adjustment,result_type) == ('none','dict')
        callbacks.append(callback)
        callback({code:[{'time':123,'lastPrice':2}]})
        return 18
    bridge = MarketBridge(SimpleNamespace(subscribe_quote=subscribe, unsubscribe_quote=lambda native: released.append(native)),tx=tx)
    bridge.pump()
    result = bridge.dispatch('xtdata.subscribe_quote', {'stock_code':'000300.SH','period':'tick','callback_event':'first'}, {'client_id':'client-a'})
    assert tx.messages[0]['event'] == 'first'
    with pytest.raises(ValueError, match='another client'):
        bridge.dispatch('xtdata.unsubscribe_quote', {'subscribe_id':result['subscribe_id']}, {'client_id':'client-b'})
    tx.connection_generation = 2
    bridge.pump()
    callbacks[0]({'000300.SH':[{'lastPrice':3}]})
    assert released == [18] and len(tx.messages) == 1 and not bridge.subscriptions


def test_failed_unsubscribe_keeps_subscription():
    bridge = MarketBridge(SimpleNamespace(subscribe_whole_quote=lambda codes, callback: 7, unsubscribe_quote=lambda seq: -1), tx=Transport())
    result = bridge.dispatch('xtdata.subscribe_whole_quote', {'code_list':['SH']}, {'client_id':'a'})
    with pytest.raises(RuntimeError):
        bridge._unsubscribe(result['subscribe_id'],'a')
    assert result['subscribe_id'] in bridge.subscriptions


def test_sdk_registers_callback_before_request(monkeypatch):
    callbacks = {}
    removed = []
    def request(action, params, **kwargs):
        if action == 'xtdata.subscribe_quote':
            callbacks[params['callback_event']]({'first':1})
            return {'subscribe_id':'a'}
        return True
    client = SimpleNamespace(request=request, add_callback=lambda key, cb: callbacks.update({key:cb}), remove_callback=lambda key, cb: removed.append(key))
    monkeypatch.setattr(xtdata,'get_client',lambda: client)
    received = []
    seq = xtdata.subscribe_quote('000300.SH',callback=received.append)
    assert received == [{'first':1}]
    assert xtdata.unsubscribe_quote(seq) and removed


def test_protocol_matches_pinned_reference():
    path = Path(__file__).parent / 'reference' / 'protocol.py'
    spec = importlib.util.spec_from_file_location('upstream_protocol',path)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    frame = pd.DataFrame({'close':[12.34,None], 'volume':[2**60+37,23]}, index=['20260910','20260911'])
    assert reference.encode_value(frame) == protocol.encode_value(frame)
    for value in [{'data':frame}, [None,2.3,True], {'code':'000300.SH'}]:
        assert reference.encode_value(value) == protocol.encode_value(value)
    args = dict(action='xtdata.get_local_data',params={'stock_list':['000300.SH'],'period':'1d'},request_id='fixed',client_id='test')
    left, right = reference.loads_message(reference.pack_request(**args)), protocol.loads_message(protocol.pack_request(**args))
    left.pop('ts'); right.pop('ts')
    assert left == right


def test_embedded_python36_and_isolated_names():
    root = Path(__file__).resolve().parents[1]
    for name in EMBEDDED:
        ast.parse((root / 'pfor_qmt' / name).read_text('utf-8'), feature_version=(3,6))
    ast.parse((root / 'qmt_scripts' / 'PFOR_MARKET.py').read_text('gbk'), feature_version=(3,6))
    assert 'pfor_qmt' in get_config()['pipe_name']
    assert get_config()['request_channel'] == 'pfor_qmt.market.request'
    assert not (root / 'pfor_qmt' / 'xttrader.py').exists()


def test_terminal_timer_accepts_no_arguments_and_cancels_native_key(monkeypatch):
    import qmt_scripts.PFOR_MARKET as entry
    calls = []
    fake = SimpleNamespace(context=None,start=lambda:fake,pump=lambda:calls.append('pump'),close=lambda:calls.append('close'))
    monkeypatch.setattr(entry,'MarketBridge',lambda *args:fake)
    def schedule(callback,*args,**kwargs):
        callback()
        return 'native-timer-key'
    context = SimpleNamespace(schedule_run=schedule,cancel_schedule_run=lambda key:calls.append(key))
    entry.init(context)
    entry.after_init(context)
    entry.stop(context)
    assert calls == ['pump','native-timer-key','close']


def test_hub_preserves_long_download_timeout(monkeypatch):
    import pfor_qmt.hub as module
    hub = MarketHub(show=False)
    failures = []
    hub._send_error = lambda conn,request,message:failures.append(request)
    monkeypatch.setattr(module.time,'perf_counter',lambda:100)
    hub.pending = {'download':{'api_received_at':1,'timeout_seconds':180},'short':{'api_received_at':1,'timeout_seconds':15}}
    assert hub._cleanup_expired_pending() == 1
    assert failures == ['short'] and 'download' in hub.pending
