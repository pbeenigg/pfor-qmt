import pytest
import psycopg

from test_console import console_app
from test_console_navigation import navigation_page
from test_settlement_weekly import report_app
from browser_helpers import navigate
from playwright.sync_api import expect


def test_dataset_recycle_keeps_history_and_restores_disabled(console_app):
    app=console_app;store=app.store
    dataset=store.create_dataset(dict(name='可回收',members=['000001.SZ'],scheduled=True))
    store.query("INSERT INTO bars(instrument_id,code,source,period,time,open,high,low,close) SELECT instrument_id,code,source,'1d','2026-09-18 00:00:00+08'::timestamptz,1,2,1,2 FROM securities WHERE code='000001.SZ'")
    history=store.history('000001.SZ')['rows']
    url='/console/datasets/'+str(dataset['id'])
    deleted=app.dispatch('POST',url+'/delete',dict(revision=dataset['revision']))
    assert deleted['deleted_at'] and not deleted['scheduled']
    assert history and store.history('000001.SZ')['rows']==history
    assert not app.dispatch('GET','/datasets',{})
    assert app.dispatch('POST','/datasets/query',dict(trash='deleted'))['total']==1
    with pytest.raises(ValueError,match='回收站'):
        app.prepare_download(dict(dataset_id=str(dataset['id'])))
    with pytest.raises(ValueError,match='版本|变更'):
        app.dispatch('POST',url+'/restore',dict(revision=dataset['revision']))
    restored=app.dispatch('POST',url+'/restore',dict(revision=deleted['revision']))
    assert not restored['deleted_at'] and not restored['scheduled']
    assert restored['members']==dataset['members']


def test_recycle_refuses_active_jobs_and_referenced_datasets(console_app):
    app=console_app;store=app.store
    dataset=store.create_dataset(dict(name='范围',members=['000001.SZ']))
    params=dict(revision=dataset['revision'])
    url='/console/datasets/'+str(dataset['id'])+'/delete'
    job=store.create_job('download',dict(dataset_id=str(dataset['id']),members=dataset['members']))
    with pytest.raises(ValueError,match='活动|排队|运行'):app.dispatch('POST',url,params)
    with pytest.raises(ValueError,match='活动|排队|运行'):app.dispatch('POST','/jobs/'+str(job['id'])+'/delete',{})
    store.update_job(job['id'],state='succeeded')
    plan=app.dispatch('POST','/console/maintenance',dict(name='依赖计划',payload=dict(dataset_id=str(dataset['id']))))
    with pytest.raises(ValueError,match='维护'):app.dispatch('POST',url,params)
    deleted_plan=app.dispatch('POST','/console/maintenance/'+str(plan['id'])+'/delete',dict(revision=plan['revision']))
    deleted=app.dispatch('POST',url,params)
    with pytest.raises(ValueError,match='回收站'):
        app.dispatch('POST','/console/maintenance/'+str(plan['id'])+'/restore',dict(revision=deleted_plan['revision']))
    with pytest.raises(psycopg.errors.CheckViolation):
        store.create_job('download',dict(dataset_id=str(dataset['id']),members=dataset['members']))
    assert deleted['deleted_at']


def test_task_recycle_retains_evidence_and_source_filters(console_app):
    app=console_app;store=app.store
    job=store.create_job('download',dict(source='qmt',members=['000001.SZ'],periods=['1d']))
    store.update_job(job['id'],state='failed',error='离线证据')
    store.event(job['id'],'TEST_EVIDENCE','保留事件')
    url='/jobs/'+str(job['id'])
    app.dispatch('POST',url+'/delete',{})
    assert store.jobs_page({})['total']==0
    assert store.jobs_page({'trash':'deleted','sources':['qmt']})['total']==1
    assert store.jobs_page({'trash':'deleted','sources':['tushare']})['total']==0
    assert app.dispatch('GET',url,{})['error']=='离线证据'
    assert store.events_page({'code':'TEST_EVIDENCE'})['rows']
    assert store.operations_health()['attention']
    with pytest.raises(ValueError,match='回收站'):app.dispatch('POST',url+'/retry',{})
    app.dispatch('POST',url+'/restore',{})
    assert store.jobs_page({})['total']==1
    with pytest.raises(ValueError):store.jobs_page({'trash':'anything'})


