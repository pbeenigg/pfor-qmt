import pytest
from playwright.sync_api import expect

from browser_helpers import navigate
from test_console_navigation import navigation_page
from test_settlement_weekly import report_app


@pytest.mark.browser
def test_minute_dataset_select_all_and_period_change(navigation_page, report_app):
    page=navigation_page;store=report_app.store
    store.save_security('CU.SHF','沪铜主力','future',{},source='tushare',subtype='continuous')
    page.locator('#data-source').select_option('tushare');navigate(page,'datasets')
    page.get_by_role('button',name='新建数据集',exact=True).click()
    page.locator('#dataset-form [name=name]').fill('全部月份分钟')
    page.locator('#dataset-form [name=period][value="1d"]').uncheck()
    page.locator('#dataset-form [name=period][value="1m"]').check()
    page.locator('[data-picker=dataset]').click()
    expect(page.locator('#picker-constraint')).to_contain_text('具体月份')
    page.locator('#picker-select-matched').click()
    expect(page.locator('#picker-count')).to_contain_text('已选 1 个')
    page.locator('#picker-apply').click()
    page.locator('#dataset-form button[type=submit]').click()
    expect(page.locator('#dataset-editor')).not_to_be_visible()
    saved=store.query("SELECT * FROM datasets WHERE name='全部月份分钟'",one=True)
    assert saved['members']==['CU2610.SHF'] and saved['periods']==['1m']
    page.get_by_role('button',name='新建数据集',exact=True).click()
    page.locator('[data-picker=dataset]').click();page.locator('#picker-select-matched').click()
    expect(page.locator('#picker-count')).to_contain_text('已选 2 个');page.locator('#picker-apply').click()
    page.locator('#dataset-form [name=period][value="5m"]').check()
    expect(page.locator('#dataset-members-warning')).to_contain_text('CU.SHF')
    assert 'CU.SHF' in page.locator('#dataset-form [name=members]').input_value()
    page.locator('#dataset-remove-incompatible').click()
    expect(page.locator('#dataset-form [name=members]')).to_have_value('CU2610.SHF')
    expect(page.locator('#dataset-members-warning')).not_to_be_visible()


@pytest.mark.browser
def test_list_drafts_do_not_change_pagination_or_select_all(navigation_page, report_app):
    page=navigation_page
    for i in range(62):report_app.store.create_job('export',{'source':'qmt','format':'csv'})
    navigate(page,'jobs');expect(page.locator('#job-rows tr')).to_have_count(50)
    page.locator('#job-filter [name=search]').fill('尚未查询的不存在名称')
    page.locator('#job-next').click()
    expect(page.locator('#job-rows tr')).to_have_count(13)
    expect(page.locator('#job-filter .query-state')).to_contain_text('尚未应用')
    page.locator('#job-batch').get_by_role('button',name='选择全部匹配',exact=True).click()
    expect(page.locator('#job-selection-count')).to_have_text('已选 63 个')
    page.locator('#job-filter button[type=submit]').click()
    expect(page.locator('#job-rows')).to_contain_text('没有匹配')
    page.locator('#job-filter [name=search]').fill('')
    page.locator('#job-filter button[type=submit]').click()
    expect(page.locator('#job-rows tr')).to_have_count(50)


def test_task_context_names_filter_and_minute_error(report_app):
    store=report_app.store
    dataset=store.create_dataset(dict(name='铜合约分钟',source='tushare',members=['CU2610.SHF'],periods=['1m']))
    job=store.create_job('download',dict(source='tushare',account_id='main',dataset_id=str(dataset['id']),members=['CU2610.SHF'],periods=['1m']))
    store.create_job('download',dict(source='qmt',members=['000300.SH']))
    page=store.jobs_page({'sources':['tushare'],'search':'铜合约分钟','account_ids':['main'],'origins':['manual']})
    assert page['total']==1 and page['rows'][0]['id']==job['id']
    assert page['rows'][0]['dataset_name']=='铜合约分钟'
    from pfor_qmt.analytics import operations_summary
    assert operations_summary(store,{'search':'铜合约分钟'})['total']==page['total']
    store.save_security('CU.SHF','沪铜主力','future',{},source='tushare',subtype='continuous')
    with pytest.raises(ValueError,match='CU.SHF'):
        store.create_dataset(dict(name='错误范围',source='tushare',members=['CU2610.SHF','CU.SHF'],periods=['1m']))


