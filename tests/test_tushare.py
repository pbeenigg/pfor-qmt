import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pfor_qmt.accounts import profile
from pfor_qmt.data import SHANGHAI, chunks, day
from pfor_qmt.identifiers import source_code
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tasks import Worker
from pfor_qmt.tushare import TushareSource, SourceError, RateLimiter


def account(**overrides):
    return profile({'id':'main', 'token':'synthetic-test-token', 'requests_per_minute':100000, **overrides})


def daily(code='CU2610.SHF'):
    return {'ts_code':code,'trade_date':'20260914','open':'80000.00000000000001','high':'81000','low':'79000',
            'close':'80123.1234567890123456789','vol':'1152921504606847013','amount':'12345.67890123456789','oi':42,'settle':80010,'pre_settle':79999}


def seed(store):
    metadata = {'product':'CU','delivery_month':'202610','listed':'20250101','expiry':'20261031'}
    store.save_security('CU2610.SHF','沪铜2610','future',{},subtype='contract',metadata=metadata,source='tushare')
    store.save_security('cu2610.SF','沪铜2610','future',{},subtype='contract',metadata=metadata)


def download_job(store, **overrides):
    payload = dict(source='tushare',account_id='main',endpoint='https://api.tushare.pro',members=['CU2610.SHF'],periods=['1d'],start='2026-09-14',end='2026-09-14')
    payload.update(overrides)
    payload['chunks'] = chunks(payload)
    return store.create_job('download',payload)


def fake_request(self, api, params, fields=''):
    code = params.get('ts_code','CU2610.SHF')
    if api == 'fut_basic':
        exchanges={'SHFE':('CU','SHF'),'CFFEX':('IF','CFX'),'DCE':('A','DCE'),'CZCE':('AP','ZCE'),'INE':('SC','INE'),'GFEX':('SI','GFE')}
        product,market=exchanges[params['exchange']]
        symbol=product+'2610' if params['fut_type']=='1' else product
        return [dict(ts_code=symbol+'.'+market,symbol=symbol,exchange=params['exchange'],name='测试合约'+symbol,
                     fut_code=product,d_month='202610',list_date='20250101',delist_date='20261031',quote_unit='元/吨')]
    if api == 'fut_trade_cal':
        current=datetime.strptime(params['start_date'],'%Y%m%d').date()
        end=datetime.strptime(params['end_date'],'%Y%m%d').date()
        rows=[]
        while current<=end:
            rows.append(dict(exchange=params['exchange'],cal_date=current.strftime('%Y%m%d'),is_open=int(current.weekday()<5)))
            current+=timedelta(days=1)
        return rows
    if api == 'fut_daily':
        return [daily(code)]
    if api == 'fut_weekly_monthly':
        return [dict(daily(code),trade_date=params['end_date'],end_date='20260917',freq=params['freq'],pre_close='80000',oi_chg='1',exchange='SHFE',change1='3',change2='2')]
    if api == 'fut_wsr':
        return [dict(trade_date='20260914',symbol=params['symbol'],exchange=params['exchange'],warehouse='测试仓库',wh_id='01',pre_vol=10,vol=12,vol_chg=2,unit='吨')]
    if api == 'fut_holding':
        return [dict(trade_date='20260914',symbol=params['symbol'],exchange=params['exchange'],broker='测试会员',vol=12,long_hld=None,short_hld=7)]
    if api == 'ft_mins':
        return [dict(ts_code=code,trade_time='2026-09-14 21:01:00',open=1,high=2,low=1,close=2,vol=3,amount='12345.6789',oi=4)]
    if api == 'fut_mapping':
        return [dict(ts_code=code,trade_date='20260914',mapping_ts_code='CU2610.SHF')]
    raise AssertionError(api)


