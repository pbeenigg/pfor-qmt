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
from browser_helpers import choose_many, navigate, new_dataset, confirm_export


@pytest.mark.browser
@pytest.mark.postgres
def test_derivative_selection_details_and_board_dataset(store, tmp_path, monkeypatch):
    if os.environ.get('PFOR_QMT_BROWSER_TEST') != '1':
        pytest.skip('设置 PFOR_QMT_BROWSER_TEST=1 执行浏览器验收')
    from pfor_qmt import service, client as pipe_client
    monkeypatch.setattr(service, 'get_client', lambda: SimpleNamespace(request=lambda *args, **kwargs: {'mode': 'market-only'}))
    monkeypatch.setattr(pipe_client, 'get_client', lambda: SimpleNamespace(request=lambda *args, **kwargs: {'instance': 'assets', 'generation': 1}))
    for code, name, kind in [('cu2610.SF', '沪铜2610', 'future'), ('cu2610C80000.SF', '沪铜购80000', 'option'),
                             ('SP au2610&au2612.SF', '黄金跨期组合', 'future'), ('160105.SZ', '南方积极LOF', 'fund'),
                             ('113001.SH', '测试转债', 'bond'), ('000001.SZ', '平安银行', 'stock')]:
        store.save_security(code, name, kind, {'InstrumentName': name}, metadata={'multiplier': 5, 'price_tick': 10})
    with store.connect() as conn:
        store.save_board({'name': '银行行业', 'category': 'industry', 'path': ['行业', '申万']}, ['000001.SZ', '000003.SZ'], conn)
    app = Application(Settings(tmp_path, config_path=tmp_path/'config.toml'), store=store)
    app.source = SimpleNamespace(get_full_tick=lambda selected: {code: {'lastPrice': 80000} for code in selected})
    server = HTTPServer(('127.0.0.1', 0), handler_for(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    websocket = start_websocket(app, 0, server.server_port)
    app.ws_port = websocket.socket.getsockname()[1]
    output = Path(__file__).resolve().parents[1]/'output'/'playwright'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 980})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:' + str(server.server_port))
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key)
            page.locator('#login-form button').click()
            expect(page.locator('#catalog-counts')).to_contain_text('期货 2 个')
            choose_many(page,'#security-form [name="kind"]','future')
            choose_many(page,'#security-form [name="market"]','SF')
            page.locator('#security-form [name="search"]').fill('沪铜')
            page.locator('#security-form button[type=submit]').click()
            expect(page.locator('#securities tr')).to_have_count(1)
            page.locator('#securities [data-instrument="cu2610.SF"]').click()
            expect(page.locator('#instrument-title')).to_have_text('沪铜2610')
            expect(page.locator('#instrument-fields')).to_contain_text('合约乘数')
            page.screenshot(path=str(output/'multi-asset-details-desktop.png'), full_page=True)
            page.keyboard.press('Escape')
            navigate(page,'quotes')
            page.locator('[data-picker="quotes"]').click()
            page.locator('#picker-clear').click()
            choose_many(page,'#picker-kind','future')
            choose_many(page,'#picker-market','SF')
            page.locator('#picker-search').fill('黄金')
            expect(page.locator('#picker-rows input')).to_have_count(1)
            expect(page.locator('#picker-rows input')).to_have_value('SP au2610&au2612.SF')
            page.locator('#picker-rows label').click()
            expect(page.locator('#picker-rows input')).to_be_checked()
            page.locator('#picker-apply').click()
            page.locator('#quote-refresh').click()
            expect(page.locator('#quotes')).to_contain_text('SP au2610&au2612.SF')
            navigate(page,'boards')
            expect(page.locator('#board-rows')).to_contain_text('银行行业')
            page.locator('[data-board-members="0"]').click()
            expect(page.locator('#board-members')).to_contain_text('平安银行')
            expect(page.locator('#board-members')).to_contain_text('1 个成员资料未收录')
            expect(page.locator('#board-members')).to_contain_text('000003.SZ（资料未收录）')
            page.locator('[data-board-dataset="0"]').click()
            expect(page.locator('#dataset-form [name="name"]')).to_have_value('银行行业成员')
            page.locator('#dataset-form button[type="submit"]').click()
            expect(page.locator('#datasets')).to_contain_text('银行行业成员')
            assert store.query('SELECT board_name FROM datasets', one=True)['board_name'] == '银行行业'
            for width, height, label in [(1440,980,'desktop'), (390,844,'mobile'), (768,1024,'tablet')]:
                page.set_viewport_size({'width': width, 'height': height})
                for view in ('market','boards','history','settings'):
                    navigate(page,view)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), view
                navigate(page,'qmt')
                page.locator('[data-picker="diagnostics"]').click()
                choose_many(page,'#picker-kind','option')
                choose_many(page,'#picker-market','SF')
                expect(page.locator('#picker-rows input')).to_have_count(1)
                expect(page.locator('#picker-rows input')).to_have_value('cu2610C80000.SF')
                page.locator('#picker-rows input').check()
                page.screenshot(path=str(output/f'multi-asset-picker-{label}.png'), full_page=True)
                box = page.locator('#security-picker').bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                page.locator('#picker-apply').click()
                expect(page.locator('#diagnostic-code')).to_have_value('cu2610C80000.SF')
            assert not errors, errors
            browser.close()
    finally:
        websocket.shutdown()
        server.shutdown()
        server.server_close()