@pytest.mark.browser
def test_recycle_cancel_confirm_restore_and_active_task_guard(navigation_page,report_app):
    page=navigation_page;store=report_app.store
    navigate(page,'datasets')
    page.locator('#datasets [data-recycle=datasets]').click()
    page.locator('#batch-confirm').get_by_role('button',name='取消',exact=True).click()
    assert store.query('SELECT count(*) AS n FROM datasets WHERE deleted_at IS NOT NULL',one=True)['n']==0
    page.locator('#datasets [data-recycle=datasets]').click();page.locator('#batch-submit').click()
    expect(page.locator('#datasets')).to_contain_text('没有匹配')
    form=page.locator('#datasets-filter');form.locator('[name=trash]').select_option('deleted');form.locator('[type=submit]').click()
    expect(page.locator('#datasets')).to_contain_text('QMT测试集')
    expect(page.locator('#datasets [data-recycle]')).to_have_attribute('aria-label','恢复记录')
    assert page.locator('#datasets [data-download]').count()==0
    from pathlib import Path
    output=Path('output/playwright');output.mkdir(parents=True,exist_ok=True)
    for width in (1440,1024,768,390):
        page.set_viewport_size({'width':width,'height':980})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(output/f'recycle-datasets-{width}.png'),full_page=True)
    page.set_viewport_size({'width':1440,'height':980})
    page.locator('#datasets [data-recycle]').click();page.locator('#batch-submit').click()
    expect(page.locator('#datasets')).to_contain_text('没有匹配')
    form.locator('[name=trash]').select_option('active');form.locator('[type=submit]').click()
    expect(page.locator('#datasets')).to_contain_text('QMT测试集')
    navigate(page,'jobs')
    expect(page.locator('#job-rows [data-recycle]')).to_be_disabled()
    job=store.query("SELECT id FROM jobs WHERE payload->>'source'='qmt'",one=True)
    store.update_job(job['id'],state='cancelled')
    page.locator('#refresh-jobs').click();expect(page.locator('#job-rows [data-recycle]')).to_be_enabled()
    page.locator('#job-rows [data-recycle]').click();page.locator('#batch-submit').click()
    expect(page.locator('#job-rows')).to_contain_text('没有匹配')
    page.locator('#job-filter [name=trash]').select_option('deleted');page.locator('#job-filter [type=submit]').click()
    page.locator('#job-rows [data-job-detail]').click()
    expect(page.locator('#task-body')).to_contain_text('当前只读')
    assert page.locator('#task-body [data-job=retry]').count()==0
    page.locator('#task-body [data-recycle]').click();page.locator('#batch-submit').click()
    expect(page.locator('#task-body')).not_to_contain_text('当前只读')
    dataset=store.query("SELECT * FROM datasets WHERE source='qmt'",one=True)
    report_app.dispatch('POST','/console/maintenance',dict(name='可恢复计划',payload=dict(dataset_id=str(dataset['id']))))
    navigate(page,'maintenance')
    page.locator('#maintenance-rows [data-recycle]').click();page.locator('#batch-submit').click()
    expect(page.locator('#maintenance-rows')).to_contain_text('暂无匹配')
    page.locator('#maintenance-filter [name=trash]').select_option('deleted');page.locator('#maintenance-filter [type=submit]').click()
    expect(page.locator('#maintenance-rows')).to_contain_text('可恢复计划')
    assert page.locator('#maintenance-rows [data-plan]:visible').count()==0
    page.locator('#maintenance-rows [data-recycle]').click();page.locator('#batch-submit').click()
    expect(page.locator('#maintenance-rows')).to_contain_text('暂无匹配')


@pytest.mark.browser
def test_account_filter_and_saved_query_delete(navigation_page):
    page=navigation_page;navigate(page,'accounts')
    form=page.locator('#accounts-filter');form.locator('[name=search]').fill('不存在账号');form.locator('[type=submit]').click()
    expect(page.locator('#account-rows')).to_contain_text('没有匹配')
    form.locator('[type=reset]').click();expect(page.locator('#accounts-filter-count')).to_contain_text('匹配 1')
    page.locator('[data-account-delete=main]').click();expect(page.locator('#batch-confirm')).to_be_visible();page.locator('#batch-confirm').get_by_role('button',name='取消',exact=True).click()
    navigate(page,'jobs')
    tools=page.locator('#job-filter + .query-options');tools.locator('summary').first.click()
    tools.get_by_role('button',name='保存查询',exact=True).click()
    page.locator('#query-name-panel [name=name]').fill('排查范围');page.locator('#query-name-panel [type=submit]').click()
    saved=tools.locator('[data-saved]');saved.select_option(label='排查范围');saved.focus()
    expect(saved).to_have_value('排查范围')
    tools.get_by_role('button',name='删除查询',exact=True).click();page.locator('#batch-confirm').get_by_role('button',name='取消',exact=True).click()
    expect(saved.locator('option')).to_have_count(2)
    tools.get_by_role('button',name='删除查询',exact=True).click();page.locator('#batch-submit').click()
    expect(saved.locator('option')).to_have_count(1)


@pytest.mark.browser
def test_saving_after_toggle_ignores_older_list_response(navigation_page):
    page=navigation_page;navigate(page,'datasets')
    expect(page.locator('#datasets [data-edit-dataset]')).to_be_visible()
    pending=[]
    def delay_first(route):
        if not pending:pending.append((route,route.fetch()))
        else:route.continue_()
    page.route('**/api/v1/datasets/query',delay_first)
    page.locator('#datasets [data-schedule]').check()
    page.locator('#datasets [data-edit-dataset]').click()
    page.locator('#dataset-form [name=name]').fill('更新后的名称')
    page.locator('#dataset-form [type=submit]').click()
    expect(page.locator('#dataset-editor')).not_to_be_visible()
    expect(page.locator('#datasets')).to_contain_text('更新后的名称')
    assert pending
    for route,response in pending:route.fulfill(response=response)
    expect(page.locator('#datasets')).to_contain_text('更新后的名称')


@pytest.mark.browser
def test_toggle_write_blocks_row_actions_until_committed(navigation_page):
    page=navigation_page;navigate(page,'datasets')
    expect(page.locator('#datasets [data-edit-dataset]')).to_be_visible()
    pending=[]
    page.route('**/api/v1/console/datasets/*',lambda route:pending.append(route))
    page.locator('#datasets [data-schedule]').check()
    expect(page.locator('#datasets [data-edit-dataset]')).to_be_disabled()
    expect(page.locator('#datasets [data-recycle]')).to_be_disabled()
    assert len(pending)==1
    pending[0].fulfill(response=pending[0].fetch())
    page.unroute('**/api/v1/console/datasets/*')
    expect(page.locator('#datasets [data-edit-dataset]')).to_be_enabled()
    page.locator('#datasets [data-edit-dataset]').click()
    page.locator('#dataset-form [name=name]').fill('顺序写入')
    page.locator('#dataset-form [type=submit]').click()
    expect(page.locator('#dataset-editor')).not_to_be_visible()
    expect(page.locator('#datasets')).to_contain_text('顺序写入')
