import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from browser_helpers import choose_many, navigate
from pfor_qmt.exchange_holidays import save_notice
from pfor_qmt.server import HTTPServer, handler_for
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from test_exchange_holidays import notice
from test_storage_tasks import Source


@pytest.mark.browser
def test_holiday_filters_versions_sync_cancel_maintenance_and_mobile(store,tmp_path):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=Application(Settings(config_path=tmp_path/'config.toml'),store,Source())
    save_notice(store,'SHFE',notice())
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    output=Path(__file__).resolve().parents[1]/'output'/'playwright';output.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            navigate(page,'holidays');expect(page.locator('#holiday-rows')).to_contain_text('上海期货交易所')
            expect(page.locator('.source-toolbar')).not_to_be_visible()
            choose_many(page,'#holiday-exchanges','SHFE');choose_many(page,'#holiday-years','2026')
            page.locator('#holiday-filter button[type=submit]').click()
            expect(page.locator('#holiday-status tr')).to_have_count(1)
            page.locator('#holiday-sync').click();expect(page.locator('#batch-confirm')).to_be_visible()
            page.locator('#batch-confirm [data-close-dialog]').last.click()
            assert not store.jobs_page(dict(sources=['exchange']))['rows']
            page.locator('#holiday-sync').click();page.locator('#batch-submit').click()
            expect(page.locator('#holiday-jobs')).to_contain_text('排队中')
            page.get_by_role('button',name='取消公告任务',exact=True).click()
            expect(page.locator('#holiday-jobs')).to_contain_text('已取消')
            page.get_by_role('button',name='公告任务详情',exact=True).click()
            expect(page.locator('#task-body')).to_contain_text('交易所公告')
            expect(page.locator('#task-body')).not_to_contain_text('本地行情桥')
            page.locator('#task-back').click()
            page.locator('#holiday-rows [data-notice]').first.click()
            expect(page.locator('#record-fields')).to_contain_text('公告正文')
            for kind,width,height in [('desktop',1440,980),('mobile',390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('#record-dialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                page.screenshot(path=str(output/f'holiday-details-{kind}.png'),full_page=True)
            page.keyboard.press('Escape');page.set_viewport_size({'width':1440,'height':980})
            page.locator('#holiday-plan-new').click();expect(page.locator('#holiday-plan-scope')).to_contain_text('2026')
            page.locator('#holiday-plan-form button[type=submit]').click()
            expect(page.locator('#holiday-plans')).to_contain_text('已启用')
            page.get_by_role('button',name='停用公告维护',exact=True).click()
            expect(page.locator('#holiday-plans')).to_contain_text('已停用')
            page.get_by_role('button',name='删除公告维护',exact=True).click();page.locator('#batch-submit').click()
            expect(page.locator('#holiday-plans')).to_contain_text('没有保存')
            page.locator('#holiday-plan-filter [name=trash]').select_option('deleted');page.locator('#holiday-plan-filter button').click()
            page.get_by_role('button',name='恢复公告维护',exact=True).click()
            page.locator('#holiday-plan-filter [name=trash]').select_option('active');page.locator('#holiday-plan-filter button').click()
            expect(page.locator('#holiday-plans')).to_contain_text('已停用')
            page.locator('#holiday-filter button[type=reset]').click()
            expect(page.locator('#holiday-status tr')).to_have_count(6)
            for kind,width,height in [('desktop',1440,980),('mobile',390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(output/f'holiday-list-{kind}.png'),full_page=True)
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