def test_accounts_roundtrip_environment_and_secrets(tmp_path, monkeypatch):
    settings=Settings(config_path=tmp_path/'config.toml')
    settings.accounts=[account(),account(id='secondary')]
    settings.default_account_id='main'
    settings.save()
    monkeypatch.setenv('PFOR_QMT_TUSHARE_MAIN_TOKEN','environment-secret')
    other=Settings(config_path=settings.path)
    assert other.account()['token']=='environment-secret'
    assert other.account('secondary')['token']=='synthetic-test-token'
    public=json.dumps(other.public_accounts())
    assert 'environment-secret' not in public and 'synthetic-test-token' not in public
    other.save()
    assert 'environment-secret' not in settings.path.read_text('utf-8')
    assert '[[tushare.accounts]]' in settings.path.read_text('utf-8')


@pytest.mark.parametrize('endpoint',['ftp://example.com','https://user:secret@example.com','https://example.com?token=secret','https://example.com#token','not-url'])
def test_endpoint_does_not_hide_credentials(endpoint):
    with pytest.raises(ValueError):
        account(endpoint=endpoint)


def test_http_decimal_protocol_and_error_redaction():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            body=b'{"code":0,"data":{"fields":["amount"],"items":[[1.12345678901234567890]]}}' if len(calls)==1 else json.dumps({'code':2002,'msg':'permission synthetic-test-token'}).encode()
            self.send_response(200);self.end_headers();self.wfile.write(body)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        adapter=TushareSource(account(endpoint=f'http://127.0.0.1:{server.server_port}'))
        rows=adapter.request('fut_daily',{'ts_code':'CU2610.SHF'})
        assert rows[0]['amount']==Decimal('1.12345678901234567890')
        assert calls[0]['api_name']=='fut_daily' and calls[0]['token']=='synthetic-test-token'
        with pytest.raises(SourceError) as caught:
            adapter.request('fut_daily',{})
        assert caught.value.category=='permission' and 'synthetic-test-token' not in str(caught.value)
    finally:
        server.shutdown();server.server_close();thread.join()


def test_normalization_precision_null_night_and_continuous_guard(monkeypatch):
    monkeypatch.setattr(TushareSource,'request',fake_request)
    adapter=TushareSource(account())
    row=adapter.history('CU2610.SHF','1d','2026-09-14','2026-09-14')[0]
    assert row['amount']==Decimal('123456789.0123456789')
    assert row['volume']==Decimal('1152921504606847013')
    assert row['close']==Decimal('80123.1234567890123456789')
    assert row['trading_day']==day('2026-09-14')
    minute=adapter.history('CU2610.SHF','1m','2026-09-14','2026-09-14')[0]
    assert minute['amount']==Decimal('12345.6789') and minute['trading_day'] is None
    assert minute['time'].hour==21 and minute['settlement'] is None
    with pytest.raises(SourceError,match='具体月份'):
        adapter.history('CU.SHF','1m','2026-09-14','2026-09-14','continuous')


def test_dce_monthly_average_catalog_codes(monkeypatch):
    symbols = ['L_F', 'L_FL', 'PP_F', 'PP_FL', 'V_F', 'V_FL']
    records = [dict(ts_code=symbol+'.DCE', symbol=symbol, exchange='DCE',
                    name='月均价主力或连续', fut_code=symbol.removesuffix('L') if symbol.endswith('FL') else symbol,
                    list_date=None, delist_date=None, d_month=None) for symbol in symbols]
    monkeypatch.setattr(TushareSource, 'request', lambda *args, **kwargs: records)
    rows = TushareSource(account()).catalog('DCE', '2')
    assert [row['code'] for row in rows] == [symbol+'.DCE' for symbol in symbols]
    assert all(row['subtype'] == 'continuous' and row['metadata']['delivery_month'] is None for row in rows)
    assert source_code(' l_f.dce ', 'tushare') == 'L_F.DCE'


@pytest.mark.parametrize('code', ['L_F.SF', 'L_F.UNKNOWN', 'L/F.DCE', '../L_F.DCE', 'L_F.DCE;DROP', '_F.DCE'])
def test_tushare_code_validation_keeps_market_and_character_boundaries(code):
    with pytest.raises(ValueError):
        source_code(code, 'tushare')


