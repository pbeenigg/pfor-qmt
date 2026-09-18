import os
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from pfor_qmt.server import HTTPServer, handler_for, start_websocket
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.data import SHANGHAI
from test_storage_tasks import Source, make_job


@pytest.mark.browser
@pytest.mark.postgres
def test_operations_quality_retry_logs_and_maintenance(store,tmp_path,monkeypatch):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':
        pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    source=Source();source.fail=ValueError('bad response')
    app=Application(Settings(config_path=tmp_path/'config.toml'),store,source)
    job=make_job(store);app.worker.execute(job)
    source.fail=None
    members=['%06d.SH'%index for index in range(300,351)]
    dataset=store.create_dataset(dict(name='新鲜度范围',members=members,periods=['1d'],scheduled=True,schedule_time='00:00'))
    today=datetime.now(SHANGHAI).date()
    store.query('UPDATE datasets SET schedule_from=%s WHERE id=%s',(today-timedelta(days=1),dataset['id']))
    store.query("INSERT INTO trading_dates(source,market,day,is_open) VALUES('qmt','SH',%s,true)",(today,))
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
            expect(page.locator('#freshness-rows tr')).to_have_count(50)
            expect(page.locator('#freshness-rows')).to_contain_text('尚无数据')
            page.locator('#freshness-next').click()
            expect(page.locator('#freshness-rows tr')).to_have_count(1)
            expect(page.locator('#freshness-rows')).to_contain_text('000350.SH')
            page.locator('#freshness-rows [data-record]').click()
            expect(page.locator('#record-fields')).to_contain_text('DATA_MISSING')
            page.keyboard.press('Escape')
            page.locator('#freshness-filter [name=search]').fill('000301')
            page.locator('#freshness-filter button[type=submit]').click()
            expect(page.locator('#freshness-rows')).to_contain_text('000301.SH')
            expect(page.locator('#freshness-rows tr')).to_have_count(1)
            page.locator('#freshness-filter button[type=reset]').click()
            expect(page.locator('#freshness-rows tr')).to_have_count(50)
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
            page.locator(f'#job-rows [data-job-detail="{child["id"]}"]').click()
            with page.expect_response(lambda response:response.url.endswith('/verify')) as response:
                page.locator('[data-verify]').click()
            verification=response.value.json()
            assert verification['kind']=='verify'
            app.worker.execute(store.job(verification['id']))
            page.locator('#refresh-jobs').click()
            expect(page.locator('#job-rows')).to_contain_text('只读核验')
            page.locator(f'#job-rows [data-job-detail="{verification["id"]}"]').click()
            expect(page.locator('#record-extra')).to_contain_text('coverage-v2')
            page.locator('[data-units]').click()
            page.locator('#unit-rows [data-record]').first.click()
            expect(page.locator('#record-extra')).to_contain_text('核验依据与异常区间')
            page.keyboard.press('Escape')
            page.keyboard.press('Escape')
            page.locator(f'#job-rows [data-job-detail="{job["id"]}"]').click()
            page.locator('[data-maintain]').click()
            page.locator('#maintenance-form [name=name]').fill('日线自动维护')
            page.locator('#maintenance-form button[type=submit]').click()
            expect(page.locator('#maintenance-rows')).to_contain_text('日线自动维护')
            page.locator('[data-maintenance-id]').click()
            expect(page.locator('#maintenance-rows')).to_contain_text('已停用')
            page.locator('[data-maintenance-edit]').click()
            page.locator('#maintenance-form [name=name]').fill('收盘后核对')
            page.locator('#maintenance-form [name=schedule_time]').fill('18:30')
            page.locator('#maintenance-form [name=lookback_days]').fill('3')
            page.locator('#maintenance-form button[type=submit]').click()
            expect(page.locator('#maintenance-rows')).to_contain_text('收盘后核对')
            expect(page.locator('#maintenance-rows')).to_contain_text('18:30')
            expect(page.locator('#maintenance-rows')).to_contain_text('已停用')
            plan=store.query('SELECT * FROM maintenance_plans',one=True)
            assert not plan['enabled'] and plan['lookback_days']==3
            assert store.events_page({'code':'MAINTENANCE_UPDATED'})['rows']
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
            (app.settings.runtime/'worker-qmt.jsonl').write_text(json.dumps(dict(time='2026-09-18T10:00:00Z',source='qmt',lane='download',code='DATABASE_ERROR',message='数据库连接中断',action='恢复数据库后刷新')),encoding='utf-8')
            monkeypatch.setattr(store,'health',lambda:{'connected':False,'message':'database offline'})
            page.locator('#operations-refresh').click()
            expect(page.locator('#operations-summary')).to_contain_text('未连接')
            expect(page.locator('#event-rows')).to_contain_text('暂不可读取')
            expect(page.locator('#runtime-rows')).to_contain_text('DATABASE_ERROR')
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);websocket.shutdown()
