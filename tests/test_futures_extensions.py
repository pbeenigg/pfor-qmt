import csv
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pfor_qmt.data import chunks, day, period_label, periods, SHANGHAI
from pfor_qmt.futures import normalize_report, page, request_chunks
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tasks import Worker
from pfor_qmt.tushare import SourceError, TushareSource
from test_tushare import account, daily, fake_request, seed, download_job


@pytest.fixture
def extension_app(store,tmp_path,monkeypatch):
    seed(store)
    store.save_security('CU.SHF','沪铜主力','future',{},subtype='continuous',metadata={'product':'CU'},source='tushare')
    app=Application(Settings(config_path=tmp_path/'config.toml'),store=store)
    app.settings.accounts=[account()];app.settings.default_account_id='main'
    monkeypatch.setattr(TushareSource,'request',fake_request)
    return app


@pytest.mark.parametrize('period',['1w','1mo','15m','30m','60m'])
def test_new_periods_are_tushare_only(period):
    assert periods([period],'tushare')==[period]
    with pytest.raises(ValueError): periods([period],'qmt')
    result=chunks(dict(source='tushare',members=['CU2610.SHF'],periods=[period],start='2026-09-01',end='2026-09-17'))
    assert result and all(part['period']==period for part in result)


@pytest.mark.parametrize('period,freq',[('15m','15min'),('30m','30min'),('60m','60min')])
def test_extended_minutes_send_upstream_frequency_and_keep_unknown_day(monkeypatch,period,freq):
    calls=[]
    def request(self,api,p,fields=''):
        calls.append((api,p['freq']))
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    rows=TushareSource(account()).history('CU2610.SHF',period,'2026-09-14','2026-09-14')
    assert calls==[('ft_mins',freq)] and rows[0]['trading_day'] is None
    assert rows[0]['amount']==Decimal('12345.6789')


@pytest.mark.parametrize('api,cap,period',[('fut_weekly_monthly',6000,'1w'),('fut_wsr',1000,None),('fut_holding',2000,None),('ft_mins',8000,'60m')])
def test_new_api_row_caps_split_and_refuse_truncation(monkeypatch,api,cap,period):
    adapter=TushareSource(account())
    calls=[]
    def request(self,api,p,fields=''):
        calls.append(p)
        return [{}]*cap if p['start_date']!=p['end_date'] else [dict(part=p['start_date'])]
    monkeypatch.setattr(TushareSource,'request',request)
    first=datetime(2026,9,14)
    end=first+timedelta(seconds=2) if api=='ft_mins' else first+timedelta(days=2)
    rows=adapter.bounded(api,'CU2610.SHF' if period else None,first,end,period,extra={'symbol':'CU','exchange':'SHFE'} if not period else None)
    assert len(rows)==3 and len(calls)>1
    monkeypatch.setattr(TushareSource,'request',lambda *a,**k:[{}]*cap)
    with pytest.raises(SourceError,match='最小区间'):
        adapter.bounded(api,'CU2610.SHF',first,first,period)


def test_warehouse_dimensions_nulls_and_conflicts():
    base=dict(trade_date='20260914',symbol='CU',exchange='SHFE',warehouse='仓库',wh_id='01',vol='12.000000000000000001',pre_vol=None,vol_chg=-2,unit='吨',year=2025)
    rows=normalize_report('warehouse',[base,dict(base,year=2026)],'SHFE','CU',day('2026-09-14'),day('2026-09-14'))
    assert len(rows)==2 and rows[0]['row_key']!=rows[1]['row_key']
    assert all(row['pre_vol'] is None and row['vol']==Decimal('12.000000000000000001') for row in rows)
    with pytest.raises(ValueError,match='冲突'):
        normalize_report('warehouse',[base,dict(base,vol=20)],'SHFE','CU',day('2026-09-14'),day('2026-09-14'))
    with pytest.raises(ValueError,match='不符'):
        normalize_report('warehouse',[dict(base,exchange='DCE')],'SHFE','CU',day('2026-09-14'),day('2026-09-14'))


def test_shfe_copper_and_bonded_copper_keep_upstream_product_names():
    base=dict(trade_date='20260917',symbol='CU',exchange='SHFE',warehouse='世天威外高桥',wh_id=None,unit='吨',area='上海',pre_vol=None)
    rows=normalize_report('warehouse',[dict(base,fut_name='铜',vol=400),dict(base,fut_name='铜(BC)',vol=4076)],'SHFE','CU',day('2026-09-17'),day('2026-09-17'))
    assert len(rows)==2 and {r['vol'] for r in rows}=={Decimal(400),Decimal(4076)}
    assert len({r['row_key'] for r in rows})==2


