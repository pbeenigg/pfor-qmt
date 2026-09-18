import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.server import HTTPServer, handler_for
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tushare import TushareSource
from test_tushare import account
from test_verification_repair import missing_verification, repaired_response


@pytest.mark.browser
@pytest.mark.postgres
def test_preview_cancel_repair_links_and_reverify(store,tmp_path,monkeypatch):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    original,verification=missing_verification(store,tmp_path,monkeypatch)
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    app.settings.accounts=[account()];app.settings.default_account_id='main'
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#jobs')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            page.locator(f'#job-rows [data-job-detail="{verification["id"]}"]').click()
            expect(page.locator('#record-extra')).to_contain_text('核验前任务')
            page.locator('[data-repair]').click()
            expect(page.locator('#batch-title')).to_have_text('确认从数据源补齐缺口')
            expect(page.locator('#batch-extra')).to_contain_text('2026-09-15 至 2026-09-16')
            expect(page.locator('#batch-summary')).to_contain_text('main')
            for label,width,height in [('desktop',1440,980),('mobile',390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.locator('#batch-confirm').evaluate('(el)=>el.scrollWidth <= el.clientWidth')
                page.screenshot(path=str(output/f'repair-preview-{label}.png'),full_page=True)
            page.locator('#batch-confirm button').filter(has_text='取消').click()
            assert len(store.query('SELECT * FROM jobs'))==2
            page.locator('[data-repair]').click()
            with page.expect_response(lambda response:response.url.endswith('/repair')) as response:
                page.locator('#batch-submit').click()
            created=response.value.json()
            assert response.value.status==200 and created['kind']=='download'
            monkeypatch.setattr(TushareSource,'request',repaired_response)
            app.tushare_worker.execute(store.job(created['id']))
            page.locator('#refresh-jobs').click()
            page.locator(f'#job-rows [data-job-detail="{verification["id"]}"]').click()
            expect(page.locator('#record-extra')).to_contain_text('缺口补数')
            page.locator(f'#record-extra [data-job-detail="{created["id"]}"]').click()
            expect(page.locator('#record-extra')).to_contain_text('补数依据')
            page.locator(f'#record-extra [data-job-detail="{verification["id"]}"]').click()
            expect(page.locator(f'[data-verify="{verification["id"]}"]')).to_be_visible()
            with page.expect_response(lambda response:response.url.endswith('/verify')) as response:
                page.locator(f'[data-verify="{verification["id"]}"]').click()
            recheck=response.value.json();app.worker.execute(store.job(recheck['id']))
            assert store.job(recheck['id'])['state']=='succeeded'
            page.locator('#refresh-jobs').click()
            page.locator(f'#job-rows [data-job-detail="{recheck["id"]}"]').click()
            expect(page.locator('#record-extra')).to_contain_text('校验通过')
            assert store.job(original['id'])==original and store.job(verification['id'])==verification
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