def test_saturated_responses_split_or_fail(monkeypatch):
    calls=[]
    def request(self,api,params,fields=''):
        calls.append(params)
        return [{}]*2000 if params['start_date']!=params['end_date'] else [params['start_date']]
    monkeypatch.setattr(TushareSource,'request',request)
    adapter=TushareSource(account())
    result=adapter.bounded('fut_daily','CU2610.SHF',datetime(2026,9,10),datetime(2026,9,14))
    assert result==['20260910','20260911','20260912','20260913','20260914']
    monkeypatch.setattr(TushareSource,'request',lambda *a,**k:[{}]*2000)
    with pytest.raises(SourceError,match='最小区间'):
        adapter.bounded('fut_daily','CU2610.SHF',datetime(2026,9,14),datetime(2026,9,14))


def test_shared_token_limiter_and_cancel(monkeypatch):
    RateLimiter._buckets.clear()
    ticks=[0.0]
    monkeypatch.setattr('pfor_qmt.tushare.time.monotonic',lambda:ticks[0])
    monkeypatch.setattr('pfor_qmt.tushare.time.sleep',lambda _:ticks.__setitem__(0,ticks[0]+30))
    RateLimiter.acquire('shared',1,lambda:None)
    RateLimiter.acquire('shared',10,lambda:None)
    assert ticks[0]==60
    def cancel():
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        RateLimiter.acquire('shared',1,cancel)
    RateLimiter._buckets.clear()


@pytest.mark.postgres
def test_same_instrument_two_sources_and_account_independent_upsert(store,tmp_path,monkeypatch):
    seed(store)
    ids=store.query('SELECT instrument_id FROM securities')
    assert ids[0]['instrument_id']==ids[1]['instrument_id']
    monkeypatch.setattr(TushareSource,'request',fake_request)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    first=download_job(store);worker.execute(first)
    assert store.job(first['id'])['state']=='succeeded'
    second=download_job(store,account_id='secondary');worker.execute(second)
    assert store.job(second['id'])['state']=='succeeded'
    assert len(store.history('CU2610.SHF',source='tushare')['rows'])==1
    qmt=store.create_job('download',{'members':['cu2610.SF']})
    row=dict(store.history('CU2610.SHF',source='tushare')['rows'][0],code='cu2610.SF',close=Decimal('2'))
    store.write_chunk(qmt['id'],dict(code='cu2610.SF',period='1d',start='2026-09-14',end='2026-09-14'),[row],[],1)
    assert store.history('cu2610.SF')['rows'][0]['close']==2
    assert store.history('CU2610.SHF',source='tushare')['rows'][0]['close']!=2
    assert store.query('SELECT count(*) AS n FROM bars',one=True)['n']==2


@pytest.mark.postgres
def test_permissions_empty_response_and_cancel_do_not_report_success(store,tmp_path,monkeypatch):
    seed(store)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    job=download_job(store)
    calls=[]
    def permission(*args,**kwargs):
        calls.append(1);raise SourceError('权限不足','permission')
    monkeypatch.setattr(TushareSource,'request',permission)
    worker.execute(job)
    assert len(calls)==1 and store.job(job['id'])['checkpoint']==1 and store.job(job['id'])['state']=='blocked'
    monkeypatch.setattr(TushareSource,'request',lambda self,api,p,fields='': [] if api=='fut_daily' else fake_request(self,api,p,fields))
    empty=download_job(store);worker.execute(empty)
    assert store.job(empty['id'])['state']=='partial' and not store.history('CU2610.SHF',source='tushare')['rows']
    cancelled=download_job(store)
    def cancel(self,api,p,fields=''):
        store.update_job(cancelled['id'],cancel_requested=True)
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',cancel)
    worker.execute(cancelled)
    assert store.job(cancelled['id'])['state']=='cancelled' and store.job(cancelled['id'])['checkpoint']==0


