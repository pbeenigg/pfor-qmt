import csv
from datetime import datetime, timedelta
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from pfor_qmt.data import day, SHANGHAI
from pfor_qmt.futures import normalize_report, REPORTS, request_chunks
from pfor_qmt.freshness import freshness_page
from pfor_qmt.maintenance import save_plan, tick
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tushare import SourceError, TushareSource
from test_tushare import account, seed, fake_request


def weekly():
    return dict(exchange='SHFE',prd='CU',name='铜',week='20199',week_date='20190301',vol=2**60+11,vol_yoy='-3.2',
                amount='1234.123456789012345678901234567890',amout_yoy='-2.5',cumvol=123,cumamt='9999.00000000000001',
                open_interest=None,mc_close='80000.1234567890123456789')


def settle():
    return dict(exchange='SHFE',ts_code='CU2610.SHF',trade_date='20260914',settle='80123.1234567890123456789',
                trading_fee_rate='0.050',trading_fee=0,delivery_fee='2',b_hedging_margin_rate='0.07',offset_today_fee=None)


def extended_request(self,api,p,fields=''):
    if api=='fut_settle':return [settle()]
    if api=='fut_weekly_detail':return [weekly()]
    return fake_request(self,api,p,fields)


def test_units_original_weeks_null_and_duplicates():
    row=normalize_report('weekly_detail',[weekly(),weekly()],'SHFE','CU',day('2019-03-01'),day('2019-03-01'))[0]
    assert row['week']=='20199' and row['week_date']==day('2019-03-01')
    assert row['amount']==Decimal('123412345678.9012345678901234567890')
    assert row['original_amount']==Decimal(weekly()['amount'])
    assert row['vol']==2**60+11 and row['open_interest'] is None and row['amout_yoy']==Decimal('-2.5')
    assert normalize_report('weekly_detail',[row],'SHFE','CU',day('2019-03-01'),day('2019-03-01'),stored=True)==[row]
    with pytest.raises(ValueError,match='口径'):
        normalize_report('weekly_detail',[dict(row,amount=1)],'SHFE','CU',day('2019-03-01'),day('2019-03-01'),stored=True)
    value=normalize_report('settle',[settle()],'SHFE','CU2610.SHF',day('2026-09-14'),day('2026-09-14'))[0]
    assert value['trading_fee_rate']==Decimal('0.050') and value['offset_today_fee'] is None
    with pytest.raises(ValueError,match='冲突'):
        normalize_report('settle',[settle(),dict(settle(),trading_fee=2)],'SHFE','CU2610.SHF',day('2026-09-14'),day('2026-09-14'))
    with pytest.raises(ValueError):normalize_report('weekly_detail',[dict(weekly(),week='201954')],'SHFE','CU',day('2019-03-01'),day('2019-03-01'))


def test_api_parameters_inclusive_week_date_and_requested_fields(monkeypatch):
    calls=[]
    def request(self,api,p,fields=''):
        calls.append((api,p,fields))
        return [weekly(),dict(weekly(),week_date='20190308',week='201910')] if api=='fut_weekly_detail' else [settle()]
    monkeypatch.setattr(TushareSource,'request',request)
    source=TushareSource(account())
    rows=source.report(dict(resource='weekly_detail',exchange='SHFE',symbol='CU'),'2019-03-01','2019-03-01')
    assert len(rows)==1 and rows[0]['week']=='20199'
    assert calls[0][1]==dict(exchange='SHFE',prd='CU',start_week='201901',end_week='20199')
    assert 'amout_yoy' in calls[0][2] and 'original_amount' not in calls[0][2]
    source.report(dict(resource='settle',exchange='SHFE',code='CU2610.SHF'),'2026-09-14','2026-09-14')
    assert calls[1][1]['ts_code']=='CU2610.SHF' and 'offset_today_fee' in calls[1][2]


def test_row_limits_split_and_refuse_unprovable_single_bucket(monkeypatch):
    calls=[]
    def request(self,api,p,fields=''):
        calls.append(p)
        if api=='fut_settle':return [{}]*1600 if p['start_date']!=p['end_date'] else [p['start_date']]
        return [{}]*4000 if p['start_week']!=p['end_week'] else [p['start_week']]
    monkeypatch.setattr(TushareSource,'request',request)
    source=TushareSource(account())
    assert source.bounded('fut_settle','CU2610.SHF',datetime(2026,9,14),datetime(2026,9,15))==['20260914','20260915']
    assert source.weekly_bounded('SHFE','CU',2019*53+51,2020*53+1)==['201952','201953','202001','202002','20201','20202']
    monkeypatch.setattr(TushareSource,'request',lambda *args:[{}]*4000)
    with pytest.raises(SourceError,match='单周'):source.weekly_bounded('SHFE','CU',2020*53,2020*53)
    monkeypatch.setattr(TushareSource,'request',lambda *args:[{}]*1600)
    with pytest.raises(SourceError,match='上限'):source.bounded('fut_settle','CU2610.SHF',datetime(2026,9,14),datetime(2026,9,14))