@pytest.mark.browser
@pytest.mark.postgres
def test_desktop_mobile_query_dataset_export_and_auth(store,tmp_path,monkeypatch):
    if os.environ.get('PFOR_QMT_BROWSER_TEST') != '1':
        pytest.skip('设置 PFOR_QMT_BROWSER_TEST=1 执行浏览器验收')
    app = Application(Settings(tmp_path,config_path=tmp_path / 'config.toml'),store=store)
    from pfor_qmt import service, client as pipe_client
    monkeypatch.setattr(service,'get_client',lambda: SimpleNamespace(request=lambda *args,**kwargs: {'mode':'market-only'}))
    monkeypatch.setattr(pipe_client,'get_client',lambda: SimpleNamespace(request=lambda *args,**kwargs: {'instance':'browser','generation':1}))
    subscriptions, released = [], []
    def subscribe(codes, callback):
        subscriptions.append(callback)
        callback({code:{'lastPrice':len(subscriptions),'time':20260917140000} for code in codes})
        return len(subscriptions)
    app.source = SimpleNamespace(get_full_tick=lambda codes:{codes[0]:{'lastPrice':10}},
                                get_local_data=lambda **params:{},get_trading_dates=lambda *args:[],
                                subscribe_whole_quote=subscribe,unsubscribe_quote=lambda seq:released.append(seq),
                                get_stock_list_in_sector=lambda sector:['000001.SZ'])
    for code,name,kind in [('000300.SH','沪深300','index'),('000001.SZ','平安银行','stock'),('510300.SH','沪深300ETF','etf')]:
        store.save_security(code,name,kind,{})
    with store.connect() as conn:
        conn.execute("INSERT INTO catalog_sectors(name) VALUES('沪深300')")
    # Only this isolated test schema contains these synthetic prices.
    seed = store.create_job('download',{'members':['000300.SH'],'periods':['1d']})
    start = datetime(2026,9,1,tzinfo=SHANGHAI)
    rows = [dict(code='000300.SH',period='1d',time=start+timedelta(days=index),open=Decimal('3600')+index*10,
                 high=Decimal('3660')+index*10,low=Decimal('3570')+index*10,close=Decimal('3640')+index*10,
                 volume=Decimal('2000000'),amount=Decimal('7200000000')) for index in range(10)]
    store.write_chunk(seed['id'],{'code':'000300.SH','period':'1d','start':'2026-09-01','end':'2026-09-10'},rows,[],1)
    store.update_job(seed['id'],state='succeeded',result={'rows':10})
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
            navigate(page,'quotes')
            page.locator('#quote-form button[type="submit"]').click()
            expect(page.locator('#quote-status')).to_have_text('已订阅 3 个证券')
            expect(page.locator('#quotes tr')).to_have_count(3)
            page.locator('#quote-form button[type="submit"]').click()
            expect(page.locator('#quote-status')).to_have_text('已订阅 3 个证券')
            assert len(subscriptions) == 1
            page.locator('[data-picker="quotes"]').click()
            page.locator('#picker-clear').click()
            page.locator('#picker-search').fill('ETF')
            expect(page.locator('#picker-rows input')).to_have_count(1)
            page.locator('#picker-rows input').check()
            page.locator('#picker-apply').click()
            expect(page.locator('[data-picker="quotes"]')).to_be_focused()
            expect(page.locator('#quote-selection')).to_contain_text('沪深300ETF')
            page.locator('#quote-form button[type="submit"]').click()
            expect(page.locator('#quote-status')).to_have_text('已订阅 1 个证券')
            expect(page.locator('#quotes tr')).to_have_count(1)
            subscriptions[0]({'000300.SH':{'lastPrice':999}})
            page.locator('#quote-stop').click()
            expect(page.locator('#quote-status')).to_have_text('已停止订阅')
            subscriptions[1]({'510300.SH':{'lastPrice':999}})
            expect(page.locator('#quote-stop')).to_be_disabled()
            expect(page.locator('#quotes')).not_to_contain_text('999')
            page.screenshot(path=str(output / 'market-stopped-desktop.png'),full_page=True)
            page.locator('#quote-form button[type="submit"]').click()
            expect(page.locator('#quote-status')).to_have_text('已订阅 1 个证券')
            assert released == [1,2]
            page.evaluate('state.socket.close()')
            expect(page.locator('#quote-status')).to_have_text('连接已断开，等待恢复')
            expect(page.locator('#quote-status')).to_have_text('已订阅 1 个证券',timeout=12000)
            assert len(subscriptions) == 4 and released == [1,2,3]
            page.locator('#quote-stop').click()
            expect(page.locator('#quote-status')).to_have_text('已停止订阅')
            assert released == [1,2,3,4]
            navigate(page,'history')
            page.locator('#history-form [name="start"]').fill('2026-09-01')
            page.locator('#history-form [name="end"]').fill('2026-09-10')
            page.locator('#history-form button[type="submit"], #history-form button.primary').click()
            expect(page.locator('#bars tr')).to_have_count(10)
            expect(page.locator('#chart-empty')).to_be_hidden()
            canvas = page.locator('#chart canvas')
            expect(canvas).to_be_visible()
            nonblank = canvas.evaluate('(canvas) => { const data = canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data; let n=0; for(let i=0;i<data.length;i+=4) if(data[i+3]>0 && (data[i]<180 || data[i+1]<180 || data[i+2]<180)) n++; return n; }')
            assert nonblank > 100
            new_dataset(page)
            page.locator('#dataset-form [name="name"]').fill('浏览器测试指数')
            page.locator('[data-picker="dataset"]').click()
            page.locator('#picker-search').fill('沪深300')
            expect(page.locator('#picker-rows input')).to_have_count(2)
            page.locator('#picker-rows input[value="000300.SH"]').check()
            page.locator('#picker-apply').click()
            page.locator('#dataset-form button[type="submit"]').click()
            expect(page.locator('#datasets')).to_contain_text('浏览器测试指数')
            page.evaluate('scrollTo(0,0)')
            page.screenshot(path=str(output / 'history-desktop.png'),full_page=True)
            app.worker.last_error = '交易日历为空，调度等待重试'
            navigate(page,'history')
            confirm_export(page,'#export-csv')
            expect(page.locator('#jobs')).to_be_visible()
            expect(page.locator('#worker-status')).to_contain_text('交易日历为空')
            expect(page.locator('#job-rows a[title="下载文件"]')).to_be_visible(timeout=15000)
            with page.expect_download() as download:
                page.locator('#job-rows a[title="下载文件"]').click()
            file = download.value.path()
            assert Path(file).read_bytes().startswith(b'\xef\xbb\xbf')
            app.worker.last_error = ''
            page.locator('#refresh-jobs').click()
            expect(page.locator('#worker-status')).to_be_hidden()
            page.screenshot(path=str(output / 'jobs-desktop.png'),full_page=True)
            navigate(page,'indices')
            page.locator('[data-picker="index"]').click()
            expect(page.locator('#picker-kind')).to_be_disabled()
            expect(page.locator('#picker-rows input')).to_have_count(1)
            page.locator('#picker-rows input').check()
            page.locator('#picker-apply').click()
            expect(page.locator('#index-form [name="sector"]')).to_have_value('沪深300')
            page.locator('#index-form button[type="submit"]').click()
            expect(page.locator('#index-rows')).to_contain_text('沪深300')
            navigate(page,'qmt')
            page.locator('#diagnose-source').click()
            expect(page.locator('#source-checks tr')).to_have_count(4)
            expect(page.locator('#source-result')).to_contain_text('历史链路未就绪')
            expect(page.locator('#source-checks')).to_contain_text('本地日线为空')
            page.screenshot(path=str(output / 'diagnostics-desktop.png'),full_page=True)
            for width,height,label in [(390,844,'mobile'),(768,1024,'tablet')]:
                page.set_viewport_size({'width':width,'height':height})
                for view in ['market','indices','history','jobs','settings']:
                    navigate(page,view)
                    expect(page.locator('#'+view)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(output / f'{view}-{label}.png'),full_page=True)
                navigate(page,'quotes')
                page.locator('[data-picker="quotes"]').click()
                expect(page.locator('#picker-rows input')).to_have_count(3)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                box = page.locator('#security-picker').bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width
                page.screenshot(path=str(output / f'picker-{label}.png'),full_page=True)
                page.locator('#picker-search').fill('平安')
                expect(page.locator('#picker-rows input')).to_have_count(1)
                page.keyboard.press('Escape')
                expect(page.locator('#security-picker')).not_to_be_visible()
                expect(page.locator('[data-picker="quotes"]')).to_be_focused()
            navigate(page,'access')
            assert errors == [], errors
            page.locator('[name="login_enabled"][form="settings-form"]').check()
            page.locator('[name="password"][form="settings-form"]').fill('browser-test-password-only')
            page.locator('#access-page').get_by_role('button',name='保存配置',exact=True).click()
            expect(page.locator('#notice')).to_contain_text('配置已保存')
            page.locator('[name="login_enabled"][form="settings-form"]').uncheck()
            page.locator('#access-page').get_by_role('button',name='保存配置',exact=True).click()
            expect(page.locator('[name="password"][form="settings-form"]')).to_be_disabled()
            page.locator('#logout').click()
            expect(page.locator('#login-dialog')).to_be_visible()
            browser.close()
    finally:
        app.worker.stop.set()
        app.worker.thread.join(timeout=5)
        websocket.shutdown()
        server.shutdown()
        server.server_close()


