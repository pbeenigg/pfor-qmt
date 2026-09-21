import pytest

from pfor_qmt.data import chunks, day
from pfor_qmt.tasks import Worker
from pfor_qmt.quality_checks import stored_issues
from test_storage_tasks import Source
from test_console_navigation import navigation_page
from test_settlement_weekly import report_app


def make_range(store,code,period,start,end):
    payload=dict(source='qmt',members=[code],periods=[period],start=start,end=end)
    return store.create_job('download',dict(payload,chunks=chunks(payload)))


@pytest.mark.parametrize('code',['000001.SZ','a2611.DF'])
def test_empty_weekend_daily_is_not_a_source_failure(store,tmp_path,code):
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda seconds:False
    original=make_range(store,code,'1d','2026-09-20','2026-09-20')
    worker.execute(original)
    result=store.job(original['id']);units=store.units_page(original['id'])['rows']
    assert result['state']=='succeeded' and result['checkpoint']==1
    assert result['result']['rows']==0 and result['error_code'] is None
    assert units[0]['quality_state']=='not_applicable'
    assert units[0]['issues'][0]['code']=='DAILY_WEEKEND_CLOSED'
    assert '不适用' in result['result']['message']
    assert not store.query('SELECT * FROM bars')
    assert not store.query('SELECT * FROM trading_dates')
    verify=store.create_verification(original['id']);worker.execute(verify)
    assert store.job(verify['id'])['state']=='succeeded'
    assert store.units_page(verify['id'])['rows'][0]['quality_state']=='not_applicable'


@pytest.mark.parametrize('period,start,end',[
    ('1d','2026-09-18','2026-09-20'),
    ('1d','2026-09-14','2026-09-14'),
    ('1m','2026-09-19','2026-09-19'),
    ('5m','2026-09-20','2026-09-20'),
])
def test_weekend_daily_rule_never_masks_weekday_or_minute_empty(store,tmp_path,period,start,end):
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda seconds:False
    job=make_range(store,'a2611.DF',period,start,end);worker.execute(job)
    saved=store.job(job['id'])
    assert saved['state']=='blocked' and saved['checkpoint']==0
    assert saved['error_code']=='SOURCE_NOT_READY' and not store.query('SELECT * FROM coverage')


def test_daily_records_without_calendar_are_kept_as_unverified(store,tmp_path):
    source=Source();source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source)
    job=make_range(store,'a2611.DF','1d','2026-09-14','2026-09-14');worker.execute(job)
    saved=store.job(job['id'])
    assert saved['state']=='partial' and saved['result']['rows']==1 and saved['error_code'] is None
    assert store.history('a2611.DF')['rows']


def test_saved_open_day_conflict_is_not_silently_ignored(store,tmp_path):
    store.query("INSERT INTO trading_dates(source,market,day,is_open) VALUES('qmt','DF','2026-09-20',true)")
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda seconds:False
    job=make_range(store,'a2611.DF','1d','2026-09-20','2026-09-20');worker.execute(job)
    assert store.job(job['id'])['state']=='blocked'
    assert not store.query('SELECT * FROM coverage')


def test_weekend_rule_does_not_hide_saved_bar_or_calendar_conflict():
    part=dict(code='a2611.DF',period='1d',start='2026-09-20',end='2026-09-20')
    calendar=[dict(day=day(part['start']),is_open=True)]
    result=stored_issues([],part,calendar)
    assert any(item['quality_state']=='missing' for item in result)
    result=stored_issues([dict(trading_day=day(part['start']),time=part['start'])],part,[])
    assert all(item['quality_state']!='not_applicable' for item in result)
    result=stored_issues([],dict(part,source='tushare',code='A2611.DCE'),[])
    assert any(item['code']=='CALENDAR_UNKNOWN' for item in result)


@pytest.mark.browser
@pytest.mark.parametrize('width',[1440,390])
def test_weekend_task_ui_explains_zero_rows(navigation_page,report_app,width):
    page=navigation_page;source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(report_app.store,report_app.settings.runtime,source=source);worker.stop.wait=lambda seconds:False
    job=make_range(report_app.store,'a2611.DF','1d','2026-09-20','2026-09-20');worker.execute(job)
    from browser_helpers import navigate
    from playwright.sync_api import expect
    page.set_viewport_size({'width':width,'height':980})
    navigate(page,'jobs');row=page.locator('#job-rows tr').filter(has_text=str(job['id'])[:8])
    expect(row).to_contain_text('全部分块不适用')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    row.locator('[data-job-detail]').click()
    expect(page.locator('#task-body')).to_contain_text('不适用')
    page.locator('[data-task-tab=units]').click()
    expect(page.locator('#task-records')).to_contain_text('不适用')
    page.locator('#task-records [data-record]').click()
    expect(page.locator('#record-extra')).to_contain_text('周六/周日')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=f'output/playwright/qmt-weekend-{width}.png',full_page=True)
