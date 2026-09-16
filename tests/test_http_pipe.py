import json
import queue
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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
    app = Application(Settings(tmp_path),store=store)
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
