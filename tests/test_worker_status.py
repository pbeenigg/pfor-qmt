import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.maintenance import schedule_event
from pfor_qmt.sdk import DataClient
from pfor_qmt.server import HTTPServer, handler_for
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings


def add_issues(app):
    for index,market in enumerate(('SH','SZ','IF')):
        schedule_event(app.worker,{'id':'shared' if index<2 else 'second','name':'沪深300成分' if index<2 else '<img src=x onerror=alert(1)>'},'dataset',market,
                       '交易日历请求失败，尝试使用已保存日期','NETWORK_ERROR','恢复连接后重试未完成范围')
    app.worker.last_error='legacy joined message'
    app.tushare_worker.catalog_error='目录接口暂不可用'


@pytest.mark.postgres
def test_worker_status_exposes_fields_and_keeps_legacy_status(store,tmp_path):
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    add_issues(app)
    result=app.dispatch('GET','/status',{})
    assert result['worker']=='legacy joined message'
    assert len(result['worker_issues'])==4
    first=result['worker_issues'][0]
    assert first['name']=='沪深300成分' and first['market']=='SH' and first['lane']=='schedule'
    assert first['action']=='恢复连接后重试未完成范围'
    assert result['worker_issues'][-1]['source']=='tushare'
    schedule_event(app.worker,{'id':'shared','name':'沪深300成分'},'dataset','SH')
    assert len(app.worker.status_issues())==2
    app.worker.last_error='';app.tushare_worker.catalog_error=''
    assert app.dispatch('GET','/status',{})['worker_issues']==[]
    app.worker.export_error='password=secret123'
    assert 'secret123' not in app.worker.status_issues()[0]['reason']


@pytest.mark.browser
@pytest.mark.postgres
def test_worker_warning_summary_details_refresh_recovery_and_mobile(store,tmp_path):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':
        pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    add_issues(app)
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        client=DataClient('http://127.0.0.1:'+str(server.server_port),app.settings.api_key)
        assert len(client.request('/status')['worker_issues'])==4
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(client.base_url+'/#jobs')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            expect(page.locator('#worker-alert-title')).to_have_text('后台任务需关注')
            expect(page.locator('#worker-alert-count')).to_contain_text('2 个范围 · 4 项异常')
            expect(page.locator('#worker-alert-rows')).not_to_be_visible()
            assert len(page.locator('#worker-status summary').inner_text())<100
            page.locator('#worker-status summary').focus();page.keyboard.press('Enter')
            expect(page.locator('#worker-alert-rows tr')).to_have_count(4)
            expect(page.locator('#worker-alert-rows')).to_contain_text('沪深300成分')
            expect(page.locator('#worker-alert-rows')).to_contain_text('Tushare')
            assert page.locator('#worker-alert-rows img').count()==0
            page.locator('#refresh-jobs').click()
            expect(page.locator('#worker-alert-rows')).to_be_visible()
            for label,width,height in [('desktop',1440,980),('mobile',390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                page.locator('#worker-status').scroll_into_view_if_needed()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.locator('#worker-status').evaluate('(el)=>el.scrollWidth <= el.clientWidth')
                page.screenshot(path=str(output/f'worker-status-{label}.png'),full_page=True)
            page.locator('#worker-alert-logs').click()
            expect(page.locator('#operations')).to_be_visible()
            page.locator('nav [data-view=jobs]').click()
            app.tushare_worker.catalog_error=''
            page.locator('#refresh-jobs').click()
            expect(page.locator('#worker-alert-title')).to_have_text('自动更新受阻')
            app.worker.last_error=''
            page.locator('#refresh-jobs').click()
            expect(page.locator('#worker-status')).to_be_hidden()
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
