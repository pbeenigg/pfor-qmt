import csv
import json
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from pfor_qmt.futures import REPORTS, write_records
from test_settlement_weekly import report_app
from test_console_navigation import navigation_page
from browser_helpers import navigate,choose_many
from playwright.sync_api import expect
from urllib.parse import quote


def seed_reports(app):
    rows=[]
    for index,(warehouse,unit,value) in enumerate([('总计','吨','100'),('甲,仓库','吨','60.1234567890123456789'),('乙仓库','吨','40'),('甲,仓库','手','1'),(None,'吨','2')]):
        rows.append(dict(dict.fromkeys(REPORTS['warehouse']['fields']),source='tushare',exchange='SHFE',symbol='CU',trade_date='2026-09-14',row_key=str(index),warehouse=warehouse,wh_id=str(index),unit=unit,vol=Decimal(value)))
    with app.store.connect() as conn:
        write_records(conn,'warehouse',rows)
        write_records(conn,'holding',[dict(dict.fromkeys(REPORTS['holding']['fields']),source='tushare',exchange='SHFE',symbol='CU',trade_date='2026-09-14',broker=name,vol=volume,long_hld=None,short_hld=1) for name,volume in [('会员甲',11),('会员乙',22)]])


def scope(resource='warehouse',**extras):
    return dict(source='tushare',resource=resource,exchange='SHFE',symbol='CU',start='2026-09-14',end='2026-09-14',**extras)


def test_dimensions_filter_records_summary_and_options(report_app):
    app=report_app;seed_reports(app)
    p=scope(dimensions={'warehouse':['甲,仓库'],'unit':['吨']})
    page=app.dispatch('POST','/futures/records',p)
    summary=app.dispatch('POST','/futures/summary',p)
    assert page['total']==summary['total']==1
    assert page['snapshot']==summary['snapshot']
    assert page['rows'][0]['vol']==Decimal('60.1234567890123456789')
    options=app.dispatch('POST','/futures/filter-options',p)
    assert set(options['options']['warehouse'])=={'总计','甲,仓库','乙仓库',None}
    assert set(options['options']['unit'])=={'吨','手'}
    nulls=app.dispatch('POST','/futures/records',scope(dimensions={'warehouse':[None]}))
    assert nulls['total']==1 and nulls['rows'][0]['warehouse'] is None
    holding=app.dispatch('POST','/futures/summary',scope('holding',dimensions={'broker':['会员甲']}))
    assert holding['total']==1 and holding['rows'][0]['broker']=='会员甲'
    assert app.dispatch('POST','/futures/records',scope())['total']==5
    reordered=app.dispatch('POST','/futures/records',scope(dimensions={'unit':['吨','吨'],'warehouse':['甲,仓库']}))
    assert reordered['snapshot']==page['snapshot']
    assert app.dispatch('POST','/futures/records',scope(dimensions={'warehouse':['不存在']}))['total']==0


@pytest.mark.parametrize('format',['csv','parquet'])
def test_filtered_export_is_offline_and_preserves_scope(report_app,format):
    app=report_app;seed_reports(app);app.settings.accounts=[]
    dimensions={'warehouse':['甲,仓库'],'unit':['吨']}
    job=app.dispatch('POST','/futures/export',scope(format=format,dimensions=dimensions))
    app.worker.execute(app.store.job(job['id']))
    saved=app.store.job(job['id']);assert saved['state']=='succeeded' and saved['result']['rows']==1
    target=app.settings.runtime/'exports'/saved['result']['file']
    if format=='csv':
        with target.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
    else:rows=pq.read_table(target).to_pylist()
    assert len(rows)==1 and rows[0]['warehouse']=='甲,仓库' and rows[0]['vol']=='60.1234567890123456789'
    assert json.loads(target.with_suffix(target.suffix+'.json').read_text('utf-8'))['query']['dimensions']==dimensions


