import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright,expect

from browser_helpers import choose_many
from pfor_qmt.server import HTTPServer,handler_for
from test_settlement_weekly import report_app


@pytest.mark.browser
@pytest.mark.postgres
def test_settlement_holding_and_weekly_workspace(report_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=report_app;store=app.store
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            page.locator('#data-source').select_option('tushare');page.locator('nav [data-view=futures]').click()
            choose_many(page,'#futures-form [name=resource]','settle')
            choose_many(page,'#futures-form [name=exchange]','SHFE')
            page.locator('[data-picker=report]').click();page.locator('#picker-search').fill('CU2610.SHF')
            page.locator('#picker-rows input[value="CU2610.SHF"]').check();page.locator('#picker-apply').click()
            for name in ('start','end'):page.locator(f'#futures-form [name={name}]').fill('2026-09-14')
            with page.expect_response(lambda r:r.url.endswith('/futures/sync')) as response:page.locator('#futures-sync').click()
            job=response.value.json();assert response.value.status==200
            app.tushare_worker.execute(store.job(job['id']));assert store.job(job['id'])['state']=='succeeded'
            page.locator('nav [data-view=futures]').click();page.locator('#futures-form button[type=submit]').click()
            expect(page.locator('#futures-rows')).to_contain_text('CU2610.SHF')
            expect(page.locator('#futures-rows')).to_contain_text('0.050')
            page.locator('#futures-rows [data-record]').first.click()
            expect(page.locator('#record-fields')).to_contain_text('平今仓手续率（原值）')
            expect(page.locator('#record-fields')).to_contain_text('未提供')
            for label,width,height in [('desktop',1440,980),('mobile',390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('#record-dialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                page.screenshot(path=str(output/f'settlement-{label}.png'),full_page=True)
            page.keyboard.press('Escape');page.set_viewport_size({'width':1440,'height':980})
            choose_many(page,'#futures-form [name=resource]','weekly_detail')
            expect(page.locator('#futures-contract-field')).not_to_be_visible()
            choose_many(page,'#futures-form [name=symbol]','SHFE:CU')
            for name in ('start','end'):page.locator(f'#futures-form [name={name}]').fill('2019-03-01')
            with page.expect_response(lambda r:r.url.endswith('/futures/sync')) as response:page.locator('#futures-sync').click()
            job=response.value.json();app.tushare_worker.execute(store.job(job['id']))
            assert store.job(job['id'])['result']['rows']==1
            page.locator('nav [data-view=futures]').click();page.locator('#futures-form button[type=submit]').click()
            expect(page.locator('#futures-rows')).to_contain_text('20199')
            expect(page.locator('#futures-rows')).to_contain_text('123412345678.9012345678901234567890')
            page.locator('#futures-rows [data-record]').first.click()
            expect(page.locator('#record-fields')).to_contain_text('原始成交额（亿元）')
            expect(page.locator('#record-fields')).to_contain_text('成交额同比（%）')
            for label,width,height in [('desktop',1440,980),('mobile',390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('#record-dialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                page.screenshot(path=str(output/f'weekly-detail-{label}.png'),full_page=True)
            page.keyboard.press('Escape')
            for fmt in ('csv','parquet'):
                with page.expect_response(lambda r:r.url.endswith('/futures/export')) as response:page.locator('#futures-'+fmt).click()
                export=response.value.json();app.worker.execute(store.job(export['id']))
                page.locator('#refresh-jobs').click()
                expect(page.locator('#job-rows tr').filter(has_text=export['id'][:8])).to_contain_text('已完成')
                page.locator('nav [data-view=futures]').click()
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
