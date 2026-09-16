"""Opt-in real Chromium workflow tests against isolated PostgreSQL and synthetic bars."""
import os
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.data import SHANGHAI, chunks
from pfor_qmt.server import HTTPServer, handler_for, start_websocket
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings


@pytest.mark.browser
@pytest.mark.postgres
def test_desktop_mobile_query_dataset_export_and_auth(store,tmp_path,monkeypatch):
    if os.environ.get('PFOR_QMT_BROWSER_TEST') != '1':
        pytest.skip('设置 PFOR_QMT_BROWSER_TEST=1 执行浏览器验收')
    app = Application(Settings(tmp_path,config_path=tmp_path / 'config.toml'),store=store)
    from pfor_qmt import service
    monkeypatch.setattr(service,'get_client',lambda: SimpleNamespace(request=lambda *args,**kwargs: {'mode':'market-only'}))
    app.source = SimpleNamespace(get_full_tick=lambda codes:{codes[0]:{'lastPrice':10}},
                                get_local_data=lambda **params:{},get_trading_dates=lambda *args:[])
    # Only this isolated test schema contains these synthetic prices.
    seed = store.create_job('download',{'members':['000300.SH'],'periods':['1d']})
    start = datetime(2026,9,1,tzinfo=SHANGHAI)
    rows = [dict(code='000300.SH',period='1d',time=start+timedelta(days=index),open=Decimal('3600')+index*10,
                 high=Decimal('3660')+index*10,low=Decimal('3570')+index*10,close=Decimal('3640')+index*10,
                 volume=Decimal('2000000'),amount=Decimal('7200000000')) for index in range(10)]
    store.write_chunk(seed['id'],{'code':'000300.SH','period':'1d','start':'2026-09-01','end':'2026-09-10'},rows,[],1)
    store.update_job(seed['id'],state='completed',result={'rows':10})
    app.worker.source = None
    server = HTTPServer(('127.0.0.1',0),handler_for(app))
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    websocket = start_websocket(app,0,server.server_port)
    app.ws_port = websocket.socket.getsockname()[1]
    app.worker.start()
    output = Path(__file__).resolve().parents[1] / 'output' / 'playwright'
    output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width':1440,'height':980})
            errors = []
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:' + str(server.server_port))
            expect(page.locator('#login-dialog')).to_be_visible()
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.data['api_key'])
            page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible()
            page.locator('nav [data-view="history"]').click()
            page.locator('#history-form [name="start"]').fill('2026-09-01')
            page.locator('#history-form [name="end"]').fill('2026-09-10')
            page.locator('#history-form button[type="submit"], #history-form button.primary').click()
            expect(page.locator('#bars tr')).to_have_count(10)
            expect(page.locator('#chart-empty')).to_be_hidden()
            canvas = page.locator('#chart canvas')
            expect(canvas).to_be_visible()
            nonblank = canvas.evaluate('(canvas) => { const data = canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data; let n=0; for(let i=0;i<data.length;i+=4) if(data[i+3]>0 && (data[i]<180 || data[i+1]<180 || data[i+2]<180)) n++; return n; }')
            assert nonblank > 100
            page.locator('#dataset-form [name="name"]').fill('浏览器测试指数')
            page.locator('#dataset-form [name="members"]').fill('000300.SH')
            page.locator('#dataset-form button').click()
            expect(page.locator('#datasets')).to_contain_text('浏览器测试指数')
            page.evaluate('scrollTo(0,0)')
            page.screenshot(path=str(output / 'history-desktop.png'),full_page=True)
            page.locator('#export-csv').click()
            expect(page.locator('#jobs')).to_be_visible()
            expect(page.locator('#job-rows a[title="下载文件"]')).to_be_visible(timeout=15000)
            with page.expect_download() as download:
                page.locator('#job-rows a[title="下载文件"]').click()
            file = download.value.path()
            assert Path(file).read_bytes().startswith(b'\xef\xbb\xbf')
            page.screenshot(path=str(output / 'jobs-desktop.png'),full_page=True)
            page.locator('nav [data-view="settings"]').click()
            page.locator('#diagnose-source').click()
            expect(page.locator('#source-checks tr')).to_have_count(4)
            expect(page.locator('#source-result')).to_contain_text('历史链路未就绪')
            expect(page.locator('#source-checks')).to_contain_text('本地日线为空')
            page.screenshot(path=str(output / 'diagnostics-desktop.png'),full_page=True)
            for width,height,label in [(390,844,'mobile'),(768,1024,'tablet')]:
                page.set_viewport_size({'width':width,'height':height})
                for view in ['market','indices','history','jobs','settings']:
                    page.locator(f'nav [data-view="{view}"]').click()
                    expect(page.locator('#'+view)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(output / f'{view}-{label}.png'),full_page=True)
            assert errors == [], errors
            page.locator('#settings-form [name="login_enabled"]').check()
            page.locator('#settings-form [name="password"]').fill('browser-test-password-only')
            page.locator('#settings-form button[type="submit"]').click()
            expect(page.locator('#notice')).to_contain_text('本地配置已保存')
            page.locator('#settings-form [name="login_enabled"]').uncheck()
            page.locator('#settings-form button[type="submit"]').click()
            expect(page.locator('#settings-form [name="password"]')).to_be_disabled()
            page.locator('#logout').click()
            expect(page.locator('#login-dialog')).to_be_visible()
            browser.close()
    finally:
        app.worker.stop.set()
        app.worker.thread.join(timeout=5)
        websocket.shutdown()
        server.shutdown()
        server.server_close()