def test_dimensions_reject_invalid_or_collection_scopes(report_app):
    app=report_app;seed_reports(app)
    for invalid in ({'broker':['会员甲']},{'warehouse':'甲仓库'},{'warehouse':[True]},{'warehouse':['x']*1001}):
        with pytest.raises(ValueError):app.dispatch('POST','/futures/records',scope(dimensions=invalid))
    before=app.store.jobs_page({})['total']
    with pytest.raises(ValueError,match='查询|导出'):
        app.dispatch('POST','/futures/sync',scope(dimensions={'warehouse':['甲,仓库']}))
    assert app.store.jobs_page({})['total']==before
    with pytest.raises(ValueError,match='查询|导出'):
        app.dispatch('POST','/console/maintenance',dict(name='错误维护',kind='download',payload=scope(dimensions={'warehouse':['甲,仓库']})))
    with pytest.raises(ValueError,match='顶层'):
        app.dispatch('POST','/futures/records',dict(scope(),selections=[scope(dimensions={'warehouse':['甲,仓库']})]))


def test_dimension_options_refuse_silent_truncation(report_app):
    store=report_app.store
    store.query("INSERT INTO futures_holdings(source,exchange,symbol,trade_date,broker) SELECT 'tushare','SHFE','CU','2026-09-14','会员'||n FROM generate_series(1,1001) n")
    with pytest.raises(ValueError,match='未截断'):report_app.dispatch('POST','/futures/filter-options',scope('holding'))


@pytest.mark.browser
def test_dimension_selector_query_export_and_collection_confirmation(navigation_page,report_app):
    page=navigation_page;seed_reports(report_app)
    page.locator('#data-source').select_option('tushare');navigate(page,'warehouse')
    choose_many(page,'#futures-form [name=exchange]','SHFE')
    choose_many(page,'#futures-form [name=symbol]','SHFE:CU')
    for key in ('start','end'):page.locator('#futures-form [name='+key+']').fill('2026-09-14')
    page.locator('#report-dimensions summary').click()
    expect(page.locator('#report-dimension-note')).to_contain_text('5 条')
    encoded=lambda value:quote(json.dumps(value,ensure_ascii=False),safe="~()*!.'-")
    choose_many(page,'#report-dimension-warehouse',encoded('甲,仓库'))
    choose_many(page,'#report-dimension-unit',encoded('吨'))
    page.locator('#futures-form [type=submit]').click()
    expect(page.locator('#futures-page')).to_contain_text('共 1 条')
    expect(page.locator('#futures-rows')).to_contain_text('甲,仓库')
    expect(page.locator('#report-summary')).to_contain_text('共 1 条')
    for width in (1440,1024,768,390):
        page.set_viewport_size({'width':width,'height':980})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=f'output/playwright/report-dimensions-{width}.png',full_page=True)
    page.set_viewport_size({'width':1440,'height':980})
    navigate(page,'holding');navigate(page,'warehouse')
    expect(page.locator('#report-dimension-warehouse option:checked')).to_have_attribute('value',encoded('甲,仓库'))
    page.locator('#futures-sync').click()
    expect(page.locator('#batch-title')).to_have_text('按完整资料范围采集？')
    page.locator('#batch-confirm').get_by_role('button',name='取消',exact=True).click()
    expect(page).to_have_url(__import__('re').compile('#warehouse$'))
    before=report_app.store.jobs_page({})['total']
    page.locator('#futures-sync').click();page.locator('#batch-submit').click()
    expect(page).to_have_url(__import__('re').compile('#collect$'))
    assert report_app.store.jobs_page({})['total']==before
    navigate(page,'warehouse');page.locator('#futures-csv').click()
    with page.expect_response(lambda response:response.url.endswith('/futures/export')) as exported:page.locator('#batch-submit').click()
    body=exported.value.json();assert exported.value.status==200
    assert body['payload']['dimensions']=={'warehouse':['甲,仓库'],'unit':['吨']}
    navigate(page,'warehouse')
    page.locator('#futures-form [name=start]').fill('2026-09-15');page.locator('#futures-form [name=end]').fill('2026-09-15')
    page.locator('#report-dimensions-load').click()
    expect(page.locator('#report-dimension-note')).to_contain_text('0 条')
    expect(page.locator('#report-dimension-warehouse option:checked')).to_contain_text('当前范围无记录')
    page.locator('#futures-form [type=submit]').click();expect(page.locator('#futures-page')).to_contain_text('共 0 条')