@pytest.mark.postgres
@pytest.mark.parametrize('period',['1w','1mo'])
def test_week_month_latest_calculation_date_units_and_exports(extension_app,tmp_path,monkeypatch,period):
    import pyarrow.parquet as pq
    app=extension_app;store=app.store
    as_of=['20260917'];close=['80123.1234567890123456789']
    def request(self,api,p,fields=''):
        if api=='fut_weekly_monthly':
            return [dict(daily(),trade_date='20260918' if period=='1w' else '20260930',freq=p['freq'],end_date=as_of[0],close=close[0])]
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    first=download_job(store,periods=[period],start='2026-09-14',end='2026-09-17')
    app.tushare_worker.execute(first)
    result=store.job(first['id'])
    assert result['state']=='partial' and result['result']['rows']==1
    row=store.history('CU2610.SHF',period,'2026-09-14','2026-09-17',source='tushare')['rows'][0]
    assert row['time'].date()==period_label(day('2026-09-17'),period)
    assert row['trading_day'] is None and row['as_of_date']==day('2026-09-17')
    assert row['amount']==Decimal('123456789.0123456789')
    assert Decimal(row['source_fields']['amount'])==Decimal('12345.67890123456789')
    as_of[0]='20260916';close[0]='7'
    second=download_job(store,periods=[period],start='2026-09-14',end='2026-09-17')
    app.tushare_worker.execute(second)
    assert store.history('CU2610.SHF',period,source='tushare')['rows'][0]['close']==row['close']
    for fmt in ('csv','parquet'):
        job=app.dispatch('POST','/exports',dict(source='tushare',members=['CU2610.SHF'],period=period,start='2026-09-14',end='2026-09-17',format=fmt))
        app.worker.execute(job)
        saved=store.job(job['id']);assert saved['state']=='succeeded'
        path=app.worker.runtime/'exports'/saved['result']['file']
        if fmt=='csv':
            with path.open(encoding='utf-8-sig',newline='') as f: records=list(csv.DictReader(f))
        else: records=pq.read_table(path).to_pylist()
        assert records[0]['as_of_date']=='2026-09-17'
        assert Decimal(records[0]['amount'])==row['amount']
        assert json.loads(records[0]['source_fields'])['amount']==row['source_fields']['amount']


@pytest.mark.postgres
def test_report_calendar_closed_days_and_backward_compatible_query(extension_app,monkeypatch):
    app=extension_app
    def request(self,api,p,fields=''):
        rows=fake_request(self,api,p,fields)
        if api=='fut_trade_cal':
            for row in rows: row['pretrade_date']='20260911'
        return rows
    monkeypatch.setattr(TushareSource,'request',request)
    job=app.dispatch('POST','/futures/sync',dict(source='tushare',resource='calendar',exchange='SHFE',start='2026-09-12',end='2026-09-14'))
    app.tushare_worker.execute(app.store.job(job['id']))
    assert app.store.job(job['id'])['state']=='succeeded'
    assert app.dispatch('GET','/calendar',dict(source='tushare',market='SF',start='2026-09-12',end='2026-09-14'))==[{'day':day('2026-09-14')}]
    rows=app.dispatch('GET','/futures/records',dict(source='tushare',resource='calendar',exchange='SHFE',start='2026-09-12',end='2026-09-14'))['rows']
    assert len(rows)==3 and [r['is_open'] for r in rows]==[False,False,True]
    assert rows[0]['pretrade_date']==day('2026-09-11')


@pytest.mark.postgres
@pytest.mark.parametrize('resource',['calendar','mapping','warehouse','holding'])
def test_reports_shared_queue_idempotence_query_export(extension_app,resource):
    import pyarrow.parquet as pq
    app=extension_app;store=app.store
    p=dict(source='tushare',resource=resource,exchange='SHFE',symbol='CU',code='CU.SHF',start='2026-09-14',end='2026-09-14')
    for _ in range(2):
        job=app.dispatch('POST','/futures/sync',p)
        assert job['kind']=='download' and job['payload']['account_id']=='main'
        app.tushare_worker.execute(store.job(job['id']))
        assert store.job(job['id'])['state']=='succeeded'
    result=app.dispatch('GET','/futures/records',p)
    assert len(result['rows'])==1
    if resource=='holding': assert result['rows'][0]['long_hld'] is None
    for fmt in ('csv','parquet'):
        job=app.dispatch('POST','/futures/export',dict(p,format=fmt));app.worker.execute(store.job(job['id']))
        saved=store.job(job['id']);assert saved['state']=='succeeded'
        path=app.worker.runtime/'exports'/saved['result']['file']
        if fmt=='csv':
            with path.open(encoding='utf-8-sig',newline='') as f: records=list(csv.DictReader(f))
        else: records=pq.read_table(path).to_pylist()
        assert len(records)==1 and records[0]['source']=='tushare'
        assert set(records[0])==set(result['fields'])
        for key,value in result['rows'][0].items():
            actual=records[0][key]
            if value is None: assert actual in ('',None)
            elif isinstance(value,Decimal): assert Decimal(actual)==value
            elif hasattr(value,'isoformat'): assert actual==value.isoformat()
            else: assert actual==str(value)
        note=json.loads(path.with_suffix(path.suffix+'.json').read_text('utf-8'))
        assert note['units']==result['units'] and note['provider']=='tushare'


