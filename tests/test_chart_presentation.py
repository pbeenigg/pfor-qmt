from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_helpers import navigate,choose_many
from test_console_navigation import navigation_page
from test_settlement_weekly import report_app,settle
from pfor_qmt.futures import normalize_report,write_records
from pfor_qmt.data import day


def seed_history(app):
    for n in range(12):
        app.store.save_security(f'T{n:02d}2610.SHF',f'测试品种{n:02d}十月','future',{},source='tushare',subtype='contract')
    app.store.query("INSERT INTO bars(instrument_id,code,source,period,time,open,high,low,close,volume,amount,open_interest) SELECT instrument_id,code,source,'1d','2026-09-14'::timestamptz,1,2,1,2,123456789.123456789,987654321.123456789,NULL FROM securities WHERE code LIKE 'T%%' AND source='tushare'")


def seed_reports(app):
    app.store.save_security('CU.SHF','沪铜主力','future',{},source='tushare',subtype='continuous',metadata={'product':'CU'})
    app.store.save_security('CU2611.SHF','沪铜2611','future',{},source='tushare',subtype='contract')
    # Same code in another source must never supply the display name.
    app.store.save_security('SC2610.INE','原油2610','future',{},source='tushare',subtype='contract')
    app.store.save_security('SC2610.INE','错误来源名称','future',{},source='qmt')
    app.store.query("INSERT INTO contract_mappings(source,code,trading_day,member_code) VALUES('tushare','CU.SHF','2026-09-14','CU2610.SHF'),('tushare','CU.SHF','2026-09-15','CU2611.SHF')")
    with app.store.connect() as conn:
        write_records(conn,'settle',normalize_report('settle',[settle()],'SHFE','CU2610.SHF',day('2026-09-14'),day('2026-09-14')))
        write_records(conn,'settle',normalize_report('settle',[dict(settle(),exchange='INE',ts_code='SC2610.INE')],'INE','SC2610.INE',day('2026-09-14'),day('2026-09-14')))


def test_summary_names_are_source_scoped_without_changing_numbers(report_app):
    seed_history(report_app);seed_reports(report_app)
    history=report_app.dispatch('POST','/history/summary',dict(source='tushare',members=['T002610.SHF'],periods=['1d'],start='2026-09-14',end='2026-09-14'))
    assert history['instrument_names']['T002610.SHF']=='测试品种00十月'
    assert history['groups'][0]['volume']==Decimal('123456789.123456789')
    assert history['groups'][0]['latest_open_interest'] is None
    p=dict(source='tushare',resource='mapping',code='CU.SHF',start='2026-09-14',end='2026-09-15')
    summary=report_app.dispatch('POST','/futures/summary',p)
    assert summary['instrument_names']=={'CU.SHF':'沪铜主力','CU2610.SHF':'沪铜2610','CU2611.SHF':'沪铜2611'}
    assert summary['rows'][0]['member_code']=='CU2610.SHF'
    oil=report_app.dispatch('POST','/futures/summary',dict(p,resource='settle',exchange='INE',code='SC2610.INE'))
    assert oil['instrument_names']=={'SC2610.INE':'原油2610'}
    report_app.store.query("INSERT INTO contract_mappings(source,code,trading_day,member_code) SELECT 'tushare','CU.SHF','2010-01-01'::date+n,'CU2611.SHF' FROM generate_series(0,5000) n ON CONFLICT DO NOTHING")
    report_app.store.query("INSERT INTO contract_mappings(source,code,trading_day,member_code) VALUES('tushare','CU.SHF','2009-01-01','SC2610.INE')")
    archived=dict(p,start='2009-01-01')
    assert 'SC2610.INE' not in report_app.dispatch('POST','/futures/summary',archived)['instrument_names']
    first=report_app.dispatch('POST','/futures/records',dict(archived,limit=1))
    assert first['instrument_names']['SC2610.INE']=='原油2610'


