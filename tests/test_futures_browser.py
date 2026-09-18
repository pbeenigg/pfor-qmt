import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.server import HTTPServer, handler_for, start_websocket
from test_futures_extensions import extension_app
from browser_helpers import choose_many


@pytest.mark.browser
@pytest.mark.postgres
def test_reports_and_extended_periods_browser(extension_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':
        pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=extension_app
    app.capabilities['main']={'capabilities':{'minutes':{'state':'permission'}}}
    server=HTTPServer(('127.0.0.1',0),handler_for(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    websocket=start_websocket(app,0,server.server_port);app.ws_port=websocket.socket.getsockname()[1]
    app.worker.start();app.tushare_worker.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1440,'height':980})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#history')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key)
            page.locator('#login-form button').click();expect(page.locator('#login-dialog')).not_to_be_visible()
            page.locator('#data-source').select_option('tushare')
            expect(page.locator('#dataset-form [value="1w"]')).to_be_enabled()
            expect(page.locator('#dataset-form [value="60m"]')).to_be_disabled()
            page.locator('nav [data-view=futures]').click()
            form=page.locator('#futures-form')
            choose_many(page,'#futures-form [name=exchange]','SHFE')
            for resource in ('calendar','warehouse','holding','mapping'):
                page.locator('nav [data-view=futures]').click()
                choose_many(page,'#futures-form [name=resource]',resource)
                form.locator('[name=start]').fill('2026-09-14');form.locator('[name=end]').fill('2026-09-14')
                if resource in ('warehouse','holding'): choose_many(page,'#futures-form [name=symbol]','SHFE:CU')
                if resource=='mapping': choose_many(page,'#futures-form [name=code]','CU.SHF')
                with page.expect_response(lambda r:r.url.endswith('/futures/sync') and r.request.method=='POST') as response:
                    page.locator('#futures-sync').click()
                assert response.value.status==200,response.value.text()
                job=response.value.json()
                expect(page.locator('#job-rows tr').filter(has_text=job['id'][:8])).to_contain_text('已完成',timeout=15000)
                page.locator('nav [data-view=futures]').click()
                form.locator('button[type=submit]').click()
                expect(page.locator('#futures-rows tr')).to_have_count(1)
                expect(page.locator('#futures-rows')).to_contain_text('2026-09-14')
                if resource=='warehouse':
                    expect(page.locator('#futures-rows')).to_contain_text('测试仓库')
                    page.screenshot(path=str(output/'futures-warehouse-desktop.png'),full_page=True)
                if resource=='mapping':
                    page.locator('[data-mapped-history="CU2610.SHF"]').click()
                    expect(page.locator('#history-form [name=code]')).to_have_value('CU2610.SHF')
                    page.locator('nav [data-view=futures]').click()
                with page.expect_response(lambda r:r.url.endswith('/futures/export') and r.request.method=='POST') as exported:
                    page.locator('#futures-csv').click()
                assert exported.value.status==200
                export_id=exported.value.json()['id']
                download=page.locator('#job-rows tr').filter(has_text=export_id[:8]).locator('[aria-label="下载文件"]')
                expect(download).to_be_visible(timeout=15000)
                with page.expect_download() as file: download.click()
                assert 'tushare' in Path(file.value.path()).read_text('utf-8-sig')
            page.locator('nav [data-view=futures]').click()
            choose_many(page,'#futures-form [name=resource]','holding')
            form.locator('[name=scope]').select_option('contract')
            page.locator('[data-picker=report]').click();page.locator('#picker-search').fill('CU2610')
            page.locator('#picker-rows [value="CU2610.SHF"]').check();page.locator('#picker-apply').click()
            with page.expect_response(lambda r:r.url.endswith('/futures/sync') and r.request.method=='POST') as response:
                page.locator('#futures-sync').click()
            assert response.value.json()['payload']['symbol']=='CU2610'
            page.locator('nav [data-view=futures]').click()
            page.set_viewport_size({'width':390,'height':844})
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'futures-holding-mobile.png'),full_page=True)
            page.locator('nav [data-view=history]').click()
            page.locator('#dataset-form [name=name]').fill('铜周月')
            page.locator('[data-picker=dataset]').click();page.locator('#picker-search').fill('CU2610')
            page.locator('#picker-rows [value="CU2610.SHF"]').check();page.locator('#picker-apply').click()
            page.locator('#dataset-form [value="1d"]').uncheck();page.locator('#dataset-form [value="1w"]').check();page.locator('#dataset-form [value="1mo"]').check()
            page.locator('#dataset-form button[type=submit]').click()
            expect(page.locator('#datasets')).to_contain_text('铜周月')
            page.locator('#datasets [data-download]').click()
            expect(page.locator('#download-form input[value="1w"]')).to_be_checked()
            expect(page.locator('#download-form input[value="1mo"]')).to_be_enabled()
            page.locator('#download-form [name=start]').fill('2026-09-14');page.locator('#download-form [name=end]').fill('2026-09-17')
            page.locator('#download-form button.primary').click()
            expect(page.locator('#job-rows')).to_contain_text('待核验',timeout=15000)
            page.locator('nav [data-view=history]').click()
            choose_many(page,'#history-form [name=period]','1w')
            page.locator('#history-form [name=start]').fill('2026-09-14');page.locator('#history-form [name=end]').fill('2026-09-17')
            page.locator('#history-form button.primary').click()
            expect(page.locator('#bars')).to_contain_text('周期未结束')
            expect(page.locator('#bars')).to_contain_text('2026-09-17')
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'futures-weekly-mobile.png'),full_page=True)
            page.locator('#data-source').select_option('qmt')
            expect(page.locator('nav [data-view=futures]')).to_be_hidden()
            expect(page.locator('#dataset-form [value="1w"]')).to_be_disabled()
            assert not errors
            browser.close()
    finally:
        app.worker.stop.set();app.tushare_worker.stop.set()
        app.worker.thread.join(10);app.tushare_worker.thread.join(10)
        websocket.shutdown();server.shutdown();server.server_close();thread.join()