@pytest.mark.postgres
def test_report_cancel_failure_empty_and_checkpoint_resume(extension_app,monkeypatch):
    app=extension_app;store=app.store
    p=dict(source='tushare',resource='warehouse',exchange='SHFE',symbol='CU',start='2026-08-01',end='2026-09-14')
    calls=[]
    def request(self,api,params,fields=''):
        if api=='fut_wsr':
            calls.append(params['start_date'])
            if len(calls)==2: raise SourceError('仓单权限不足','permission')
            return [dict(trade_date=params['start_date'],symbol='CU',exchange='SHFE',warehouse='仓库',unit='吨',vol=1)]
        return fake_request(self,api,params,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    job=app.dispatch('POST','/futures/sync',p);app.tushare_worker.execute(store.job(job['id']))
    failed=store.job(job['id'])
    assert failed['state']=='partial' and failed['checkpoint']==2 and failed['result']['rows']==1
    retried=store.retry_job(job['id']);app.tushare_worker.execute(retried)
    assert calls.count('20260801')==1
    assert store.job(retried['id'])['state']=='partial'
    cancelled=app.dispatch('POST','/futures/sync',dict(p,start='2026-09-14'))
    def cancel(self,api,params,fields=''):
        store.update_job(cancelled['id'],cancel_requested=True)
        return fake_request(self,api,params,fields)
    monkeypatch.setattr(TushareSource,'request',cancel)
    before=store.query('SELECT count(*) AS n FROM futures_warehouse_receipts',one=True)['n']
    app.tushare_worker.execute(store.job(cancelled['id']))
    assert store.job(cancelled['id'])['state']=='cancelled' and store.job(cancelled['id'])['checkpoint']==0
    assert store.query('SELECT count(*) AS n FROM futures_warehouse_receipts',one=True)['n']==before
    monkeypatch.setattr(TushareSource,'request',lambda self,api,p,fields='':[] if api=='fut_wsr' else fake_request(self,api,p,fields))
    empty=app.dispatch('POST','/futures/sync',dict(p,start='2026-09-14'));app.tushare_worker.execute(store.job(empty['id']))
    assert store.job(empty['id'])['state']=='partial'


def test_ine_holdings_use_official_shfe_entry(monkeypatch):
    calls=[]
    def request(self,api,p,fields=''):
        calls.append(p);return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    rows=TushareSource(account()).report(dict(resource='holding',exchange='INE',symbol='SC'),'2026-09-14','2026-09-14')
    assert calls[0]['exchange']=='SHFE' and rows[0]['exchange']=='SHFE'


@pytest.mark.postgres
def test_permission_guard_does_not_block_weekly_and_mapping_requires_continuous(extension_app):
    app=extension_app
    app.capabilities['main']={'capabilities':{'minutes':{'state':'permission'}}}
    dataset=app.dispatch('POST','/datasets',dict(source='tushare',members=['CU.SHF'],name='铜周月',periods=['1w','1mo']))
    assert dataset['periods']==['1w','1mo']
    with pytest.raises(ValueError,match='分钟'):
        app.dispatch('POST','/datasets',dict(source='tushare',members=['CU2610.SHF'],name='分钟',periods=['15m']))
    with pytest.raises(ValueError,match='主力'):
        app.dispatch('POST','/futures/sync',dict(source='tushare',resource='mapping',code='CU2610.SHF',start='2026-09-14',end='2026-09-14'))
    with pytest.raises(ValueError,match='Tushare'):
        app.dispatch('POST','/futures/sync',dict(source='qmt',resource='calendar',exchange='SHFE',start='2026-09-14',end='2026-09-14'))
    with pytest.raises(ValueError):
        request_chunks(dict(resource='holding',code='',symbol='CU',exchange='SHFE',start='2026-01-01',end='2100-01-01'))


@pytest.mark.postgres
def test_report_pagination_and_offline_query(extension_app):
    app=extension_app
    p=dict(source='tushare',resource='calendar',exchange='SHFE',start='2026-09-01',end='2026-09-14')
    job=app.dispatch('POST','/futures/sync',p);app.tushare_worker.execute(app.store.job(job['id']))
    app.settings.accounts=[]
    first=app.dispatch('GET','/futures/records',dict(p,limit=5))
    second=app.dispatch('GET','/futures/records',dict(p,limit=5,offset=first['next_offset']))
    assert first['next_offset']==5 and second['rows'][0]['day']>first['rows'][-1]['day']
    assert len(page(app.store,p,5000)['rows'])==14


@pytest.mark.postgres
def test_closed_calendar_dates_never_enter_schedule_fallback(extension_app,monkeypatch):
    app=extension_app;store=app.store
    p=dict(source='tushare',resource='calendar',exchange='SHFE',start='2026-09-07',end='2026-09-14')
    job=app.dispatch('POST','/futures/sync',p);app.tushare_worker.execute(store.job(job['id']))
    dataset=app.dispatch('POST','/datasets',dict(source='tushare',members=['CU2610.SHF'],name='铜周线',periods=['1w'],scheduled=True))
    store.query('UPDATE datasets SET schedule_from=%s WHERE id=%s',(day('2026-09-12'),dataset['id']))
    original=app.tushare_worker.calendar
    def calendar(code,start,end,count=-1,adapter=None):
        if count==-1: raise ConnectionError('offline')
        return original(code,start,end,count,adapter)
    monkeypatch.setattr(app.tushare_worker,'calendar',calendar)
    app.tushare_worker.schedule(datetime(2026,9,14,19,tzinfo=SHANGHAI))
    app.tushare_worker.schedule(datetime(2026,9,14,19,tzinfo=SHANGHAI))
    jobs=store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL')
    assert len(jobs)==1 and jobs[0]['payload']['end']=='2026-09-14'
    assert jobs[0]['payload']['periods']==['1w']


@pytest.mark.postgres
def test_report_network_retry_and_transaction_rollback(extension_app,monkeypatch):
    app=extension_app;store=app.store
    p=dict(source='tushare',resource='holding',exchange='SHFE',symbol='CU',start='2026-09-14',end='2026-09-14')
    calls=[]
    def offline(self,api,params,fields=''):
        calls.append(api);raise ConnectionError('offline')
    monkeypatch.setattr(TushareSource,'request',offline)
    monkeypatch.setattr(app.tushare_worker.stop,'wait',lambda _:False)
    job=app.dispatch('POST','/futures/sync',p);app.tushare_worker.execute(store.job(job['id']))
    assert calls==['fut_holding']*4 and store.job(job['id'])['checkpoint']==0
    monkeypatch.setattr(TushareSource,'request',fake_request)
    def fail(*args,**kwargs): raise OSError('storage failed')
    monkeypatch.setattr(store,'write_coverage',fail)
    app.tushare_worker.execute(store.job(job['id']))
    assert store.job(job['id'])['checkpoint']==0
    assert not store.query('SELECT * FROM futures_holdings')


def test_sdk_report_operations_keep_source_and_account(monkeypatch):
    from pfor_qmt.sdk import DataClient
    calls=[];client=DataClient()
    monkeypatch.setattr(client,'request',lambda path,payload=None,**query:calls.append((path,payload,query)))
    client.sync_futures('calendar','2026-09-14','2026-09-17',account_id='main',exchange='SHFE')
    client.futures_records('holding','2026-09-14','2026-09-17',exchange='SHFE',symbol='CU',offset=200)
    client.export_futures('warehouse','2026-09-14','2026-09-17',format='parquet',exchange='SHFE',symbol='CU')
    assert calls[0][1]['account_id']=='main' and calls[0][1]['source']=='tushare'
    assert calls[1][2]['offset']==200 and calls[2][1]['format']=='parquet'


@pytest.mark.postgres
def test_corrected_daily_report_replaces_members_but_empty_does_not_erase(extension_app,monkeypatch):
    from pfor_qmt.futures import write_records
    store=extension_app.store
    raw=dict(trade_date='20260914',symbol='CU',exchange='SHFE',broker='会员一',vol=3)
    rows=normalize_report('holding',[raw,dict(raw,broker='会员二')],'SHFE','CU',day('2026-09-14'),day('2026-09-14'))
    with store.connect() as conn: write_records(conn,'holding',rows)
    with store.connect() as conn: write_records(conn,'holding',rows[:1])
    with store.connect() as conn: write_records(conn,'holding',[])
    assert store.query('SELECT broker FROM futures_holdings')==[{'broker':'会员一'}]


def test_mapping_conflicts_are_not_silently_overwritten(monkeypatch):
    rows=[dict(ts_code='CU.SHF',trade_date='20260914',mapping_ts_code=code) for code in ['CU2610.SHF','CU2611.SHF']]
    monkeypatch.setattr(TushareSource,'request',lambda *a,**k:rows)
    with pytest.raises(SourceError,match='冲突'):
        TushareSource(account()).mapping('CU.SHF','2026-09-14','2026-09-14')