def test_legacy_week_string_filter_does_not_lose_single_digit_weeks(monkeypatch):
    samples=[weekly(),dict(weekly(),week='201901',week_date='20190104'),dict(weekly(),week='201952',week_date='20191227')]
    def request(self,api,p,fields=''):
        return [row for row in samples if p['start_week']<=row['week']<=p['end_week']]
    monkeypatch.setattr(TushareSource,'request',request)
    source=TushareSource(account())
    rows=source.report(dict(resource='weekly_detail',exchange='SHFE',symbol='CU'),'2019-03-01','2019-03-01')
    assert len(rows)==1 and rows[0]['week']=='20199'


@pytest.fixture
def report_app(store,tmp_path,monkeypatch):
    seed(store)
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    app.settings.accounts=[account()];app.settings.default_account_id='main'
    monkeypatch.setattr(TushareSource,'request',extended_request)
    return app


@pytest.mark.postgres
@pytest.mark.parametrize('resource,start,end,choice',[('settle','2026-09-14','2026-09-14',{'code':'CU2610.SHF'}),('weekly_detail','2019-03-01','2019-03-01',{'symbol':'CU'})])
def test_sync_repeat_query_offline_verify_and_both_exports(report_app,tmp_path,resource,start,end,choice):
    app=report_app;store=app.store
    p=dict(source='tushare',resource=resource,exchange='SHFE',start=start,end=end,**choice)
    first=app.dispatch('POST','/futures/sync',p);app.tushare_worker.execute(store.job(first['id']))
    second=app.dispatch('POST','/futures/sync',p);app.tushare_worker.execute(store.job(second['id']))
    rows=app.dispatch('POST','/futures/records',p)['rows']
    assert len(rows)==1 and store.job(first['id'])['result']['rows']==1
    assert store.query('SELECT count(*) AS n FROM '+REPORTS[resource]['table'],one=True)['n']==1
    check=store.create_verification(first['id']);app.worker.execute(check)
    assert store.job(check['id'])['state']=='partial'
    issue_code=store.units_page(check['id'])['rows'][0]['issues'][0]['code']
    assert issue_code==('WEEKLY_COVERAGE_UNVERIFIED' if resource=='weekly_detail' else 'REPORT_PUBLICATION_UNVERIFIED')
    app.settings.accounts=[]
    for fmt in ['csv','parquet']:
        job=app.dispatch('POST','/futures/export',dict(p,format=fmt));app.worker.execute(store.job(job['id']))
        exported=store.job(job['id']);assert exported['state']=='succeeded'
        path=app.settings.runtime/'exports'/exported['result']['file']
        if fmt=='csv':
            assert path.read_bytes().startswith(b'\xef\xbb\xbf')
            with path.open(encoding='utf-8-sig',newline='') as stream: data=list(csv.DictReader(stream))
        else:data=pq.read_table(path).to_pylist()
        for key,value in rows[0].items():
            if value is None:
                assert data[0][key]==(None if fmt=='parquet' else '')
            else:
                assert str(data[0][key])==str(value)
    assert store.health()['version']==8
    store.migrate()
    assert len(store.query('SELECT * FROM '+REPORTS[resource]['table']))==1


@pytest.mark.postgres
def test_weekly_does_not_request_daily_calendar_or_fabricate_gaps(report_app,monkeypatch):
    app=report_app
    def no_calendar(*args,**kwargs):pytest.fail('Weekly publication must not be checked as daily rows')
    monkeypatch.setattr(app.tushare_worker,'calendar',no_calendar)
    job=app.dispatch('POST','/futures/sync',dict(source='tushare',resource='weekly_detail',exchange='SHFE',symbol='CU',start='2019-02-25',end='2019-03-03'))
    app.tushare_worker.execute(app.store.job(job['id']))
    unit=app.store.units_page(job['id'])['rows'][0]
    assert unit['row_count']==1 and len(unit['issues'])==1
    assert unit['issues'][0]['code']=='WEEKLY_COVERAGE_UNVERIFIED'


