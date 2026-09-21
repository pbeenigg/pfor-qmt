import pytest
from playwright.sync_api import expect

from browser_helpers import navigate, choose_many
from test_console_navigation import navigation_page
from test_settlement_weekly import report_app


@pytest.mark.browser
def test_sort_and_page_size_do_not_apply_draft_filters(navigation_page):
    page=navigation_page;navigate(page,'jobs')
    expect(page.locator('#job-rows [data-job-detail]')).to_have_count(1)
    page.locator('#job-filter [name=search]').fill('尚未提交的筛选')
    tools=page.locator('#job-filter + .query-options');tools.locator('summary').first.click()
    tools.locator('[data-page-size]').select_option('100')
    expect(page.locator('#job-rows [data-job-detail]')).to_have_count(1)
    tools.locator('[data-direction]').select_option('asc')
    expect(page.locator('#job-rows [data-job-detail]')).to_have_count(1)
    expect(page.locator('#job-filter .query-state')).to_contain_text('尚未应用')
    navigate(page,'exports');navigate(page,'jobs')
    expect(tools.locator('[data-page-size]')).to_have_value('100')
    expect(page.locator('#job-filter [name=search]')).to_have_value('尚未提交的筛选')


@pytest.mark.browser
def test_report_reset_keeps_its_page_resource(navigation_page):
    page=navigation_page;page.locator('#data-source').select_option('tushare');navigate(page,'warehouse')
    tools=page.locator('#futures-form + .query-options');tools.locator('summary').first.click()
    tools.get_by_role('button',name='重置筛选',exact=True).click()
    expect(page.locator('#futures-form [name=resource] option:checked')).to_have_attribute('value','warehouse')
    expect(page.locator('#futures-product-field')).to_be_visible()
    expect(page.locator('#futures-form [name=start]')).not_to_have_value('')
    expect(page.locator('#futures-form [name=end]')).not_to_have_value('')


@pytest.mark.browser
def test_report_pages_restore_dependent_product_selection(navigation_page,report_app):
    page=navigation_page
    report_app.store.save_security('A2611.DCE','豆一2611','future',{},subtype='contract',source='tushare',metadata={'product':'A'})
    page.locator('#data-source').select_option('tushare');navigate(page,'warehouse')
    choose_many(page,'#futures-form [name=exchange]','SHFE')
    choose_many(page,'#futures-form [name=symbol]','SHFE:CU')
    page.locator('#futures-form [name=start]').fill('2026-09-01')
    navigate(page,'weekly_detail')
    choose_many(page,'#futures-form [name=exchange]','DCE')
    choose_many(page,'#futures-form [name=symbol]','DCE:A')
    navigate(page,'warehouse')
    expect(page.locator('#futures-form [name=symbol] option:checked')).to_have_attribute('value','SHFE:CU')
    expect(page.locator('#futures-form [name=start]')).to_have_value('2026-09-01')


@pytest.mark.browser
def test_loading_report_options_does_not_restore_over_new_edits(navigation_page):
    page=navigation_page;page.locator('#data-source').select_option('tushare');navigate(page,'holding')
    choose_many(page,'#futures-form [name=exchange]','SHFE')
    choose_many(page,'#futures-form [name=symbol]','SHFE:CU')
    navigate(page,'calendar');pending=[]
    page.route('**/api/v1/futures/options?*',lambda route:pending.append(route))
    navigate(page,'holding')
    page.locator('#futures-form [name=scope]').select_option('contract')
    page.locator('#futures-form [name=start]').fill('2026-08-01')
    assert pending
    for route in pending:route.fulfill(response=route.fetch())
    expect(page.locator('#view-loading')).not_to_be_visible()
    expect(page.locator('#futures-form [name=scope]')).to_have_value('contract')
    expect(page.locator('#futures-form [name=start]')).to_have_value('2026-08-01')


def seed_units(app):
    job=app.store.create_job('download',{'source':'qmt','members':['cu2610.SF','000300.SH'],'periods':['1d']})
    for index in range(7):
        bad=index%2==0
        app.store.write_unit(job['id'],index,dict(code='cu2610.SF' if bad else '000300.SH',period='1d',start='2026-09-01',end='2026-09-02'),
                             'blocked' if bad else 'succeeded',[dict(code='SOURCE_PERMISSION',quality_state='pending_verification',reason='接口权限不足',action='核对账号权限')] if bad else [],error_code='SOURCE_PERMISSION' if bad else None)
    app.store.update_job(job['id'],state='partial')
    return job


def test_unit_filters_counts_pagination_and_event_prefix(report_app):
    app=report_app;job=seed_units(app);path='/jobs/'+str(job['id'])+'/units'
    first=app.dispatch('GET',path,dict(states='blocked',quality_states='pending_verification',search='cu2610',limit=2))
    assert first['total']==4 and [row['unit_index'] for row in first['rows']]==[0,2]
    second=app.dispatch('GET',path,dict(states='blocked',quality_states='pending_verification',search='cu2610',limit=2,offset=first['next_offset']))
    assert [row['unit_index'] for row in second['rows']]==[4,6] and second['next_offset'] is None
    assert app.dispatch('GET',path,{})['total']==7
    with pytest.raises(ValueError):app.dispatch('GET',path,{'quality_states':'unknown'})
    app.store.event(job['id'],'SAMPLE_EVENT','示例合约权限不足')
    events=app.store.events_page(dict(job_id=str(job['id'])[:8],search='示例合约'))
    assert len(events['rows'])==1 and events['rows'][0]['code']=='SAMPLE_EVENT'


