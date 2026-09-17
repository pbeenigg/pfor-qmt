import json
import queue
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from types import SimpleNamespace

import pytest

from pfor_qmt.client import MarketClient, CfquantError
from pfor_qmt.hub import MarketHub
from pfor_qmt.market_bridge import MarketBridge
from pfor_qmt.pipe_client import PipeRpcClient
from pfor_qmt.pipe_transport import PipeTxClient
from pfor_qmt.protocol import loads_message
from pfor_qmt.sdk import DataClient
from pfor_qmt.server import HTTPServer, handler_for, start_websocket
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings


@pytest.fixture
def app_server(store,tmp_path):
    app = Application(Settings(tmp_path,config_path=tmp_path / 'config.toml'),store=store)
    server = HTTPServer(('127.0.0.1',0),handler_for(app))
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    client = DataClient('http://127.0.0.1:' + str(server.server_port),app.settings.data['api_key'])
    try:
        yield app, server, client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.postgres
def test_http_auth_no_secret_disclosure_and_sdk_roundtrip(app_server):
    app, server, client = app_server
    with pytest.raises(HTTPError) as failure:
        DataClient(client.base_url).request('/status')
    assert failure.value.code == 401
    body = client.request('/settings')
    assert 'api_key' not in body and 'dsn' not in body and 'login_hash' not in body
    dataset = client.create_dataset('HTTP SDK test',['000300.SH'])
    job = client.download(dataset['id'],'2026-09-14','2026-09-14')
    assert job['state'] == 'queued' and job['payload']['members'] == ['000300.SH']
    assert client.cancel(job['id'])['state'] == 'cancelled'
    assert client.retry(job['id'])['state'] == 'queued'
    assert client.history('000300.SH')['source'] == 'postgresql'
    with pytest.raises(HTTPError) as failure:
        client.request('/xttrader/order_stock',{})
    assert failure.value.code == 404


@pytest.mark.postgres
def test_readonly_diagnostics_distinguish_bridge_and_history(app_server,monkeypatch):
    from pfor_qmt import service
    app, server, client = app_server
    monkeypatch.setattr(service,'get_client',lambda: SimpleNamespace(request=lambda *args,**kwargs: {'mode':'market-only'}))
    app.source = SimpleNamespace(get_full_tick=lambda codes:{codes[0]:{'lastPrice':10}},
                                get_local_data=lambda **kwargs:{},get_trading_dates=lambda *args:[])
    report = client.request('/source/diagnostics',{})
    assert not report['history_readable']
    assert [item['state'] for item in report['checks']] == ['ok','ok','empty','empty']
    assert not app.store.query('SELECT * FROM jobs')
    assert not app.store.query('SELECT * FROM bars')
    with pytest.raises(HTTPError) as failure:
        DataClient(client.base_url).request('/source/diagnostics',{})
    assert failure.value.code == 401


@pytest.mark.postgres
def test_acceptance_tool_download_query_repeat_and_exports(app_server,tmp_path):
    from tools.live_acceptance import run
    from test_storage_tasks import Source
    app, server, client = app_server
    app.worker.source = Source()
    app.worker.start()
    report = {}
    try:
        run(client,app.store,tmp_path,'2026-09-14','2026-09-14',report)
        assert report['state'] == 'passed'
        assert report['rows'] == 3
        assert report['duplicate_upsert'] == report['database_sdk'] == 'passed'
        assert report['exports'] == {'csv':'passed','parquet':'passed'}
        assert len(report['jobs']) == 4
    finally:
        app.worker.stop.set()
        app.worker.thread.join(timeout=5)


@pytest.mark.postgres
def test_http_origin_and_login_cookie(app_server):
    app, server, client = app_server
    request = Request(client.base_url + '/api/v1/login',data=json.dumps({'credential':app.settings.data['api_key']}).encode(),headers={'Content-Type':'application/json','Origin':'https://foreign.invalid'})
    with pytest.raises(HTTPError) as failure:
        urlopen(request)
    assert failure.value.code == 403
    request.remove_header('Origin')
    with urlopen(request) as response:
        cookie = response.headers['Set-Cookie']
        assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
    with urlopen(Request(client.base_url + '/api/v1/status',headers={'Cookie':cookie})) as response:
        assert json.load(response)['database']['connected']


