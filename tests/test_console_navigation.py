import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright,expect

from pfor_qmt.server import HTTPServer,handler_for
from test_settlement_weekly import report_app
from browser_helpers import navigate


@pytest.fixture
def navigation_page(report_app):
    if os.environ.get('PFOR_QMT_BROWSER_TEST')!='1':pytest.skip('PFOR_QMT_BROWSER_TEST=1 enables Chromium')
    app=report_app
    app.store.create_dataset(dict(name='QMT测试集',members=['000300.SH']))
    app.dispatch('POST','/datasets',dict(source='tushare',name='Tushare测试集',members=['CU2610.SHF']))
    app.store.create_job('export',dict(source='qmt',format='csv'))
    app.store.create_job('export',dict(source='tushare',format='csv'))
    server=HTTPServer(('127.0.0.1',0),handler_for(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':980});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#catalog')
            page.get_by_label('API Key 或网页登录密码').fill(app.settings.api_key);page.locator('#login-form button').click()
            expect(page.locator('#login-dialog')).not_to_be_visible()
            yield page
            assert not errors,errors
            browser.close()
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.browser
def test_source_is_global_persistent_and_filters_all_data_pages(navigation_page):
    page=navigation_page
    page.locator('#data-source').select_option('tushare')
    for target in ('datasets','quotes','indices','boards','overview','quality','runtime','jobs','exports','maintenance','accounts','qmt','database','access','settle'):
        navigate(page,target)
        expect(page.locator('#data-source')).to_be_visible()
        expect(page.locator('#data-source')).to_have_value('tushare')
        if target in ('quotes','indices','boards'):expect(page.locator('#source-unavailable')).to_be_visible()
        if target=='datasets':
            expect(page.locator('#datasets')).to_contain_text('Tushare测试集')
            expect(page.locator('#datasets')).not_to_contain_text('QMT测试集')
        if target=='jobs':
            expect(page.locator('#job-rows tr')).to_have_count(1)
            expect(page.locator('#job-rows')).to_contain_text('Tushare')
    page.reload();expect(page.locator('#data-source')).to_have_value('tushare')
    expect(page.locator('#futures')).to_be_visible()
    page.locator('#data-source').select_option('qmt');expect(page.locator('#source-unavailable')).to_be_visible()
    navigate(page,'datasets');expect(page.locator('#datasets')).to_contain_text('QMT测试集');expect(page.locator('#datasets')).not_to_contain_text('Tushare测试集')


@pytest.mark.browser
def test_slow_health_never_disables_navigation_and_runtime_loads_independently(navigation_page):
    page=navigation_page;pending=[]
    page.route('**/api/v1/health*',lambda route:pending.append(route))
    navigate(page,'overview')
    expect(page.locator('nav [data-route=overview]')).to_be_enabled()
    navigate(page,'quality');expect(page.locator('nav [data-route=quality]')).to_be_enabled()
    navigate(page,'runtime');expect(page.locator('nav [data-route=runtime]')).to_be_enabled()
    expect(page.locator('#runtime-rows')).to_contain_text('尚无运行故障日志')
    assert pending
    handled=list(pending)
    for request in handled:request.fulfill(status=503,content_type='application/json',body='{"error":"delayed health failed"}')
    navigate(page,'overview');expect(page.locator('nav [data-route=overview]')).to_be_enabled()
    navigate(page,'datasets');expect(page.locator('#datasets')).to_contain_text('QMT测试集')
    assert page.locator('dialog[open]').count()==0
    for request in pending:
        if request not in handled:request.fulfill(status=503,content_type='application/json',body='{"error":"delayed health failed"}')


@pytest.mark.browser
def test_dataset_action_buttons_fit_and_do_not_overlap(navigation_page):
    page=navigation_page;navigate(page,'datasets')
    out=Path(__file__).resolve().parents[1]/'output'/'playwright';out.mkdir(parents=True,exist_ok=True)
    for width in (1440,768,390):
        page.set_viewport_size({'width':width,'height':980})
        actions=page.locator('#datasets .actions').first
        expect(actions).to_be_visible()
        boxes=actions.locator('button').evaluate_all('(buttons)=>buttons.map(b=>({left:b.getBoundingClientRect().left,right:b.getBoundingClientRect().right,top:b.getBoundingClientRect().top,bottom:b.getBoundingClientRect().bottom,width:b.clientWidth,scroll:b.scrollWidth,name:b.getAttribute("aria-label")||b.title}))')
        assert all(box['scroll']<=box['width'] and box['name'] for box in boxes),boxes
        assert all(a['right']<=b['left'] or a['bottom']<=b['top'] for a,b in zip(boxes,boxes[1:])),boxes
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(out/f'global-source-actions-{width}.png'),full_page=True)


def test_scoped_health_and_logs_do_not_mix_providers(report_app):
    import json
    app=report_app
    for source in ('qmt','tushare'):
        job=app.store.create_job('download',{'source':source})
        app.store.update_job(job['id'],state='failed',error='source unavailable')
        app.store.event(job['id'],'SOURCE_SCOPE',source,context={'source':'tushare' if source=='qmt' else 'qmt'})
        app.store.event(None,'SOURCE_SCOPE',source,context={'source':source})
        (app.settings.runtime/('worker-'+source+'.jsonl')).parent.mkdir(parents=True,exist_ok=True)
        (app.settings.runtime/('worker-'+source+'.jsonl')).write_text(json.dumps({'source':source,'code':'SOURCE_UNAVAILABLE','message':source}),encoding='utf-8')
    for source in ('qmt','tushare'):
        result=app.dispatch('GET','/health',{'source':source,'view':'quality'})
        assert result['attention'] and {row['source'] for row in result['attention']}=={source}
        assert result['scheduled_freshness'] is None
        logs=app.dispatch('GET','/runtime/events',{'source':source})
        assert {row['source'] for row in logs['rows']}=={source}
        events=app.store.events_page({'sources':[source],'code':'SOURCE_SCOPE','limit':1})
        following=app.store.events_page({'sources':[source],'code':'SOURCE_SCOPE','limit':1,'before':events['next_before']})
        assert events['next_before'] and following['next_before'] is None
        assert [row['message'] for row in events['rows']+following['rows']]==[source,source]
        assert events['rows'][0]['id']!=following['rows'][0]['id']
    assert len(app.dispatch('GET','/runtime/events',{})['rows'])==2
