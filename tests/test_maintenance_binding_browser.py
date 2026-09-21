import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from browser_helpers import navigate
from pfor_qmt.server import HTTPServer, handler_for
from test_settlement_weekly import report_app
from test_tushare import account


@pytest.fixture
def maintenance_browser(report_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    server=HTTPServer(('127.0.0.1',0),handler_for(report_app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#jobs')
            page.get_by_label('API Key 或网页登录密码').fill(report_app.settings.api_key)
            page.locator('#login-form button').click();expect(page.locator('#login-dialog')).not_to_be_visible()
            page.locator('#data-source').select_option('tushare')
            yield page
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.browser
def test_old_task_to_maintenance_retains_binding_and_runs_custom_range(report_app,maintenance_browser):
    app=report_app;page=maintenance_browser
    app.settings.accounts.append(account(id='other'));app.settings.default_account_id='other'
    dataset=app.dispatch('POST','/datasets',dict(source='tushare',account_id='main',name='原沪铜',members=['CU2610.SHF'],periods=['1d','5m']))
    job=app.dispatch('POST','/downloads',dict(dataset_id=str(dataset['id']),periods=['1d'],start='2026-09-14',end='2026-09-14'))
    app.store.save_security('A2610.DCE','豆一2610','future',{},source='tushare',subtype='contract')
    app.dispatch('POST','/console/datasets/'+str(dataset['id']),dict(revision=dataset['revision'],members=['A2610.DCE'],account_id='other'))
    page.locator('#refresh-jobs').click();page.locator(f'[data-job-detail="{job["id"]}"]').click()
    page.locator('[data-save-plan]').click();expect(page.locator('#maintenance-dialog')).to_be_visible()
    expect(page.locator('#maintenance-scope-note')).to_contain_text('main')
    page.locator('#maintenance-form [name=name]').fill('保留原任务')
    with page.expect_response(lambda r:r.url.endswith('/console/maintenance') and r.request.method=='POST') as response:
        page.locator('#maintenance-form [type=submit]').click()
    assert response.value.status==200,response.value.text()
    saved=response.value.json();assert saved['payload']['members']==['CU2610.SHF'] and saved['payload']['account_id']=='main' and saved['payload']['periods']==['1d']
    assert not saved['enabled']
    page.locator('[data-plan=run]').click();expect(page.locator('#maintenance-run')).to_be_visible()
    page.locator('#maintenance-run-form [type=submit]').click()
    expect(page.locator('#maintenance-run-form .form-error')).to_contain_text('交易日历不足')
    page.locator('#maintenance-run-form [name=mode]').select_option('custom')
    for key in ('start','end'):page.locator(f'#maintenance-run-form [name={key}]').fill('2026-09-14')
    page.locator('#maintenance-run-form [type=submit]').click();expect(page.locator('#batch-confirm')).to_be_visible()
    expect(page.locator('#batch-summary')).to_contain_text('main')
    before=app.store.query('SELECT count(*) AS n FROM jobs',one=True)['n']
    page.locator('#batch-confirm button').filter(has_text='取消').click()
    assert app.store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==before
    page.locator('#maintenance-run-form [type=submit]').click()
    with page.expect_response(lambda r:r.url.endswith('/run') and r.request.method=='POST') as response:
        page.locator('#batch-submit').click()
    assert response.value.status==200,response.value.text()
    created=response.value.json()['jobs'][0]
    assert created['payload']['account_id']=='main' and created['payload']['members']==['CU2610.SHF']
    assert created['payload']['start']==created['payload']['end']=='2026-09-14'
    app.tushare_worker.execute(app.store.job(created['id']))
    assert app.store.job(created['id'])['state']=='succeeded'
    page.locator('#refresh-jobs').click();expect(page.locator('#job-rows tr').filter(has_text=created['id'][:8])).to_contain_text('已完成')
    assert app.store.query('SELECT last_date FROM maintenance_plans WHERE id=%s',(saved['id'],),one=True)['last_date'] is None


@pytest.mark.browser
def test_calendar_bootstrap_mobile_and_all_cancel_paths(report_app,maintenance_browser):
    app=report_app;page=maintenance_browser
    calendar=app.dispatch('POST','/console/maintenance',dict(name='初始化日历',kind='download',payload={'source':'tushare','resource':'calendar','exchange':'SHFE','account_id':'main'}))
    navigate(page,'maintenance');page.locator('[data-plan=run]').click()
    expect(page.locator('#maintenance-run-form [name=mode]')).to_have_value('custom')
    page.locator('#maintenance-run-form [name=preset]').select_option('3d')
    expected=page.evaluate("dateRangeFor('3d')")
    expect(page.locator('#maintenance-run-form [name=start]')).to_have_value(expected['start'])
    out=Path(__file__).resolve().parents[1]/'output'/'playwright';out.mkdir(parents=True,exist_ok=True)
    for width in (1440,390):
        page.set_viewport_size({'width':width,'height':980})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert page.locator('#maintenance-run').evaluate('el=>el.scrollWidth<=el.clientWidth')
        assert page.locator('#maintenance-run-form [type=submit]').evaluate('el=>el.scrollWidth<=el.clientWidth')
        page.screenshot(path=str(out/f'maintenance-run-{width}.png'),full_page=True)
    page.keyboard.press('Escape');page.set_viewport_size({'width':1440,'height':980})
    page.locator('[data-plan=edit]').click();expect(page.locator('#maintenance-dialog')).to_be_visible()
    page.locator('#maintenance-form [name=name]').fill('不保存的修改')
    for trigger in ('cancel','close','escape'):
        if trigger=='cancel':page.locator('#maintenance-form').get_by_role('button',name='取消',exact=True).click()
        elif trigger=='close':page.locator('#maintenance-dialog > .picker-heading [data-close-dialog]').click()
        else:page.keyboard.press('Escape')
        expect(page.locator('#batch-title')).to_have_text('放弃未保存的修改？')
        page.locator('#batch-confirm button').filter(has_text='取消').click()
        expect(page.locator('#maintenance-form [name=name]')).to_have_value('不保存的修改')
    page.locator('#maintenance-form').get_by_role('button',name='取消',exact=True).click();page.locator('#batch-submit').click()
    expect(page.locator('#maintenance-dialog')).not_to_be_visible()
    assert app.store.query('SELECT name FROM maintenance_plans WHERE id=%s',(calendar['id'],),one=True)['name']=='初始化日历'
    assert app.store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==0