@pytest.mark.browser
def test_history_summary_is_bounded_searchable_and_chart_selectable(navigation_page,report_app):
    page=navigation_page;seed_history(report_app)
    page.locator('#data-source').select_option('tushare');navigate(page,'history')
    page.locator('[data-picker=history]').click();page.locator('#picker-clear').click()
    page.locator('#picker-active').uncheck();page.locator('#picker-search').fill('测试品种')
    page.locator('#picker-select-matched').click();expect(page.locator('#picker-count')).to_contain_text('12 个');page.locator('#picker-apply').click()
    page.locator('#history-form button.primary').click()
    expect(page.locator('#history-summary-rows tr')).to_have_count(5)
    expect(page.locator('#history-summary-count')).to_contain_text('12 组')
    expect(page.locator('#history-summary-rows')).to_contain_text('测试品种00十月')
    page.locator('#history-summary-next').click()
    expect(page.locator('#history-summary-page')).to_contain_text('6–10')
    first=page.locator('#history-summary-rows [data-summary-chart]').first
    code=first.get_attribute('data-summary-chart');first.click()
    expect(page.locator('#chart-code')).to_have_value(code)
    expect(page.locator('#chart-window-note')).to_contain_text('共 1 条')
    page.locator('#history-summary-search').fill('品种11')
    expect(page.locator('#history-summary-rows tr')).to_have_count(1)
    expect(page.locator('#history-summary-rows')).to_contain_text('品种11')
    page.locator('#history-summary-search').fill('')
    page.locator('#history-summary-size').select_option('10');expect(page.locator('#history-summary-rows tr')).to_have_count(10)
    page.locator('#history-summary-size').select_option('5')
    page.locator('#history-summary-rows [data-record]').first.click()
    expect(page.locator('#record-fields')).to_contain_text('123456789.123456789')
    expect(page.locator('#record-fields')).to_contain_text('未提供');page.keyboard.press('Escape')
    out=Path('output/playwright');out.mkdir(parents=True,exist_ok=True)
    for width in (1440,1024,768,390):
        page.set_viewport_size({'width':width,'height':980})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(out/f'history-summary-table-{width}.png'),full_page=True)
    page.locator('#history-summary > summary').click();expect(page.locator('#history-summary-rows')).not_to_be_visible()
    page.locator('#data-source').select_option('qmt');expect(page.locator('#history-summary')).not_to_be_visible()


@pytest.mark.browser
def test_mapping_and_settlement_chart_labels_use_names(navigation_page,report_app):
    page=navigation_page;seed_reports(report_app)
    page.locator('#data-source').select_option('tushare');navigate(page,'mapping')
    choose_many(page,'#futures-form [name=exchange]','SHFE');choose_many(page,'#futures-form [name=code]','CU.SHF')
    page.locator('#futures-form [name=start]').fill('2026-09-14');page.locator('#futures-form [name=end]').fill('2026-09-15')
    page.locator('#futures-form [type=submit]').click()
    expect(page.locator('#report-summary')).to_contain_text('沪铜主力')
    expect(page.locator('#report-series option:checked')).to_have_text('沪铜主力')
    expect(page.locator('#futures-rows')).to_contain_text('沪铜2611')
    chart=page.locator('#report-chart')
    labels=chart.evaluate("el=>{const o=echarts.getInstanceByDom(el).getOption();return {legend:o.legend[0].formatter(o.series[0].name),axis:o.yAxis[0].axisLabel.formatter('CU2610.SHF'),tooltip:o.tooltip[0].formatter([{seriesName:o.series[0].name,dataIndex:0}])};}")
    assert labels['legend']=='沪铜主力' and labels['axis']=='沪铜2610'
    assert '沪铜2610' in labels['tooltip'] and '错误来源名称' not in labels['tooltip']
    navigate(page,'settle');page.locator('[data-picker=report]').click()
    page.locator('#picker-search').fill('CU2610.SHF');page.locator('#picker-rows input[value="CU2610.SHF"]').check();page.locator('#picker-apply').click()
    page.locator('#futures-form [type=submit]').click()
    expect(page.locator('#report-series option:checked')).to_have_text('沪铜2610')
    expect(page.locator('#report-summary')).to_contain_text('沪铜2610')
    assert chart.evaluate("el=>{const o=echarts.getInstanceByDom(el).getOption();return o.tooltip[0].formatter([{seriesName:o.series[0].name,dataIndex:0}]);}").find('80123.1234567890123456789')>=0
    for resource in ('mapping','settle'):
        navigate(page,resource);expect(page.locator('#report-chart canvas')).to_be_visible()
        for width in (1440,390):
            page.set_viewport_size({'width':width,'height':980});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=f'output/playwright/named-{resource}-{width}.png',full_page=True)
        page.set_viewport_size({'width':1440,'height':980})