@pytest.mark.postgres
def test_catalog_mapping_export_and_source_default(store,tmp_path,monkeypatch):
    import csv
    import pyarrow.parquet as pq
    monkeypatch.setattr(TushareSource,'request',fake_request)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    catalog=store.create_catalog_job(['future'],'tushare','main','https://api.tushare.pro')
    worker.execute(catalog)
    assert store.job(catalog['id'])['state']=='succeeded'
    assert store.catalog_page(source='tushare')['total']==12 and store.catalog_page()['total']==0
    job=download_job(store,members=['CU.SHF']);worker.execute(job)
    assert store.query('SELECT member_code FROM contract_mappings',one=True)['member_code']=='CU2610.SHF'
    normal=download_job(store);worker.execute(normal)
    for format in ('csv','parquet'):
        exported=store.create_job('export',dict(source='tushare',members=['CU2610.SHF'],period='1d',start='2026-09-14',end='2026-09-14',format=format))
        Worker(store,tmp_path).execute(exported)
        result=store.job(exported['id'])
        assert result['state']=='succeeded'
        path=tmp_path/'exports'/result['result']['file']
        if format=='csv':
            with path.open(encoding='utf-8-sig',newline='') as file:
                rows=list(csv.DictReader(file))
        else:
            rows=pq.read_table(path).to_pylist()
        assert rows[0]['source']=='tushare' and Decimal(rows[0]['amount'])==Decimal('123456789.0123456789')
        note=json.loads(path.with_suffix(path.suffix+'.json').read_text('utf-8'))
        assert note['provider']=='tushare' and note['units']['amount']=='元'


@pytest.mark.postgres
def test_account_binding_default_rotation_and_guarded_endpoint(store,tmp_path):
    seed(store)
    app=Application(Settings(config_path=tmp_path/'config.toml'),store=store)
    app.dispatch('POST','/sources/tushare/accounts',dict(id='main',token='first-secret'))
    created=app.dispatch('POST','/datasets',dict(source='tushare',name='铜',members=['CU2610.SHF'],periods=['1d']))
    app.dispatch('POST','/sources/tushare/accounts',dict(id='second',token='second-secret',default=True))
    job=app.dispatch('POST','/downloads',dict(dataset_id=str(created['id']),start='2026-09-14',end='2026-09-14'))
    assert job['payload']['account_id']=='main'
    with pytest.raises(ValueError,match='任务'):
        app.dispatch('POST','/sources/tushare/accounts',dict(id='main',endpoint='http://localhost:1234'))
    with pytest.raises(ValueError,match='引用'):
        app.dispatch('POST','/sources/tushare/accounts/main/delete',{})
    app.dispatch('POST','/sources/tushare/accounts',dict(id='main',token='rotated-secret'))
    assert app.settings.account('main')['token']=='rotated-secret'
    assert 'secret' not in json.dumps(app.dispatch('GET','/sources',{}))
    app.capabilities['main']={'capabilities':{'minutes':{'state':'permission'}}}
    with pytest.raises(ValueError,match='分钟接口'):
        app.dispatch('POST','/datasets',dict(source='tushare',account_id='main',name='分钟',members=['CU2610.SHF'],periods=['1m']))


@pytest.mark.postgres
def test_tushare_schedule_19_and_dedup(store,tmp_path,monkeypatch):
    seed(store)
    dataset=store.create_dataset(dict(source='tushare',account_id='main',endpoint='https://api.tushare.pro',members=['CU2610.SHF'],periods=['1d'],name='schedule',scheduled=True))
    store.query('UPDATE datasets SET schedule_from=%s WHERE id=%s',(day('2026-09-14'),dataset['id']))
    monkeypatch.setattr(TushareSource,'request',fake_request)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    worker.schedule(datetime(2026,9,14,18,tzinfo=SHANGHAI))
    assert not store.query('SELECT * FROM jobs')
    worker.schedule(datetime(2026,9,14,19,tzinfo=SHANGHAI));worker.schedule(datetime(2026,9,14,19,tzinfo=SHANGHAI))
    jobs=store.query('SELECT * FROM jobs')
    assert len(jobs)==1 and jobs[0]['payload']['source']=='tushare' and jobs[0]['payload']['start']=='2026-09-08'


