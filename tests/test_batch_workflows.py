import csv
from decimal import Decimal

import pytest

from pfor_qmt.data import day
from pfor_qmt.futures import selections
from pfor_qmt.tushare import TushareSource
from test_futures_extensions import extension_app
from test_tushare import daily, fake_request


@pytest.fixture
def batch_app(extension_app, monkeypatch):
    app=extension_app
    for code,name,subtype in [('A2610.DCE','豆一2610','contract'),('A.DCE','豆一主力','continuous')]:
        app.store.save_security(code,name,'future',{},subtype=subtype,metadata={'product':'A','listed':'20250101','expiry':'20261231'},source='tushare')
    def request(self, api, params, fields=''):
        if api=='fut_mapping':
            code=params['ts_code']
            return [dict(ts_code=code,trade_date='20260914',mapping_ts_code='A2610.DCE' if code=='A.DCE' else 'CU2610.SHF')]
        return fake_request(self,api,params,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    return app


def targets(resource):
    if resource=='mapping':
        return [dict(resource=resource,code=code) for code in ('CU.SHF','A.DCE')]
    return [dict(resource=resource,exchange=exchange,symbol=symbol) for exchange,symbol in [('SHFE','CU'),('DCE','A')]]


def payload(items):
    return dict(source='tushare',selections=items,start='2026-09-14',end='2026-09-14',account_id='main')


@pytest.mark.postgres
def test_catalog_multiple_filters_and_all_matched_snapshot(batch_app):
    store=batch_app.store
    for i in range(63):
        store.save_security(f'T{i}.DCE',f'批量测试{i}','future',{},subtype='contract',source='tushare')
    p=dict(source='tushare',search='批量',kind=['future','stock'],market=['DF','SF'],subtype=['contract'])
    result=batch_app.dispatch('POST','/catalog/select',p)
    assert result['total']==63 and len(result['rows'])==63 and result['next_offset'] is None
    first=store.catalog_page(search='批量',kind='future,stock',market='DF,SF',source='tushare')
    assert first['total']==63 and len(first['rows'])==50
    assert len(store.catalog_page(search='批量',offset=50,source='tushare')['rows'])==13
    with pytest.raises(ValueError): store.catalog_page(kind=['future','invalid'])
    with pytest.raises(ValueError): store.catalog_page(market="DF'); DROP TABLE bars;--")


@pytest.mark.postgres
def test_select_all_refuses_silent_truncation(store):
    with store.connect() as conn:
        conn.execute("INSERT INTO securities(code,name,kind) SELECT lpad(i::text,6,'0')||'.SZ','证券'||i,'stock' FROM generate_series(1,10001) i")
    with pytest.raises(ValueError,match='未截断'):
        store.catalog_page(select_all=True)


@pytest.mark.postgres
@pytest.mark.parametrize('resource',['calendar','mapping','warehouse','holding'])
def test_multi_report_sync_pagination_and_exports(batch_app,resource):
    app=batch_app;store=app.store;p=payload(targets(resource)*2)
    result=app.dispatch('POST','/futures/sync',p)
    assert result['selection_count']==2 and len(result['jobs'])==1
    job=result['jobs'][0];assert job['total_chunks']==2
    app.tushare_worker.execute(store.job(job['id']))
    saved=store.job(job['id']);assert saved['state']=='succeeded' and saved['result']['rows']==2
    rows=[];offset=0
    while True:
        page=app.dispatch('POST','/futures/records',dict(p,limit=1,offset=offset))
        rows.extend(page['rows'])
        if page['next_offset'] is None: break
        offset=page['next_offset']
    assert len(rows)==2 and rows[0]!=rows[1]
    for fmt in ('csv','parquet'):
        exported=app.dispatch('POST','/futures/export',dict(p,format=fmt))['jobs'][0]
        app.worker.execute(store.job(exported['id']))
        job=store.job(exported['id']);assert job['state']=='succeeded' and job['result']['rows']==2
        path=app.worker.runtime/'exports'/job['result']['file']
        if fmt=='csv':
            with path.open(encoding='utf-8-sig',newline='') as stream: actual=list(csv.DictReader(stream))
        else:
            import pyarrow.parquet as pq
            actual=pq.read_table(path).to_pylist()
        assert len(actual)==2
        assert {row['source'] for row in actual}=={'tushare'}
    app.tushare_worker.execute(dict(saved,checkpoint=0))
    assert len(app.dispatch('POST','/futures/records',p)['rows'])==2


@pytest.mark.postgres
def test_report_batch_validation_atomicity_and_resource_separation(batch_app):
    app=batch_app;store=app.store
    bad=payload(targets('calendar')+[dict(resource='warehouse',exchange='DCE',symbol='UNKNOWN')])
    with pytest.raises(ValueError,match='产品'):
        app.dispatch('POST','/futures/sync',bad)
    assert store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==0
    p=payload(targets('calendar')+targets('warehouse'))
    created=app.dispatch('POST','/futures/sync',p)
    assert len(created['jobs'])==2 and created['selection_count']==4
    assert {job['payload']['resource'] for job in created['jobs']}=={'calendar','warehouse'}
    with pytest.raises(ValueError,match='不能混合'):
        app.dispatch('POST','/futures/records',p)
    app.capabilities['main']={'capabilities':{'warehouse':{'state':'permission'}}}
    with pytest.raises(ValueError,match='权限'):
        app.dispatch('POST','/futures/sync',p)
    assert store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==2


@pytest.mark.postgres
def test_batched_report_failure_checkpoint_and_cancel(batch_app,monkeypatch):
    app=batch_app;store=app.store
    original=TushareSource.request
    def fail_second(self,api,params,fields=''):
        if api=='fut_wsr' and params['exchange']=='DCE': raise ValueError('second target rejected')
        return original(self,api,params,fields)
    monkeypatch.setattr(TushareSource,'request',fail_second)
    job=app.dispatch('POST','/futures/sync',payload(targets('warehouse')))['jobs'][0]
    app.tushare_worker.execute(store.job(job['id']))
    failed=store.job(job['id']);assert failed['state']=='partial' and failed['checkpoint']==2
    assert failed['result']['rows']==1
    monkeypatch.setattr(TushareSource,'request',original)
    retried=store.retry_job(job['id']);app.tushare_worker.execute(retried)
    assert store.job(retried['id'])['checkpoint']==1
    assert store.job(retried['id'])['state']=='succeeded'
    again=app.dispatch('POST','/futures/sync',payload(targets('warehouse')))['jobs'][0]
    store.update_job(again['id'],cancel_requested=True)
    app.tushare_worker.execute(store.job(again['id']))
    assert store.job(again['id'])['checkpoint']==0


@pytest.mark.postgres
def test_download_batch_period_intersections_and_history_matrix(batch_app):
    app=batch_app;store=app.store
    datasets=[]
    for code,periods in [('CU2610.SHF',['1d']),('A2610.DCE',['1d','1w'])]:
        datasets.append(app.dispatch('POST','/datasets',dict(source='tushare',name=code,members=[code],periods=periods)))
    p=dict(source='tushare',dataset_ids=[str(row['id']) for row in datasets],periods=['1d','1w'],start='2026-09-14',end='2026-09-14')
    jobs=app.dispatch('POST','/downloads/batch',p)['jobs']
    assert [job['payload']['periods'] for job in jobs]==[['1d'],['1d','1w']]
    for job in jobs: app.tushare_worker.execute(store.job(job['id']))
    assert all(store.job(job['id'])['state'] in ('succeeded','partial') for job in jobs)
    query=dict(source='tushare',members=['CU2610.SHF','A2610.DCE'],periods=['1d','1w'],start=p['start'],end=p['end'],limit=1)
    rows=[];offset=0
    while True:
        page=app.dispatch('POST','/history/query',dict(query,offset=offset));rows.extend(page['rows'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert len(rows)==3 and len({(row['code'],row['period'],row['time']) for row in rows})==3
    assert {row['close'] for row in rows}=={Decimal(daily()['close'])}
    assert page['adjustment']=='none' and page['units']['amount']=='元'
    assert page['normalization_versions']=={'1d':'tushare-futures-v1','1w':'tushare-futures-v2'}
    assert {row['normalization_version'] for row in rows}=={'tushare-futures-v1','tushare-futures-v2'}
    for fmt in ('csv','parquet'):
        exports=app.dispatch('POST','/exports/batch',dict(query,format=fmt))['jobs']
        assert len(exports)==2
        for job in exports:app.worker.execute(store.job(job['id']))
        assert sum(store.job(job['id'])['result']['rows'] for job in exports)==3
    before=store.query('SELECT count(*) AS n FROM jobs',one=True)['n']
    with pytest.raises(ValueError,match='没有交集'):
        app.dispatch('POST','/downloads/batch',dict(p,periods=['1w']))
    assert store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==before
    with pytest.raises(ValueError,match='混合数据来源'):
        app.dispatch('POST','/downloads/batch',dict(p,source='qmt'))
    assert store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==before


def test_report_ranges_cannot_be_overridden_per_target():
    result=selections(payload([dict(resource='calendar',exchange='DCE',start='1900-01-01',end='2100-01-01')]))
    assert result[0]['start']==result[0]['end']=='2026-09-14'


@pytest.mark.postgres
def test_job_filters_page_beyond_legacy_limit_and_preserve_states(batch_app):
    store=batch_app.store
    jobs=store.create_jobs('export',[dict(source='tushare',format='csv') for _ in range(205)])
    for job in jobs[:3]:store.update_job(job['id'],state='failed',error='permission sample')
    result=batch_app.dispatch('POST','/jobs/query',dict(sources=['tushare'],states=['failed','succeeded'],search='permission',limit=2))
    assert result['total']==3 and len(result['rows'])==2 and result['next_offset']==2
    assert len(batch_app.dispatch('POST','/jobs/query',dict(sources='tushare',states='failed',offset=2))['rows'])==1
    result=batch_app.dispatch('POST','/jobs/query',dict(kinds='export',offset=200))
    assert result['total']==205 and len(result['rows'])==5 and result['next_offset'] is None
    assert batch_app.dispatch('POST','/jobs/query',dict(sources='qmt'))['total']==0
    with pytest.raises(ValueError):batch_app.dispatch('POST','/jobs/query',dict(states='unknown'))
    with pytest.raises(ValueError):batch_app.dispatch('POST','/jobs/query',dict(start='2026-09-18',end='2026-09-14'))


@pytest.mark.postgres
def test_catalog_selected_exchanges_and_running_scope_guard(batch_app):
    app=batch_app;store=app.store
    p=dict(source='tushare',kinds=['future'],exchanges=['SHFE','DCE'])
    job=app.dispatch('POST','/catalog/sync',p)
    assert app.dispatch('POST','/catalog/sync',dict(p,exchanges=['DCE']))['id']==job['id']
    with pytest.raises(ValueError,match='其他交易所'):
        app.dispatch('POST','/catalog/sync',dict(p,exchanges=['GFEX']))
    app.tushare_worker.execute(store.job(job['id']))
    saved=store.job(job['id'])
    assert saved['state']=='succeeded' and saved['checkpoint']==4
    assert {chunk['exchange'] for chunk in saved['payload']['chunks']}=={'SHFE','DCE'}
    assert not store.query("SELECT 1 FROM securities WHERE source='tushare' AND market='GF'")


def test_batch_sdk_preserves_arrays_and_source(monkeypatch):
    from pfor_qmt.sdk import DataClient
    calls=[];client=DataClient()
    monkeypatch.setattr(client,'request',lambda path,payload=None,**query:calls.append((path,payload,query)))
    client.query_history(['A.DCE','CU.SHF'],['1d','1w'],'2026-09-14','2026-09-17',source='tushare')
    client.sync_futures_batch(targets('calendar'),'2026-09-14','2026-09-17',account_id='main')
    client.download_batch(['dataset-1','dataset-2'],['1d'],source='tushare')
    assert calls[0][1]['periods']==['1d','1w']
    assert calls[1][1]['account_id']=='main' and len(calls[1][1]['selections'])==2
    assert calls[2][1]['source']=='tushare'