@pytest.mark.postgres
def test_optional_login_password_enable_and_disable(app_server):
    app, server, client = app_server
    password = '本地测试-Password-123'
    client.request('/settings',{'login_enabled':True,'password':password})
    assert app.settings.check_password(password)
    request = Request(client.base_url + '/api/v1/login',data=json.dumps({'credential':password}).encode(),headers={'Content-Type':'application/json'})
    with urlopen(request) as response:
        assert json.load(response)['ok']
    client.request('/settings',{'login_enabled':False})
    assert not app.settings.check_password(password)
    loaded = Settings(config_path=app.settings.path)
    assert loaded.path.suffix == '.toml' and not loaded.check_password(password)
    assert not (app.settings.runtime / 'settings.local.json').exists()


@pytest.mark.postgres
def test_rejected_settings_do_not_modify_live_or_persisted_config(app_server):
    app, server, client = app_server
    original = app.settings.path.read_bytes()
    with pytest.raises(HTTPError) as failure:
        client.request('/settings',{'qmt_root':'D:/invalid-change','login_enabled':True,'password':'short'})
    assert failure.value.code == 400
    assert app.settings.qmt_root == ''
    assert app.settings.path.read_bytes() == original


@pytest.mark.postgres
def test_environment_qmt_path_is_not_written_back(app_server,monkeypatch):
    app, server, client = app_server
    monkeypatch.setenv('PFOR_QMT_QMT_ROOT','D:/EnvironmentQMT')
    client.request('/settings',{'qmt_root':app.settings.qmt_root,'login_enabled':False})
    monkeypatch.delenv('PFOR_QMT_QMT_ROOT')
    assert Settings(config_path=app.settings.path).qmt_root == ''


@pytest.mark.postgres
def test_websocket_ticket_and_job_event(app_server):
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed
    app, server, client = app_server
    websocket = start_websocket(app,0,server.server_port)
    app.ws_port = websocket.socket.getsockname()[1]
    try:
        ticket = client.request('/ws-ticket',{})
        address = 'ws://127.0.0.1:%s/?ticket=%s' % (app.ws_port,ticket['ticket'])
        with connect(address) as socket:
            for _ in range(30):
                if app.listeners:
                    break
                time.sleep(.02)
            app.publish({'event':'job','data':{'state':'completed'}})
            assert json.loads(socket.recv(timeout=3))['data']['state'] == 'completed'
        with connect(address) as socket:
            with pytest.raises(ConnectionClosed):
                socket.recv(timeout=3)
    finally:
        websocket.shutdown()


@pytest.mark.postgres
def test_websocket_watch_switch_stop_and_recovery(app_server,monkeypatch):
    from websockets.sync.client import connect
    from pfor_qmt import client as pipe_client, server as module
    app, server, client = app_server
    callbacks, released = [], []
    identity, now = [1], [100.0]
    monkeypatch.setattr(pipe_client,'get_client',lambda: SimpleNamespace(request=lambda *args,**kwargs: {'instance':'test','generation':identity[0]}))
    monkeypatch.setattr(module,'time',SimpleNamespace(time=time.time,monotonic=lambda:now[0]))
    def subscribe(codes,callback):
        callbacks.append(callback)
        callback({codes[0]:{'lastPrice':len(callbacks)}})
        return len(callbacks)
    app.source = SimpleNamespace(subscribe_whole_quote=subscribe,unsubscribe_quote=lambda seq:released.append(seq))
    websocket = start_websocket(app,0,server.server_port)
    app.ws_port = websocket.socket.getsockname()[1]
    def receive(socket,event):
        for _ in range(8):
            message = json.loads(socket.recv(timeout=3))
            if message['event'] == event:
                return message
        pytest.fail('Expected event: ' + event)
    try:
        ticket = client.request('/ws-ticket',{})
        with connect('ws://127.0.0.1:%s/?ticket=%s' % (app.ws_port,ticket['ticket'])) as socket:
            socket.send(json.dumps({'action':'watch','codes':['000300.SH']}))
            assert receive(socket,'quote')['data'] == {'000300.SH':{'lastPrice':1}}
            socket.send(json.dumps({'action':'watch','codes':['510300.SH']}))
            assert receive(socket,'quote')['data'] == {'510300.SH':{'lastPrice':2}}
            callbacks[0]({'000300.SH':{'lastPrice':999}})
            identity[0], now[0] = 2, 120
            assert receive(socket,'source')['connected'] is False
            now[0] = 126
            assert receive(socket,'quote')['data'] == {'510300.SH':{'lastPrice':3}}
            socket.send(json.dumps({'action':'unwatch'}))
            assert receive(socket,'watch')['codes'] == []
            callbacks[2]({'510300.SH':{'lastPrice':999}})
            app.publish({'event':'job','data':{'state':'completed'}})
            assert json.loads(socket.recv(timeout=3)) == {'event':'job','data':{'state':'completed'}}
            with pytest.raises(TimeoutError):
                socket.recv(timeout=.4)
            now[0] = 200
            socket.send(json.dumps({'action':'watch','codes':['000001.SZ']}))
            assert receive(socket,'quote')['data'] == {'000001.SZ':{'lastPrice':4}}
        deadline = time.monotonic() + 3
        while app.listeners and time.monotonic() < deadline:
            time.sleep(.02)
        assert not app.listeners and released == [1,2,3,4]
    finally:
        websocket.shutdown()