@pytest.mark.postgres
def test_upgrade_v3_preserves_original_values_and_identity(store):
    from pathlib import Path
    from psycopg import sql
    with store.connect() as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(store.schema)))
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(store.schema)))
        for path in sorted((Path(__file__).resolve().parents[1]/'pfor_qmt'/'migrations').glob('00[1-3]_*.sql')):
            conn.execute(path.read_text('utf-8'))
            conn.execute('INSERT INTO schema_version(version) VALUES(%s) ON CONFLICT DO NOTHING',(int(path.stem[:3]),))
        conn.execute("INSERT INTO securities(code,name,kind,market,metadata) VALUES('cu2610.SF','沪铜2610','future','SF','{\"product\":\"cu\",\"expiry\":\"20261015\"}')")
        conn.execute("INSERT INTO bars(code,period,time,close,volume,amount) VALUES('cu2610.SF','1d','2026-09-14 00:00:00+08','80123.123456789012345678901234567890',42,NULL)")
        before=conn.execute('SELECT code,period,time,close,volume,amount,source FROM bars').fetchone()
    store.migrate();store.migrate()
    after=store.query('SELECT code,period,time,close,volume,amount,source FROM bars',one=True)
    assert before==after
    store.save_security('CU2610.SHF','沪铜2610','future',{},subtype='contract',metadata={'product':'CU','delivery_month':'202610'},source='tushare')
    ids=store.query('SELECT instrument_id FROM securities')
    assert ids[0]['instrument_id']==ids[1]['instrument_id']
    assert store.health()['version']==6


@pytest.mark.postgres
def test_independent_workers_do_not_wait_for_qmt(store,tmp_path,monkeypatch):
    import time
    seed(store)
    entered=threading.Event();release=threading.Event()
    class Qmt:
        def download_history_data2(self,*args):
            entered.set();release.wait(10)
            raise ConnectionError('QMT offline')
    monkeypatch.setattr(TushareSource,'request',fake_request)
    qmt=Worker(store,tmp_path,source=Qmt())
    ts=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    p=dict(members=['cu2610.SF'],periods=['1d'],start='2026-09-14',end='2026-09-14')
    p['chunks']=chunks(p)
    old=store.create_job('download',p)
    qmt.start();ts.start()
    try:
        assert entered.wait(5)
        job=download_job(store)
        until=time.monotonic()+5
        while time.monotonic()<until and store.job(job['id'])['state'] not in ('succeeded','failed'):
            time.sleep(.05)
        assert store.job(job['id'])['state']=='succeeded'
        assert store.job(old['id'])['state']=='running'
    finally:
        qmt.stop.set();ts.stop.set();release.set()
        qmt.thread.join(10);ts.thread.join(10)
        assert not qmt.thread.is_alive() and not ts.thread.is_alive()


def test_unknown_calendar_market_is_not_substituted(monkeypatch):
    calls=[]
    def request(self,api,params,fields=''):
        calls.append(params['exchange']);return []
    monkeypatch.setattr(TushareSource,'request',request)
    with pytest.raises(SourceError,match='GFEX'):
        TushareSource(account()).calendar('GF','2026-09-14','2026-09-14')
    assert calls==['GFEX']


@pytest.mark.postgres
def test_checkpoint_resume_uses_fixed_account_and_no_duplicate_rows(store,tmp_path,monkeypatch):
    seed(store)
    calls=[]
    def request(self,api,p,fields=''):
        calls.append((api,p.get('freq')))
        if api=='ft_mins' and calls.count(('ft_mins','1min'))==1:
            raise SourceError('分钟暂未授权','permission')
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda identifier:account() if identifier=='main' else (_ for _ in ()).throw(AssertionError()))
    job=download_job(store,periods=['1d','1m']);worker.execute(job)
    failed=store.job(job['id'])
    assert failed['state']=='partial' and failed['checkpoint']==2
    assert failed['result']['rows']==1
    assert store.query('SELECT result FROM jobs WHERE id=%s',(job['id'],),one=True)['result']['rows']==1
    retried=store.retry_job(job['id']);worker.execute(retried)
    assert store.job(retried['id'])['state']=='partial'
    assert calls.count(('fut_daily',None))==1
    assert store.query('SELECT count(*) AS n FROM bars',one=True)['n']==2


