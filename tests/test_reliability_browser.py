import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.server import HTTPServer, handler_for, start_websocket
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from test_storage_tasks import Source, make_job


@pytest.mark.browser
@pytest.mark.postgres
def test_operations_quality_retry_logs_and_maintenance(store,tmp_path):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':
        pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    source=Source();source.fail=ValueError('bad response')
    app=Application(Settings(config_path=tmp_path/'config.toml'),store,source)
    job=make_job(store);app.worker.execute(job)
    source.fail=None
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    websocket=start_websocket(app,0,server.server_port);app.ws_port=websocket.socket.getsockname()[1]
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#operations')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            expect(page.locator('#operations')).to_be_visible()
            expect(page.locator('#attention-rows')).to_contain_text('bad response')
            expect(page.locator('#event-rows')).to_contain_text('INVALID_DATA')
            page.locator('#attention-rows [data-job-detail]').click()
            expect(page.locator('#record-extra')).to_contain_text('下一步')
            page.locator('[data-units]').click()
            expect(page.locator('#unit-rows')).to_contain_text('已拒绝')
            with page.expect_response(lambda response:response.url.endswith('/retry')) as response:
                page.locator('[data-retry-unit]').click()
            child=response.value.json()
            assert child['parent_id']==str(job['id'])
            app.worker.execute(store.job(child['id']))
            page.locator('#refresh-jobs').click()
            expect(page.locator('#job-rows')).to_contain_text('已完成')
            page.locator(f'#job-rows [data-job-detail="{job["id"]}"]').click()
            page.locator('[data-maintain]').click()
            page.locator('#maintenance-form [name=name]').fill('日线自动维护')
            page.locator('#maintenance-form button[type=submit]').click()
            expect(page.locator('#maintenance-rows')).to_contain_text('日线自动维护')
            page.locator('[data-maintenance-id]').click()
            expect(page.locator('#maintenance-rows')).to_contain_text('已停用')
            page.locator('#event-filter [name=job_id]').fill(str(job['id']))
            page.locator('#event-filter button[type=submit]').click()
            expect(page.locator('#event-rows')).to_contain_text('INVALID_DATA')
            page.locator('#event-rows [data-record]').first.click()
            expect(page.locator('#record-extra')).to_contain_text('上下文')
            page.keyboard.press('Escape')
            for label,width,height in [('desktop',1440,980),('mobile',390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                page.screenshot(path=str(output/f'reliability-{label}.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                expect(page.locator('#event-filter button[type=submit]')).to_be_visible()
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);websocket.shutdown()