@pytest.mark.browser
@pytest.mark.postgres
def test_selector_keeps_cross_page_choices_and_cancel_restores_form(store, tmp_path):
    if os.environ.get('PFOR_QMT_BROWSER_TEST') != '1':
        pytest.skip('设置 PFOR_QMT_BROWSER_TEST=1 执行浏览器验收')
    with store.connect() as conn:
        conn.execute("INSERT INTO securities(code,name,kind) SELECT lpad(i::text,6,'0')||'.SZ','测试证券'||i,'stock' FROM generate_series(1,65) i")
    app = Application(Settings(tmp_path, config_path=tmp_path / 'config.toml'), store=store)
    server = HTTPServer(('127.0.0.1',0), handler_for(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    websocket = start_websocket(app,0,server.server_port)
    app.ws_port = websocket.socket.getsockname()[1]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto('http://127.0.0.1:' + str(server.server_port))
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key)
            page.locator('#login-form button').click()
            new_dataset(page)
            page.locator('[data-picker="dataset"]').click()
            expect(page.locator('#picker-rows input')).to_have_count(50)
            page.locator('#picker-all').check()
            expect(page.locator('#picker-count')).to_have_text('已选 50 个')
            page.locator('#picker-next').click()
            expect(page.locator('#picker-rows input')).to_have_count(15)
            page.locator('#picker-rows input[value="000065.SZ"]').check()
            page.locator('#picker-selected-only').check()
            expect(page.locator('#picker-page')).to_contain_text('共 51 个')
            page.locator('#picker-search').fill('测试证券65')
            expect(page.locator('#picker-rows input')).to_have_count(1)
            expect(page.locator('#picker-rows input')).to_be_checked()
            page.locator('#picker-apply').click()
            selected = page.locator('#dataset-form [name="members"]').input_value()
            assert len(selected.split(',')) == 51 and '000065.SZ' in selected
            page.locator('[data-picker="dataset"]').click()
            page.locator('#picker-clear').click()
            expect(page.locator('#picker-count')).to_have_text('已选 0 个')
            expect(page.locator('#picker-apply')).to_be_disabled()
            page.keyboard.press('Escape')
            assert page.locator('#dataset-form [name="members"]').input_value() == selected
            expect(page.locator('[data-picker="dataset"]')).to_be_focused()
            browser.close()
    finally:
        websocket.shutdown()
        server.shutdown()
        server.server_close()