@pytest.mark.postgres
def test_daily_only_download_from_mixed_dataset_and_legacy_progress(store,tmp_path,monkeypatch):
    seed(store)
    app=Application(Settings(config_path=tmp_path/'config.toml'),store=store)
    app.settings.accounts=[account()]
    app.settings.default_account_id='main'
    dataset=app.dispatch('POST','/datasets',dict(source='tushare',name='铜多周期',members=['CU2610.SHF'],periods=['1d','1m','5m']))
    app.capabilities['main']={'capabilities':{'minutes':{'state':'permission'}}}
    request=dict(dataset_id=str(dataset['id']),start='2026-09-14',end='2026-09-14')
    with pytest.raises(ValueError,match='分钟接口'):
        app.dispatch('POST','/downloads',request)
    job=app.dispatch('POST','/downloads',dict(request,periods=['1d']))
    assert job['payload']['periods']==['1d'] and job['payload']['account_id']=='main'
    assert job['total_chunks']==1
    calls=[]
    def daily_only(self,api,p,fields=''):
        calls.append(api)
        assert api!='ft_mins'
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',daily_only)
    app.tushare_worker.execute(store.job(job['id']))
    assert store.job(job['id'])['state']=='succeeded' and 'fut_daily' in calls
    assert app.dispatch('GET','/datasets',{})[0]['periods']==['1d','1m','5m']
    # Old failed jobs have coverage but no result count. Reads must preserve their status.
    store.update_job(job['id'],state='failed',result={},error='ft_mins: 权限不足')
    detail=app.dispatch('GET','/jobs/'+str(job['id']),{})
    summary=next(row for row in app.dispatch('GET','/jobs',{}) if row['id']==job['id'])
    assert detail['result']['rows']==summary['result']['rows']==1
    assert detail['state']==summary['state']=='failed'
    for invalid in ([], ['tick'], '1d', None):
        with pytest.raises(ValueError):
            app.dispatch('POST','/downloads',dict(request,periods=invalid))
    narrow=app.dispatch('POST','/datasets',dict(source='tushare',name='铜日线',members=['CU2610.SHF'],periods=['1d']))
    with pytest.raises(ValueError,match='数据集'):
        app.dispatch('POST','/downloads',dict(request,dataset_id=str(narrow['id']),periods=['5m']))


@pytest.mark.postgres
def test_catalog_progress_survives_failure_and_resume(store,tmp_path,monkeypatch):
    calls=[]
    def request(self,api,p,fields=''):
        calls.append((p['exchange'],p['fut_type']))
        if len(calls)==6:
            raise SourceError('合约资料异常','invalid_response')
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    job=store.create_catalog_job(['future'],'tushare','main','https://api.tushare.pro')
    worker.execute(job)
    failed=store.job(job['id'])
    assert failed['state']=='partial' and failed['checkpoint']==12 and failed['result']['rows']==11
    retried=store.retry_job(job['id']);worker.execute(retried)
    result=store.job(retried['id'])
    assert result['state']=='succeeded' and result['result']['rows']==1
    assert store.query("SELECT count(*) AS n FROM securities WHERE source='tushare'",one=True)['n']==12
    assert calls.count(('DCE','2'))==2 and calls.count(('CFFEX','1'))==1


def test_sdk_download_optional_periods_preserves_old_default():
    from pfor_qmt.sdk import DataClient
    calls=[]
    client=DataClient()
    client.request=lambda path,payload: calls.append((path,payload))
    client.download('dataset','2026-09-14','2026-09-17')
    client.download('dataset','2026-09-14','2026-09-17',periods=['1d'])
    assert 'periods' not in calls[0][1] and calls[1][1]['periods']==['1d']
