import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.server import HTTPServer, handler_for
from test_console import console_app
from test_settlement_weekly import report_app
from browser_helpers import choose_many


@pytest.mark.browser
def test_console_navigation_editing_history_and_responsive(console_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=console_app;server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#market')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible();expect(page.locator('#view-title')).to_have_text('证券目录')
            assert not errors,errors
            page.locator('nav [data-route=datasets]').click();page.get_by_role('button',name='新建数据集',exact=True).click()
            page.locator('#dataset-form [name=name]').fill('界面数据集')
            page.locator('[data-picker=dataset]').click();expect(page.locator('#picker-rows input[value="000001.SZ"]')).to_be_visible();page.locator('#picker-rows input[value="000001.SZ"]').check();page.locator('#picker-apply').click()
            page.locator('#dataset-form [type=submit]').click();expect(page.locator('#dataset-editor')).not_to_be_visible();expect(page.locator('#datasets')).to_contain_text('界面数据集')
            with page.expect_response(lambda response:'/console/datasets/' in response.url) as toggled:
                page.locator('#datasets [data-schedule]').check()
            assert toggled.value.status==200 and toggled.value.json()['scheduled'] is True
            expect(page.locator('#datasets [data-schedule]')).to_be_checked()
            page.locator('#datasets [data-edit-dataset]').click();page.locator('#dataset-form [name=name]').fill('修改后的数据集');page.locator('#dataset-form [type=submit]').click()
            expect(page.locator('#datasets')).to_contain_text('修改后的数据集')
            for route in ('collect','jobs','exports','maintenance','overview','quality','events','runtime','accounts','qmt','database','access','history'):
                page.locator(f'nav [data-route="{route}"]').click();expect(page).to_have_url(f'http://127.0.0.1:{server.server_port}/#{route}')
                assert page.locator('.view:visible').count()==1
            for width in (1440,1024,768,390):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(output/f'console-history-{width}.png'),full_page=True)
            assert not errors,errors
            browser.close()
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.browser
def test_copy_catalog_maintenance_keeps_selected_exchanges(report_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=report_app
    original=app.dispatch('POST','/console/maintenance',dict(name='上期所目录',kind='catalog',payload={'source':'tushare','account_id':'main','kinds':['future'],'exchanges':['SHFE']}))
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980})
            page.goto(f'http://127.0.0.1:{server.server_port}/#maintenance')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible();page.locator('#data-source').select_option('tushare')
            page.locator('[data-plan=copy]').click()
            expect(page.locator('#maintenance-dialog')).to_be_visible()
            assert page.locator('#maintenance-exchanges').evaluate('el=>[...el.selectedOptions].map(o=>o.value)')==['SHFE']
            with page.expect_response(lambda response:response.url.endswith('/console/maintenance') and response.request.method=='POST') as response:
                page.locator('#maintenance-form [type=submit]').click()
            assert response.value.status==200,response.value.text()
            copy=response.value.json()
            assert copy['payload']['exchanges']==['SHFE'] and not copy['enabled'] and copy['id']!=str(original['id'])
            assert app.store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==0
            browser.close()
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.browser
def test_console_populated_charts_exports_and_report_scopes(report_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=report_app;store=app.store
    for resource,start,choice in [('settle','2026-09-14',{'code':'CU2610.SHF'}),('weekly_detail','2019-03-01',{'symbol':'CU'})]:
        job=app.dispatch('POST','/futures/sync',dict(source='tushare',resource=resource,start=start,end=start,exchange='SHFE',**choice));app.tushare_worker.execute(store.job(job['id']))
    with store.connect() as conn:
        conn.execute("INSERT INTO bars(code,source,period,time,open,high,low,close,volume,open_interest,amount) SELECT 'CU2610.SHF','tushare','1m','2026-09-01'::timestamptz + n*interval '1 minute',80000+n,80010+n,79990+n,80005+n,1,100+n,1000 FROM generate_series(0,5100) n")
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright'
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#settle')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click();expect(page.locator('#login-dialog')).not_to_be_visible()
            page.locator('#data-source').select_option('tushare')
            choose_many(page,'#futures-form [name=exchange]','SHFE')
            page.locator('[data-picker=report]').click();page.locator('#picker-search').fill('CU2610.SHF');page.locator('#picker-rows input[value="CU2610.SHF"]').check();page.locator('#picker-apply').click()
            for key in ('start','end'):page.locator(f'#futures-form [name={key}]').fill('2026-09-14')
            page.locator('#futures-form [type=submit]').click();expect(page.locator('#futures-rows')).to_contain_text('CU2610.SHF');expect(page.locator('#report-summary')).to_contain_text('共 1 条')
            page.locator('#futures-rows [data-record]').click();expect(page.locator('#record-fields')).to_contain_text('80123.1234567890123456789');page.keyboard.press('Escape')
            for width in (1440,1024,768,390):
                page.set_viewport_size({'width':width,'height':980});page.screenshot(path=str(output/f'console-settle-{width}.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),page.evaluate('({width:innerWidth,overflow:[...document.querySelectorAll("body *")].filter(el=>el.getBoundingClientRect().right>innerWidth+1&&!el.closest(".table-wrap,dialog,[hidden]")).map(el=>[el.id,el.className,el.getBoundingClientRect().width]).slice(0,15)})')
            page.set_viewport_size({'width':1440,'height':980});page.locator('nav [data-route=weekly_detail]').click()
            choose_many(page,'#futures-form [name=symbol]','SHFE:CU')
            for key in ('start','end'):page.locator(f'#futures-form [name={key}]').fill('2019-03-01')
            page.locator('#futures-form [type=submit]').click();expect(page.locator('#futures-rows')).to_contain_text('20199');expect(page.locator('#report-chart canvas')).to_be_visible()
            page.locator('nav [data-route=history]').click();page.locator('[data-picker=history]').click();page.locator('#picker-search').fill('CU2610.SHF');page.locator('#picker-rows input[value="CU2610.SHF"]').check();page.locator('#picker-apply').click()
            choose_many(page,'#history-form [name=period]','1m');page.locator('#history-form [name=start]').fill('2026-09-01');page.locator('#history-form [name=end]').fill('2026-09-05');page.locator('#history-form button.primary').click()
            expect(page.locator('#history-summary')).to_contain_text('5101 条');expect(page.locator('#chart-window-note')).to_contain_text('1000 / 共 5101')
            assert page.locator('#bars tr').count()==50
            page.locator('#chart-window').select_option('5000');expect(page.locator('#chart-window-note')).to_contain_text('5000 / 共 5101');page.locator('#chart-selection button').first.click();expect(page.locator('#chart-window-note')).to_contain_text('101 / 共 5101')
            page.screenshot(path=str(output/'console-history-data.png'),full_page=True)
            assert page.locator('#chart canvas').evaluate('(canvas)=>{const a=canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data;let n=0;for(let i=3;i<a.length;i+=4)if(a[i]>0)n++;return n>1000;}')
            page.locator('#history-form [name=start]').fill('2026-09-03')
            page.locator('#next-page').click();expect(page.locator('#page-status')).to_contain_text('51–100')
            page.locator('#prev-page').click();expect(page.locator('#page-status')).to_contain_text('1–50')
            expect(page.locator('#history-summary')).to_contain_text('5101 条')
            page.locator('#export-csv').click();expect(page.locator('#batch-summary')).to_contain_text('2026-09-01 至 2026-09-05');page.locator('#batch-confirm button').filter(has_text='取消').click()
            page.locator('nav [data-route=jobs]').click()
            page.locator('#job-batch').get_by_role('button',name='选择本页',exact=True).click()
            assert page.locator('[data-select-job]:checked').count()==2
            page.locator('#job-batch').get_by_role('button',name='清空选择',exact=True).click()
            expect(page.locator('#job-selection-count')).to_have_text('已选 0 个')
            assert page.locator('[data-select-job]:checked').count()==0
            assert not errors,errors
            browser.close()
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