@pytest.mark.postgres
def test_settlement_revision_updates_once_and_empty_response_keeps_data(report_app,monkeypatch):
    app=report_app;store=app.store
    payload=dict(source='tushare',resource='settle',exchange='SHFE',code='CU2610.SHF',start='2026-09-14',end='2026-09-14')
    job=app.dispatch('POST','/futures/sync',payload);app.tushare_worker.execute(store.job(job['id']))
    original=store.query('SELECT * FROM futures_settlements',one=True)
    def changed(self,api,p,fields=''):
        return [dict(settle(),trading_fee='9.00000000000001')] if api=='fut_settle' else fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',changed)
    second=app.dispatch('POST','/futures/sync',payload);app.tushare_worker.execute(store.job(second['id']))
    updated=store.query('SELECT * FROM futures_settlements',one=True)
    assert updated['trading_fee']==Decimal('9.00000000000001') and updated['updated_at']>original['updated_at']
    assert store.query('SELECT count(*) AS n FROM futures_settlements',one=True)['n']==1
    monkeypatch.setattr(TushareSource,'request',lambda self,api,p,fields='':[] if api=='fut_settle' else fake_request(self,api,p,fields))
    empty=app.dispatch('POST','/futures/sync',payload);app.tushare_worker.execute(store.job(empty['id']))
    assert store.job(empty['id'])['state']=='partial' and store.query('SELECT * FROM futures_settlements',one=True)==updated


@pytest.mark.postgres
def test_empty_permissions_account_binding_validation_and_maintenance(report_app,monkeypatch):
    app=report_app;store=app.store
    targets=[dict(resource='settle',exchange='SHFE',code='CU2610.SHF'),dict(resource='weekly_detail',exchange='SHFE',symbol='CU'),dict(resource='holding',exchange='SHFE',symbol='CU')]
    p=dict(source='tushare',selections=targets,start='2026-09-14',end='2026-09-14')
    app.capabilities['main']={'capabilities':{'settle':{'state':'permission'}}}
    with pytest.raises(ValueError):app.dispatch('POST','/futures/sync',p)
    assert not store.query('SELECT * FROM jobs')
    app.capabilities={}
    jobs=app.dispatch('POST','/futures/sync',p)['jobs']
    assert len(jobs)==3 and all(job['payload']['account_id']=='main' for job in jobs)
    for job in jobs:app.tushare_worker.execute(store.job(job['id']))
    weekly_job=next(job for job in jobs if job['payload']['resource']=='weekly_detail')
    assert store.job(weekly_job['id'])['state']=='partial' and store.job(weekly_job['id'])['result']['rows']==0
    for job in jobs:save_plan(store,dict(job_id=str(job['id']),name=job['payload']['resource']))
    tick(app.tushare_worker,datetime(2026,9,14,19,tzinfo=SHANGHAI))
    assert len(store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL'))==3
    assert {r['resource'] for r in freshness_page(store,{},datetime(2026,9,14,20,tzinfo=SHANGHAI))['rows']}=={'settle','holding','weekly_detail'}
    with pytest.raises(ValueError):app.dispatch('POST','/futures/sync',dict(source='tushare',resource='settle',exchange='DCE',code='CU2610.SHF',start='2026-09-14',end='2026-09-14'))
    with pytest.raises(ValueError):app.dispatch('POST','/futures/sync',dict(source='tushare',resource='weekly_detail',exchange='SHFE',symbol='UNKNOWN',start='2026-09-14',end='2026-09-14'))


def test_twenty_year_weekly_range_and_ine_holding_still_uses_shfe(monkeypatch):
    parts=request_chunks(dict(resource='weekly_detail',source='tushare',exchange='SHFE',symbol='CU',code='',start='2010-03-01',end='2026-09-18'))
    assert parts[0]['start']=='2010-03-01' and parts[-1]['end']=='2026-09-18'
    calls=[]
    def request(self,api,p,fields=''):
        calls.append((api,p))
        return [dict(trade_date='20260914',exchange='SHFE',symbol='SC',broker='会员',vol=None,long_hld=20,short_hld=None)]
    monkeypatch.setattr(TushareSource,'request',request)
    result=TushareSource(account()).report(dict(resource='holding',exchange='INE',symbol='SC'),'2026-09-14','2026-09-14')
    assert calls[0][1]['exchange']=='SHFE' and result[0]['exchange']=='SHFE' and result[0]['vol'] is None
