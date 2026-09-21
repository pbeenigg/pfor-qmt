"""Real browser workflow, isolated database, synthetic Tushare responses."""
import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect
from browser_helpers import choose_many, navigate, new_dataset, confirm_export

from pfor_qmt.server import HTTPServer, handler_for, start_websocket
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tushare import TushareSource, SourceError
from test_tushare import fake_request, account, seed


@pytest.mark.browser
@pytest.mark.postgres
def test_multi_account_tushare_full_workflow_without_qmt(store,tmp_path,monkeypatch):
    if os.environ.get('PFOR_QMT_BROWSER_TEST') != '1':
        pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    monkeypatch.setattr(TushareSource,'request',fake_request)
    app=Application(Settings(config_path=tmp_path/'config.toml'),store=store)
    server=HTTPServer(('127.0.0.1',0),handler_for(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    websocket=start_websocket(app,0,server.server_port)
    app.ws_port=websocket.socket.getsockname()[1]
    app.worker.start();app.tushare_worker.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright'
    output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1440,'height':980})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key)
            page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible()
            page.locator('#account-new').click()
            expect(page.locator('#account-form')).to_be_visible()
            page.locator('#account-form [name=id]').fill('main')
            page.locator('#account-form [name=name]').fill('研究账号')
            page.locator('#account-form [name=token]').fill('synthetic-browser-token')
            assert page.locator('#account-form').evaluate('form => form.checkValidity()'), page.locator('#account-form').evaluate('form => [...form.elements].filter(e=>!e.checkValidity()).map(e=>[e.name,e.validationMessage])')
            try:
                with page.expect_response(lambda response: response.url.endswith('/sources/tushare/accounts') and response.request.method=='POST',timeout=5000) as saved:
                    page.locator('#account-form button[type=submit]').click()
            except Exception:
                raise AssertionError({'errors':errors,'notice':page.locator('#notice').text_content(),'invalid':page.locator('#account-form').evaluate('form => [...form.elements].filter(e=>!e.checkValidity()).map(e=>[e.name,e.validationMessage])')}) from None
            assert saved.value.status==200, saved.value.text()
            expect(page.locator('#account-rows')).to_contain_text('研究账号')
            expect(page.locator('#account-form [name=token]')).to_have_value('')
            page.locator('[data-account-test=main]').click()
            expect(page.locator('#account-test-status')).to_contain_text('检测完成',timeout=15000)
            expect(page.locator('#account-capabilities')).to_contain_text('可用')
            page.screenshot(path=str(output/'tushare-settings-desktop.png'),full_page=True)
            navigate(page,'collect')
            page.locator('#data-source').select_option('tushare')
            page.locator('[data-collect-mode=catalog]').click()
            expect(page.locator('#qmt-live')).to_be_hidden()
            page.locator('#catalog-form button[type=submit]').click()
            expect(page.locator('#catalog-status')).to_contain_text('已完成',timeout=20000)
            expect(page.locator('#catalog-counts')).to_contain_text('期货 12 个')
            navigate(page,'catalog')
            page.locator('#security-form [name=search]').fill('CU2610')
            page.locator('#security-form button[type=submit]').click()
            expect(page.locator('#securities tr')).to_have_count(1)
            new_dataset(page)
            page.locator('#dataset-form [name=name]').fill('铜日线')
            page.locator('[data-picker=dataset]').click()
            page.locator('#picker-search').fill('CU2610')
            expect(page.locator('#picker-rows input')).to_have_count(1)
            page.locator('#picker-rows input').check()
            page.locator('#picker-apply').click()
            page.locator('#dataset-form button[type=submit]').click()
            expect(page.locator('#datasets')).to_contain_text('铜日线')
            expect(page.locator('#datasets')).to_contain_text('19:00')
            page.locator('#datasets [data-download]').click()
            expect(page.locator('#download-form [name=dataset_id]')).not_to_have_value('')
            page.locator('#download-form [name=start]').fill('2026-09-14')
            page.locator('#download-form [name=end]').fill('2026-09-14')
            with page.expect_response(lambda response: response.url.endswith('/downloads') and response.request.method=='POST') as download:
                page.locator('#download-form button.primary').click()
                page.locator('#batch-submit').click()
            assert download.value.status==200, download.value.text()
            expect(page.locator('#job-rows')).to_contain_text('1 行',timeout=15000)
            navigate(page,'history')
            page.locator('[data-picker=history]').click()
            page.locator('#picker-search').fill('CU2610')
            expect(page.locator('#picker-rows input')).to_have_count(1)
            page.locator('#picker-rows input').check();page.locator('#picker-apply').click()
            page.locator('#history-form [name=start]').fill('2026-09-14')
            page.locator('#history-form [name=end]').fill('2026-09-14')
            page.locator('#history-form button.primary').click()
            expect(page.locator('#bars [title="80123.1234567890123456789"]')).to_have_count(1)
            expect(page.locator('#history-units')).to_contain_text('成交额：元')
            page.screenshot(path=str(output/'tushare-history-desktop.png'),full_page=True)
            confirm_export(page,'#export-csv')
            expect(page.locator('#job-rows a[aria-label="下载文件"]')).to_have_count(1,timeout=15000)
            with page.expect_download() as info:
                page.locator('#job-rows a[aria-label="下载文件"]').click()
            exported=Path(info.value.path()).read_text('utf-8-sig')
            assert 'tushare' in exported and '80123.1234567890123456789' in exported
            page.set_viewport_size({'width':390,'height':844})
            navigate(page,'settings')
            page.locator('[data-account-edit=main]').click()
            expect(page.locator('#account-form')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'tushare-settings-mobile.png'),full_page=True)
            page.keyboard.press('Escape')
            navigate(page,'history')
            page.screenshot(path=str(output/'tushare-history-mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('#data-source').select_option('qmt')
            expect(page.locator('#history-form [name=code]')).to_have_value('')
            expect(page.locator('#bars')).not_to_contain_text('80123.1234567890123456789')
            assert not errors
            browser.close()
    finally:
        app.worker.stop.set();app.tushare_worker.stop.set()
        app.worker.thread.join(timeout=10);app.tushare_worker.thread.join(timeout=10)
        websocket.shutdown();server.shutdown();server.server_close();thread.join()


@pytest.mark.browser
@pytest.mark.postgres
def test_failed_minutes_keep_daily_rows_and_allow_daily_only_retry(store,tmp_path,monkeypatch):
    if os.environ.get('PFOR_QMT_BROWSER_TEST') != '1':
        pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    def request(self,api,p,fields=''):
        if api=='ft_mins':
            raise SourceError('ft_mins: 接口权限不足，请核对账号权限','permission')
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    seed(store)
    app=Application(Settings(config_path=tmp_path/'config.toml'),store=store)
    app.settings.accounts=[account()]
    app.settings.default_account_id='main'
    dataset=app.dispatch('POST','/datasets',dict(source='tushare',name='铜多周期',members=['CU2610.SHF'],periods=['1d','1m','5m']))
    failed=app.dispatch('POST','/downloads',dict(dataset_id=str(dataset['id']),start='2026-09-14',end='2026-09-14'))
    app.tushare_worker.execute(store.job(failed['id']))
    server=HTTPServer(('127.0.0.1',0),handler_for(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    websocket=start_websocket(app,0,server.server_port)
    app.ws_port=websocket.socket.getsockname()[1]
    app.worker.start();app.tushare_worker.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright'
    output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1440,'height':980})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#jobs')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key)
            page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible()
            failed_row=page.locator('#job-rows tr').filter(has_text=str(failed['id'])[:8])
            page.locator('#data-source').select_option('tushare')
            expect(failed_row).to_contain_text('部分完成')
            expect(failed_row).to_contain_text('已入库 1 行')
            expect(failed_row).to_contain_text('只选日线')
            navigate(page,'collect')
            page.locator('#data-source').select_option('tushare')
            choose_many(page,'#download-form [name=dataset_id]',str(dataset['id']))
            daily=page.locator('#download-form input[value="1d"]')
            minute=page.locator('#download-form input[value="1m"]')
            five=page.locator('#download-form input[value="5m"]')
            expect(daily).to_be_checked();expect(minute).to_be_checked();expect(five).to_be_checked()
            minute.uncheck();five.uncheck()
            page.locator('#download-form [name=start]').fill('2026-09-14')
            page.locator('#download-form [name=end]').fill('2026-09-14')
            with page.expect_response(lambda r:r.url.endswith('/downloads') and r.request.method=='POST') as response:
                page.locator('#download-form button.primary').click()
                page.locator('#batch-submit').click()
            assert response.value.status==200
            job=response.value.json()
            assert job['payload']['periods']==['1d'] and job['payload']['account_id']=='main'
            completed_row=page.locator('#job-rows tr').filter(has_text=job['id'][:8])
            expect(completed_row).to_contain_text('已完成',timeout=15000)
            expect(completed_row).to_contain_text('已入库 1 行')
            navigate(page,'settings')
            page.locator('[data-account-test=main]').click()
            expect(page.locator('#account-test-status')).to_contain_text('检测完成',timeout=15000)
            expect(page.locator('#account-capabilities')).to_contain_text('权限不足')
            navigate(page,'collect')
            expect(minute).to_be_disabled();expect(five).to_be_disabled();expect(daily).to_be_enabled()
            expect(page.locator('#download-capability')).to_contain_text('历史分钟权限不可用')
            page.screenshot(path=str(output/'tushare-failed-minutes-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            expect(daily).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'tushare-failed-minutes-mobile.png'),full_page=True)
            # Source-scoped selection clears on switching, but persisted bindings and permissions remain.
            page.locator('#data-source').select_option('qmt')
            expect(daily).to_be_disabled()
            page.locator('#data-source').select_option('tushare')
            choose_many(page,'#download-form [name=dataset_id]',str(dataset['id']))
            expect(minute).to_be_disabled()
            assert app.dispatch('GET','/datasets',{})[0]['periods']==['1d','1m','5m']
            assert not errors
            browser.close()
    finally:
        app.worker.stop.set();app.tushare_worker.stop.set()
        app.worker.thread.join(timeout=10);app.tushare_worker.thread.join(timeout=10)
        websocket.shutdown();server.shutdown();server.server_close();thread.join()
