import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from browser_helpers import choose_many
from pfor_qmt.server import HTTPServer, handler_for, start_websocket
from test_batch_workflows import batch_app
from test_futures_extensions import extension_app


@pytest.mark.browser
@pytest.mark.postgres
def test_batch_selection_dates_records_and_jobs(batch_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':
        pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=batch_app;store=app.store
    app.capabilities['main']={'capabilities':{'minutes':{'state':'permission'}}}
    for i in range(63):
        store.save_security(f'T{i}.DCE',f'批量选择{i}','future',{},subtype='contract',source='tushare')
    datasets=[app.dispatch('POST','/datasets',dict(source='tushare',name=name,members=[code],periods=periods))
              for name,code,periods in [('铜日线','CU2610.SHF',['1d']),('豆一日周线','A2610.DCE',['1d','1w'])]]
    server=HTTPServer(('127.0.0.1',0),handler_for(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    websocket=start_websocket(app,0,server.server_port);app.ws_port=websocket.socket.getsockname()[1]
    app.worker.start();app.tushare_worker.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#history')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key)
            page.locator('#login-form button').click();expect(page.locator('#login-dialog')).not_to_be_visible()
            page.locator('#data-source').select_option('tushare')
            page.locator('[data-picker=history]').click()
            page.locator('#picker-search').fill('批量选择')
            expect(page.locator('#picker-page')).to_contain_text('共 63 个')
            page.locator('#picker-select-matched').click()
            expect(page.locator('#picker-count')).to_contain_text('已选 63 个')
            page.locator('#picker-next').click()
            expect(page.locator('#picker-rows input:checked')).to_have_count(13)
            page.locator('#picker-apply').click()
            assert len(page.locator('#history-form [name=code]').input_value().split(','))==63
            page.locator('[data-picker=history]').click();page.locator('#picker-clear').click();page.keyboard.press('Escape')
            assert len(page.locator('#history-form [name=code]').input_value().split(','))==63
            page.locator('[data-picker=history]').click();page.locator('#picker-clear').click()
            choose_many(page,'#picker-market','DF','SF')
            for code in ('CU2610.SHF','A2610.DCE'):
                page.locator('#picker-search').fill(code)
                page.locator(f'#picker-rows [value="{code}"]').check()
            page.locator('#picker-apply').click()
            choose_many(page,'#history-form [name=period]','1d','1w')
            for preset in ('today','3d','1w','3m','6m','1y','2y','3y'):
                page.locator('[data-date-preset=history-form]').select_option(preset)
                expected=page.evaluate('(preset)=>dateRangeFor(preset)',preset)
                expect(page.locator('#history-form [name=start]')).to_have_value(expected['start'])
                expect(page.locator('#history-form [name=end]')).to_have_value(expected['end'])
            assert page.evaluate("dateRangeFor('1y','2024-02-29').start")=='2023-02-28'
            assert page.evaluate("dateRangeFor('3m','2026-05-31').start")=='2026-02-28'
            assert page.evaluate("dateRangeFor('3d','2026-01-01').start")=='2025-12-30'
            page.locator('nav [data-view=jobs]').click()
            choose_many(page,'#download-form [name=dataset_id]',*[str(row['id']) for row in datasets])
            page.locator('#download-form [data-check-all]').check()
            expect(page.locator('#download-form input[value="1d"]')).to_be_checked()
            expect(page.locator('#download-form input[value="1w"]')).to_be_checked()
            page.locator('[data-date-preset=download-form]').select_option('3d')
            for name in ('start','end'):page.locator(f'#download-form [name={name}]').fill('2026-09-14')
            page.locator('#download-form button.primary').click()
            expect(page.locator('#batch-summary')).to_contain_text('铜日线')
            page.locator('#batch-confirm button').filter(has_text='取消').click()
            assert store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==0
            page.locator('#download-form button.primary').click()
            with page.expect_response(lambda r:r.url.endswith('/downloads/batch')) as response:page.locator('#batch-submit').click()
            assert response.value.status==200,response.value.text()
            jobs=response.value.json()['jobs'];assert len(jobs)==2
            for job in jobs:
                expect(page.locator('#job-rows tr').filter(has_text=job['id'][:8])).to_contain_text('待核验' if '1w' in job['payload']['periods'] else '已完成',timeout=15000)
            choose_many(page,'#job-filter [name=states]','completed')
            page.locator('#job-filter button[type=submit]').click()
            expect(page.locator('#job-rows tr')).to_have_count(1)
            expect(page.locator('#job-page')).to_contain_text('共 1 个')
            page.locator('#job-filter button[type=reset]').click()
            expect(page.locator('#job-rows tr')).to_have_count(2)
            page.locator('[data-job-detail]').first.click()
            expect(page.locator('#record-fields')).to_contain_text('任务状态')
            expect(page.locator('#record-extra')).to_contain_text('覆盖与缺口')
            page.keyboard.press('Escape')
            page.locator('nav [data-view=history]').click()
            for name in ('start','end'):page.locator(f'#history-form [name={name}]').fill('2026-09-14')
            page.locator('#history-form button.primary').click()
            expect(page.locator('#bars tr')).to_have_count(3)
            expect(page.locator('#bars')).to_contain_text('A2610.DCE')
            expect(page.locator('#bars')).to_contain_text('CU2610.SHF')
            expect(page.locator('#chart-selection')).to_be_visible()
            page.locator('#chart-code').select_option('CU2610.SHF')
            page.screenshot(path=str(output/'batch-history-desktop.png'),full_page=True)
            page.locator('#bars [data-record]').first.click()
            expect(page.locator('#record-fields')).to_contain_text('成交额')
            page.locator('#record-next').click();expect(page.locator('#record-position')).to_have_text('本页 2 / 3')
            page.keyboard.press('Escape')
            page.locator('#export-csv').click()
            with page.expect_response(lambda r:r.url.endswith('/exports/batch')) as response:page.locator('#batch-submit').click()
            assert len(response.value.json()['jobs'])==2
            page.locator('nav [data-view=futures]').click()
            choose_many(page,'#futures-form [name=resource]','calendar','warehouse','holding','mapping')
            choose_many(page,'#futures-form [name=exchange]','SHFE','DCE')
            choose_many(page,'#futures-form [name=symbol]','SHFE:CU','DCE:A')
            choose_many(page,'#futures-form [name=code]','CU.SHF','A.DCE')
            page.locator('[data-date-preset=futures-form]').select_option('today')
            for name in ('start','end'):page.locator(f'#futures-form [name={name}]').fill('2026-09-14')
            page.locator('#futures-sync').click()
            expect(page.locator('#batch-summary')).to_contain_text('8')
            with page.expect_response(lambda r:r.url.endswith('/futures/sync')) as response:page.locator('#batch-submit').click()
            assert response.value.status==200,response.value.text()
            jobs=response.value.json()['jobs'];assert len(jobs)==4
            for job in jobs:expect(page.locator('#job-rows tr').filter(has_text=job['id'][:8])).to_contain_text('已完成',timeout=15000)
            page.locator('nav [data-view=futures]').click()
            page.locator('#futures-form button[type=submit]').click()
            expect(page.locator('#futures-rows tr')).to_have_count(2)
            for resource in ('warehouse','holding','mapping'):
                page.locator(f'[data-resource-tab={resource}]').click()
                expect(page.locator('#futures-rows tr')).to_have_count(2)
                expect(page.locator('#futures-units')).not_to_be_empty()
            page.locator('[data-resource-tab=warehouse]').click()
            expect(page.locator('#futures-rows')).to_contain_text('测试仓库')
            page.screenshot(path=str(output/'batch-futures-desktop.png'),full_page=True)
            for fmt in ('csv','parquet'):
                page.locator(f'#futures-{fmt}').click()
                with page.expect_response(lambda r:r.url.endswith('/futures/export')) as response:page.locator('#batch-submit').click()
                jobs=response.value.json()['jobs'];assert len(jobs)==4
                for job in jobs:expect(page.locator('#job-rows tr').filter(has_text=job['id'][:8])).to_contain_text('已完成',timeout=15000)
                page.locator('nav [data-view=futures]').click()
            for width,height,label in [(1440,980,'desktop'),(390,844,'mobile'),(768,1024,'tablet')]:
                page.set_viewport_size({'width':width,'height':height})
                page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#futures-form button[type=submit]').click()
                expect(page.locator('#futures-rows tr')).to_have_count(2)
                page.locator('#futures-rows [data-record]').first.click()
                assert not errors,errors
                expect(page.locator('#record-dialog')).to_be_visible()
                expect(page.locator('#record-fields')).to_contain_text('昨日仓单')
                assert page.locator('#record-dialog').evaluate('(el)=>el.scrollWidth <= el.clientWidth')
                page.screenshot(path=str(output/f'batch-details-{label}.png'),full_page=True)
                page.keyboard.press('Escape')
                page.locator('[data-multi-for=futures-form-exchange]').click()
                page.locator('#option-search').fill('商所')
                page.locator('#option-clear').click();page.locator('#option-all').check()
                expect(page.locator('#option-count')).to_contain_text('已选 2 项')
                page.screenshot(path=str(output/f'batch-options-{label}.png'),full_page=True)
                page.keyboard.press('Escape')
                expect(page.locator('[data-multi-for=futures-form-exchange]')).to_be_focused()
            assert not errors,errors
            browser.close()
    finally:
        app.worker.stop.set();app.tushare_worker.stop.set()
        app.worker.thread.join(10);app.tushare_worker.thread.join(10)
        websocket.shutdown();server.shutdown();server.server_close();thread.join()