@pytest.mark.browser
def test_task_unit_filter_shows_object_and_quality(navigation_page,report_app):
    page=navigation_page;job=seed_units(report_app)
    navigate(page,'jobs');page.locator(f'[data-job-detail="{job["id"]}"]').click()
    page.locator('[data-task-tab=units]').click()
    form=page.locator('#task-unit-filter');form.locator('[name=search]').fill('cu2610')
    form.locator('[type=submit]').click()
    expect(page.locator('#task-unit-count')).to_contain_text('共 4')
    expect(page.locator('#task-records')).to_contain_text('cu2610.SF')
    expect(page.locator('#task-records')).not_to_contain_text('000300.SH')
    expect(page.locator('#task-records')).to_contain_text('待核验')
    from pathlib import Path
    output=Path('output/playwright');output.mkdir(parents=True,exist_ok=True)
    for width in (1440,1024,768,390):
        page.set_viewport_size({'width':width,'height':980})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(output/f'query-context-units-{width}.png'),full_page=True)
    page.set_viewport_size({'width':1440,'height':980})
    choose_many(page,'#task-unit-filter [name=quality_states]','verified')
    form.locator('[type=submit]').click();expect(page.locator('#task-records')).to_contain_text('没有匹配')
    form.locator('[type=reset]').click();expect(page.locator('#task-unit-count')).to_contain_text('共 7')
    page.locator('[data-task-tab=events]').click()
    event=page.locator('#task-event-filter');event.locator('[name=search]').fill('分块未入库');event.locator('[type=submit]').click()
    expect(page.locator('#task-event-count')).to_contain_text('4 条')
    page.locator('#task-refresh').click();expect(page.locator('#task-event-count')).to_contain_text('4 条')


@pytest.mark.browser
def test_export_task_returns_to_export_list(navigation_page):
    page=navigation_page;navigate(page,'exports')
    page.locator('#job-rows [data-job-detail]').click();page.locator('#task-back').click()
    expect(page).to_have_url(__import__('re').compile('#exports$'))


@pytest.mark.browser
def test_history_chart_failure_keeps_successful_table(navigation_page,report_app):
    page=navigation_page;store=report_app.store
    store.query("INSERT INTO bars(instrument_id,code,source,period,time,open,high,low,close) SELECT instrument_id,code,source,'1d','2026-09-14'::timestamptz,1,2,1,2 FROM securities WHERE source='qmt' AND code='cu2610.SF'")
    navigate(page,'history')
    page.locator('[data-picker=history]').click();page.locator('#picker-clear').click()
    page.locator('#picker-search').fill('cu2610.SF');page.locator('#picker-rows input[value="cu2610.SF"]').check();page.locator('#picker-apply').click()
    page.route('**/api/v1/history/chart',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"图表暂不可用"}'))
    page.locator('#history-form button.primary').click()
    expect(page.locator('#bars')).to_contain_text('cu2610.SF')
    expect(page.locator('#history-form .query-state')).to_contain_text('已应用')
    expect(page.locator('#chart-empty')).to_contain_text('图表读取失败')
    page.locator('#data-source').select_option('tushare')
    expect(page.locator('#history-units')).to_have_text('')
    expect(page.locator('#chart-empty')).to_have_text('暂无历史行情')
    expect(page.locator('#next-page')).to_be_disabled()


@pytest.mark.browser
def test_selected_jobs_follow_refreshed_execution_state(navigation_page,report_app):
    page=navigation_page;navigate(page,'jobs')
    selected=page.locator('#job-rows [data-select-job]');selected.check()
    report_app.store.update_job(selected.input_value(),state='cancelled')
    page.locator('#refresh-jobs').click();expect(page.locator('#job-rows')).to_contain_text('已取消')
    page.locator('#job-batch').get_by_role('button',name='取消所选',exact=True).click()
    expect(page.locator('#notice')).to_contain_text('没有允许此操作')
    expect(page.locator('#batch-confirm')).not_to_be_visible()


@pytest.mark.browser
def test_overview_query_recovers_after_database_reconnect(navigation_page,report_app,monkeypatch):
    page=navigation_page;health=report_app.store.health
    monkeypatch.setattr(report_app.store,'health',lambda:{'connected':False})
    navigate(page,'overview');expect(page.locator('#operations-metrics')).to_contain_text('数据库不可用')
    monkeypatch.setattr(report_app.store,'health',health)
    page.locator('#overview-filter [type=submit]').click()
    expect(page.locator('#operations-metrics')).to_contain_text('qmt')
    expect(page.locator('#operations-summary')).to_contain_text('已连接')


@pytest.mark.browser
def test_history_paging_keeps_chart_window(navigation_page,report_app):
    page=navigation_page
    report_app.store.query("INSERT INTO bars(instrument_id,code,source,period,time,open,high,low,close) SELECT instrument_id,code,source,'1d','2026-06-01'::timestamptz+n*interval '1 day',1,2,1,2 FROM securities CROSS JOIN generate_series(0,60) n WHERE source='qmt' AND code='cu2610.SF'")
    navigate(page,'history');page.locator('[data-picker=history]').click();page.locator('#picker-clear').click()
    page.locator('#picker-search').fill('cu2610.SF');page.locator('#picker-rows input[value="cu2610.SF"]').check();page.locator('#picker-apply').click()
    page.locator('#history-form button.primary').click();expect(page.locator('#chart-window-note')).to_contain_text('共 61 条')
    window=page.locator('#chart-window-note').inner_text();requests=[]
    page.on('request',lambda request:requests.append(request.url) if request.url.endswith('/history/chart') else None)
    page.locator('#next-page').click();expect(page.locator('#page-status')).to_have_text('共 61 条 · 51–61')
    assert page.locator('#chart-window-note').inner_text()==window and not requests