@pytest.mark.postgres
def test_websocket_invalid_commands_and_failed_unwatch_keep_connection(app_server,monkeypatch):
    from websockets.sync.client import connect
    from pfor_qmt import client as pipe_client
    app, server, client = app_server
    callbacks = []
    monkeypatch.setattr(pipe_client,'get_client',lambda: SimpleNamespace(request=lambda *args,**kwargs: {'instance':'test','generation':1}))
    app.source = SimpleNamespace(subscribe_whole_quote=lambda codes,cb:callbacks.append(cb) or 1,
                                unsubscribe_quote=lambda seq:False)
    websocket = start_websocket(app,0,server.server_port)
    app.ws_port = websocket.socket.getsockname()[1]
    try:
        ticket = client.request('/ws-ticket',{})
        with connect('ws://127.0.0.1:%s/?ticket=%s' % (app.ws_port,ticket['ticket'])) as socket:
            for command in [[],None,{'action':'unknown'},{'action':'watch','codes':[]}]:
                socket.send(json.dumps(command))
                assert json.loads(socket.recv(timeout=3))['event'] == 'error'
            socket.send(json.dumps({'action':'watch','codes':['000300.SH']}))
            assert json.loads(socket.recv(timeout=3))['event'] == 'source'
            assert json.loads(socket.recv(timeout=3))['event'] == 'watch'
            socket.send(json.dumps({'action':'unwatch'}))
            error = json.loads(socket.recv(timeout=3))
            assert error['event'] == 'error' and error['codes'] == ['000300.SH']
            callbacks[0]({'000300.SH':{'lastPrice':5}})
            assert json.loads(socket.recv(timeout=3))['data']['000300.SH']['lastPrice'] == 5
            app.source.unsubscribe_quote = lambda seq:True
            socket.send(json.dumps({'action':'unwatch'}))
            assert json.loads(socket.recv(timeout=3)) == {'event':'watch','codes':[]}
    finally:
        websocket.shutdown()


def test_real_windows_pipe_market_request_reconnect_and_trade_rejection(tmp_path,monkeypatch):
    import os
    if os.name != 'nt':
        pytest.skip('Windows named pipes only')
    monkeypatch.setenv('PFOR_QMT_PIPE_HUB_STATUS_FILE',str(tmp_path / 'pipe-status.json'))
    pipe = r'\\.\pipe\pfor_qmt_test_' + uuid.uuid4().hex
    hub = MarketHub(pipe_name=pipe,show=False)
    threading.Thread(target=hub.start,daemon=True).start()
    transport = PipeTxClient(pipe_name=pipe,request_channel='pfor_qmt.market.request',show=False,reconnect_interval=.1)
    bridge = MarketBridge(object(),tx=transport).start()
    stop = threading.Event()
    def pump():
        while not stop.wait(.01):
            bridge.pump()
    threading.Thread(target=pump,daemon=True).start()
    client = MarketClient(pipe_name=pipe,timeout=3)
    raw = PipeRpcClient(pipe_name=pipe,timeout=3)
    try:
        for _ in range(100):
            if hub.qmt_rx_by_channel and hub.qmt_tx_by_channel:
                break
            time.sleep(.02)
        assert client.request('pfor.ping')['mode'] == 'market-only'
        with pytest.raises(CfquantError,match='Unsupported'):
            raw.request('xttrader.order_stock',{})
        client.close()
        assert client.request('pfor.ping')['source'] == 'qmt'
    finally:
        client.close()
        raw.close()
        stop.set()
        bridge.close()
        hub.close()