@pytest.mark.browser
def test_job_context_export_scope_and_failed_query_keep_results(navigation_page, report_app):
    page=navigation_page;store=report_app.store
    dataset=store.query("SELECT * FROM datasets WHERE source='tushare' LIMIT 1",one=True)
    store.create_job('download',dict(source='tushare',account_id='main',dataset_id=str(dataset['id']),members=['CU2610.SHF'],periods=['1m'],start='2026-09-01',end='2026-09-02'))
    page.locator('#data-source').select_option('tushare');navigate(page,'jobs')
    expect(page.locator('#job-rows')).to_contain_text('Tushare测试集')
    expect(page.locator('#job-rows')).to_contain_text('main')
    expect(page.locator('#job-rows')).to_contain_text('1 分钟')
    expect(page.locator('#job-rows')).to_contain_text('手动创建')
    page.locator('#job-filter [name=search]').fill('不可用筛选')
    page.route('**/api/v1/jobs/query',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"查询暂不可用"}'))
    page.locator('#job-filter button[type=submit]').click()
    expect(page.locator('#job-filter .query-state')).to_contain_text('查询失败')
    expect(page.locator('#job-rows')).to_contain_text('Tushare测试集')
    page.unroute('**/api/v1/jobs/query')
    navigate(page,'exports');expect(page.locator('#job-rows tr')).to_have_count(1)
    page.locator('#job-filter button[type=reset]').click()
    expect(page.locator('#job-rows tr')).to_have_count(1)
    expect(page.locator('#job-rows')).not_to_contain_text('历史回补')
    from pathlib import Path
    out=Path('output/playwright');out.mkdir(parents=True,exist_ok=True)
    navigate(page,'jobs')
    page.locator('#job-filter button[type=reset]').click()
    expect(page.locator('#job-rows')).to_contain_text('Tushare测试集')
    for width in (1440,1024,768,390):
        page.set_viewport_size({'width':width,'height':980})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(out/f'job-context-{width}.png'),full_page=True)


@pytest.mark.browser
def test_damaged_preferences_and_columns_leave_empty_state_visible(navigation_page):
    page=navigation_page
    page.evaluate("localStorage.setItem('pfor:queries:job-filter:jobs:qmt','broken-json')")
    page.reload();navigate(page,'jobs')
    tools=page.locator('#job-filter + .query-options')
    tools.locator('summary').first.click()
    tools.locator('[data-saved]').focus()
    tools.locator('.column-menu summary').click()
    expect(tools.locator('[data-column="0"]')).to_be_disabled()
    tools.locator('[data-column="1"]').uncheck()
    page.locator('#job-filter [name=search]').fill('没有此任务')
    page.locator('#job-filter button[type=submit]').click()
    expect(page.locator('#job-rows .empty')).to_be_visible()
    expect(page.locator('#job-rows .empty')).to_contain_text('没有匹配')


@pytest.mark.browser
def test_late_catalog_load_cannot_replace_submitted_search(navigation_page):
    page=navigation_page;navigate(page,'jobs')
    catalog=[];search=[];queries=[]
    page.route('**/api/v1/catalog?*',lambda route:catalog.append(route))
    def hold_search(route):
        queries.append(route.request.url)
        if 'search=cu2610' in route.request.url:search.append(route)
        else:route.continue_()
    page.route('**/api/v1/catalog/securities?*',hold_search)
    navigate(page,'catalog')
    page.locator('#security-form [name=search]').fill('cu2610')
    page.locator('#security-form button[type=submit]').click()
    page.wait_for_timeout(100)
    assert catalog and search
    before=len(queries)
    for route in catalog:route.fulfill(response=route.fetch())
    expect(page.locator('#view-loading')).not_to_be_visible()
    assert len(queries)==before,queries
    for route in search:route.fulfill(response=route.fetch())
    expect(page.locator('#securities tr')).to_have_count(1)
    expect(page.locator('#securities')).to_contain_text('cu2610.SF')
