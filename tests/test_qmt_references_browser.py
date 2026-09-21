import os
import threading

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.server import HTTPServer, handler_for
from test_qmt_references import qmt_app, choice, run
from test_qmt_reference_native_responses import NativeSource
from browser_helpers import choose_many, navigate, sync_report


@pytest.mark.browser
def test_qmt_reference_flows_and_responsive_views(qmt_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=qmt_app
    for params in (choice(),choice('calendar'),choice(mode='history')):run(app,params)
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#mapping')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible()
            expect(page.locator('#source-unavailable')).not_to_be_visible()
            choose_many(page,'#futures-form [name=code]','a00.DF')
            form=page.locator('#futures-form')
            expect(form.locator('[name=mapping_mode]')).to_have_value('current')
            expect(form.locator('[name=start]')).not_to_be_visible()
            form.locator('button[type=submit]').click()
            expect(page.locator('#futures-rows')).to_contain_text('豆一2611')
            expect(page.locator('#futures-rows')).to_contain_text('2026-09-21')
            expect(page.locator('#report-chart')).not_to_be_visible()
            page.locator('#qmt-reference-test').click()
            expect(page.locator('#qmt-reference-results')).to_contain_text('可用')
            for width in (1440,1024,768,390):
                page.set_viewport_size({'width':width,'height':980})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=f'output/playwright/qmt-mapping-{width}.png',full_page=True)
            page.set_viewport_size({'width':1440,'height':980})
            with page.expect_response(lambda r:r.url.endswith('/futures/sync') and r.request.method=='POST') as response:sync_report(page)
            assert response.value.status==200,response.value.text()
            job=response.value.json();assert job['payload']['source']=='qmt' and job['payload']['account_id'] is None
            app.worker.execute(app.store.job(job['id']))
            navigate(page,'mapping');form.locator('[name=mapping_mode]').select_option('history')
            form.locator('[name=start]').fill('2026-09-18');form.locator('[name=end]').fill('2026-09-20');form.locator('button[type=submit]').click()
            expect(page.locator('#futures-units')).to_contain_text('每日上游映射')
            expect(page.locator('#futures-rows')).to_contain_text('2026-09-18')
            expect(page.locator('#report-chart canvas')).to_be_visible()
            assert page.locator('#report-chart canvas').first.evaluate('(c)=>{const p=c.getContext("2d").getImageData(0,0,c.width,c.height).data;let n=0;for(let i=3;i<p.length;i+=4)if(p[i])n++;return n>100;}')
            navigate(page,'calendar')
            form.locator('[name=start]').fill('2026-09-18');form.locator('[name=end]').fill('2026-09-20');form.locator('button[type=submit]').click()
            expect(page.locator('#futures-rows')).to_contain_text('休市')
            expect(page.locator('#report-calendar')).to_contain_text('未知')
            page.locator('#calendar-layout').select_option('month');expect(page.locator('#futures-rows')).not_to_be_visible()
            page.locator('#calendar-layout').select_option('list');expect(page.locator('#report-calendar')).not_to_be_visible()
            page.locator('#calendar-layout').select_option('both');expect(page.locator('#futures-rows')).to_be_visible()
            for width in (1440,1024,768,390):
                page.set_viewport_size({'width':width,'height':980})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=f'output/playwright/qmt-calendar-{width}.png',full_page=True)
            page.locator('#data-source').select_option('tushare')
            expect(page.locator('#qmt-reference-test')).not_to_be_visible()
            navigate(page,'mapping');expect(page.locator('#futures-mapping-mode')).not_to_be_visible()
            navigate(page,'accounts');page.locator('#account-new').click()
            expect(page.locator('#account-editor')).to_be_visible()
            page.locator('#account-form [name=name]').fill('未保存的输入')
            page.evaluate("Workspace.navigate('accounts')")
            expect(page.locator('#account-editor')).to_be_visible()
            expect(page.locator('#account-form [name=name]')).to_have_value('未保存的输入')
            assert not errors,errors
            browser.close()
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.browser
def test_native_mapping_and_observed_calendar_are_honest_in_browser(qmt_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=qmt_app;app.source=app.worker.source=NativeSource()
    run(app,choice());run(app,dict(choice('calendar'),start='2026-09-14'))
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#calendar')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible()
            form=page.locator('#futures-form');form.locator('[name=start]').fill('2026-09-14');form.locator('[name=end]').fill('2026-09-20')
            form.locator('button[type=submit]').click()
            expect(page.locator('#futures-rows')).to_contain_text('K线日期观察')
            expect(page.locator('#futures-rows')).not_to_contain_text('休市')
            expect(page.locator('#report-calendar')).to_contain_text('未知')
            page.locator('#qmt-reference-test').click()
            expect(page.locator('#qmt-reference-results')).to_contain_text('有限可用')
            expect(page.locator('#qmt-reference-results')).not_to_contain_text('更新模型')
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':980})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=f'output/playwright/qmt-observed-calendar-{width}.png',full_page=True)
            navigate(page,'mapping');choose_many(page,'#futures-form [name=code]','a00.DF');form.locator('button[type=submit]').click()
            expect(page.locator('#futures-rows')).to_contain_text('豆一2611')
            assert not errors,errors
            browser.close()
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
