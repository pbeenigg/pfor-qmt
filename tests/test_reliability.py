import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pfor_qmt.data import SHANGHAI, day, normalize_bars
from pfor_qmt.maintenance import save_plan, tick, repair
from pfor_qmt.reliability import DataRejected, classify_gaps, redact
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.storage import document
from pfor_qmt.tasks import Worker, LeaseLost
from pfor_qmt.tushare import TushareSource, SourceError
from test_storage_tasks import Source, make_job
from test_tushare import account, seed, download_job, fake_request


def bars(records):
    return normalize_bars(records,'CU2610.SHF','1d',day('2026-09-14'),day('2026-09-14'),'tushare')


def test_conflicts_are_not_last_row_wins_and_identical_duplicates_collapse():
    row=dict(time='20260914',open='1.0000000000001',high='2',low='1',close='2')
    assert len(bars([row,row]))==1
    with pytest.raises(DataRejected,match='冲突') as caught:
        bars([row,dict(row,close='1')])
    assert caught.value.code=='DUPLICATE_CONFLICT'
    assert caught.value.sample['first']['open']==Decimal('1.0000000000001')


@pytest.mark.parametrize('changes',[{'open':3},{'close':0},{'high':0},{'volume':-1},{'open_interest':-1}])
def test_price_range_and_quantity_validation(changes):
    with pytest.raises(DataRejected):
        bars([dict(time='20260914',open=1,high=2,low=1,close=2,**{key:value for key,value in changes.items() if key not in ('open','high','close')}) | changes])


def test_negative_prices_are_not_rejected_and_null_stays_null():
    row=bars([dict(time='20260914',open=-2,high=-1,low=-3,close=-1,volume=None)])[0]
    assert row['open']==-2 and row['volume'] is None


def test_unknown_report_publication_is_not_fabricated_as_missing():
    gaps=classify_gaps([{'reason':'交易日无资料，待核验'}])
    assert gaps[0]['quality_state']=='pending_verification' and not gaps[0]['retryable']
    assert redact({'token':'abc','context':{'password':'def'},'url':'https://user:pw@host/?token=abc'})=={'token':'[redacted]','context':{'password':'[redacted]'},'url':'[endpoint]'}


@pytest.mark.postgres
def test_invalid_contract_isolated_and_retry_preserves_original(store,tmp_path):
    source=Source()
    original=source.get_local_data
    def read(**params):
        if params['stock_list']==['000300.SH']:
            return {'000300.SH':[dict(time='20260914',open=5,high=2,low=1,close=2)]}
        return original(**params)
    source.get_local_data=read
    worker=Worker(store,tmp_path,source=source)
    job=make_job(store,['000300.SH','000001.SZ']);worker.execute(job)
    saved=store.job(job['id'])
    assert saved['state']=='partial' and saved['checkpoint']==2
    assert store.history('000300.SH')['rows']==[] and len(store.history('000001.SZ')['rows'])==1
    units=store.units_page(job['id'])['rows']
    assert [row['quality_state'] for row in units]==['rejected','verified']
    event=next(row for row in store.events_page({'job_id':str(job['id'])})['rows'] if row['code']=='OHLC_RANGE')
    assert event['sample']['open']=='5'
    retry=store.retry_job(job['id'],[0])
    assert retry['parent_id']==job['id'] and len(retry['payload']['chunks'])==1
    assert store.retry_job(job['id'])['id']==retry['id']
    source.get_local_data=original;source.downloads.clear();worker.execute(retry)
    assert [row[0] for row in source.downloads]==[['000300.SH']]
    assert store.job(retry['id'])['state']=='succeeded'
    assert store.job(job['id'])['state']=='partial'
    with pytest.raises(ValueError,match='没有可重试'):store.retry_job(job['id'])


@pytest.mark.postgres
def test_permission_blocks_only_interface_and_does_not_loop(store,tmp_path,monkeypatch):
    seed(store);calls=[]
    def request(self,api,p,fields=''):
        calls.append(api)
        if api=='ft_mins':raise SourceError('分钟权限不足','permission')
        return fake_request(self,api,p,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    job=download_job(store,periods=['1m','5m','1d'])
    Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(job)
    assert store.job(job['id'])['state']=='partial'
    assert calls.count('ft_mins')==1 and calls.count('fut_daily')==1
    units=store.units_page(job['id'])['rows']
    assert [row['state'] for row in units]==['blocked','blocked','succeeded']
    assert not units[0]['retryable']


@pytest.mark.postgres
def test_lifecycle_outside_range_no_request_and_no_fake_rows(store,tmp_path,monkeypatch):
    seed(store)
    store.query("UPDATE securities SET metadata=metadata || %s WHERE source='tushare'",(document({'listed':'20260915','expiry':'20261015'}),))
    monkeypatch.setattr(TushareSource,'request',lambda *args:pytest.fail('Outside lifecycle must not be requested'))
    job=download_job(store);Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(job)
    assert store.job(job['id'])['state']=='succeeded'
    assert store.units_page(job['id'])['rows'][0]['quality_state']=='not_applicable'
    assert not store.query('SELECT * FROM bars')


@pytest.mark.postgres
def test_events_pagination_redaction_retention_and_health(store,tmp_path):
    job=make_job(store)
    for index in range(3):store.event(job['id'],'TEST','safe',context={'token':'private'},sample={'password':'private'})
    first=store.events_page({'job_id':str(job['id']),'limit':2})
    second=store.events_page({'job_id':str(job['id']),'limit':2,'before':first['next_before']})
    assert len(second['rows'])==1
    assert not {row['id'] for row in first['rows']} & {row['id'] for row in second['rows']}
    assert 'private' not in json.dumps(first,default=str)
    store.query("UPDATE job_events SET created_at=now()-interval '31 days' WHERE id=%s",(first['rows'][0]['id'],))
    store.query("UPDATE job_events SET created_at=now()-interval '91 days' WHERE id=%s",(first['rows'][1]['id'],))
    store.housekeeping()
    rows=store.events_page({'job_id':str(job['id'])})['rows']
    assert len(rows)==2 and next(row for row in rows if row['id']==first['rows'][0]['id'])['sample'] is None
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    health=app.dispatch('GET','/health',{})
    assert health['database']['version']==6 and health['database_free_space']['state']=='unverified'
    assert health['runtime_disk']['free_bytes']>0


@pytest.mark.postgres
def test_maintenance_explicit_scope_calendar_and_idempotency(store,tmp_path,monkeypatch):
    seed(store)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    job=download_job(store)
    plan=save_plan(store,{'job_id':str(job['id']),'name':'铜日线维护'})
    monkeypatch.setattr(TushareSource,'calendar',lambda self,market,start,end: [day('2026-09-08'),day('2026-09-09'),day('2026-09-10'),day('2026-09-11'),day('2026-09-14')])
    tick(worker,datetime(2026,9,14,18,59,tzinfo=SHANGHAI))
    assert len(store.query('SELECT * FROM jobs'))==1
    tick(worker,datetime(2026,9,14,19,tzinfo=SHANGHAI));tick(worker,datetime(2026,9,14,19,tzinfo=SHANGHAI))
    jobs=store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL')
    assert len(jobs)==1
    assert jobs[0]['payload']['members']==['CU2610.SHF'] and jobs[0]['payload']['start']=='2026-09-08'
    assert jobs[0]['payload']['account_id']=='main'
    with pytest.raises(ValueError,match='已有'):save_plan(store,{'job_id':str(job['id']),'name':'重复'})
    store.query('UPDATE maintenance_plans SET enabled=false WHERE id=%s',(plan['id'],))
    tick(worker,datetime(2026,9,15,19,tzinfo=SHANGHAI))
    assert len(store.query('SELECT * FROM jobs'))==2


@pytest.mark.postgres
def test_bounded_auto_repair_does_not_touch_manual_or_permission_failures(store,tmp_path):
    source=Source();source.fail=ConnectionError('sensitive')
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda _:False
    manual=make_job(store);worker.execute(manual)
    plan=save_plan(store,{'job_id':str(manual['id']),'name':'自动范围'})
    job=make_job(store);store.update_job(job['id'],payload=dict(job['payload'],maintenance_id=str(plan['id'])))
    worker.execute(store.job(job['id']))
    now=datetime.now(SHANGHAI)+timedelta(hours=2)
    repair(worker,now);repair(worker,now)
    children=store.query('SELECT * FROM jobs WHERE parent_id IS NOT NULL')
    assert len(children)==1 and children[0]['parent_id']==job['id']
    assert children[0]['payload']['repair_attempt']==1
    child=children[0];store.update_job(child['id'],payload=dict(child['payload'],repair_attempt=3))
    worker.execute(store.job(child['id']));repair(worker,now+timedelta(days=2))
    assert len(store.query('SELECT * FROM jobs WHERE parent_id IS NOT NULL'))==1


@pytest.mark.postgres
def test_unknown_minute_session_not_retried_indefinitely(store,tmp_path,monkeypatch):
    seed(store);monkeypatch.setattr(TushareSource,'request',fake_request)
    job=download_job(store,periods=['1m'])
    Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(job)
    with pytest.raises(ValueError,match='没有可重试'):store.retry_job(job['id'])
    assert store.job(job['id'])['state']=='partial'


@pytest.mark.postgres
def test_legacy_completed_filter_and_retry_api_compatibility(store,tmp_path):
    worker=Worker(store,tmp_path,source=Source());job=make_job(store);worker.execute(job)
    assert store.jobs_page({'states':['completed']})['rows'][0]['id']==job['id']


@pytest.mark.postgres
def test_repeat_write_reports_changes_and_is_transactional(store,tmp_path,monkeypatch):
    worker=Worker(store,tmp_path,source=Source())
    first,second=make_job(store),make_job(store)
    worker.execute(first);worker.execute(second)
    assert store.units_page(first['id'])['rows'][0]['stats']==dict(read=1,inserted=1,updated=0,unchanged_or_older=0)
    assert store.units_page(second['id'])['rows'][0]['stats']==dict(read=1,inserted=0,updated=0,unchanged_or_older=1)
    third=make_job(store,['000002.SZ'])
    original=store.write_coverage
    def fail_after_write(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('simulated commit failure')
    monkeypatch.setattr(store,'write_coverage',fail_after_write)
    worker.execute(third)
    assert not store.history('000002.SZ')['rows']
    assert not store.query('SELECT * FROM coverage WHERE job_id=%s',(third['id'],))
    assert store.job(third['id'])['checkpoint']==0
    assert store.units_page(third['id'])['rows'][0]['state']=='failed'


@pytest.mark.postgres
@pytest.mark.parametrize('resource',['calendar','mapping','warehouse','holding'])
def test_all_report_maintenance_reuses_exact_selection(store,tmp_path,monkeypatch,resource):
    from pfor_qmt.futures import request_chunks
    p=dict(source='tushare',account_id='main',endpoint=account()['endpoint'],resource=resource,exchange='SHFE',symbol='CU',code='CU.SHF' if resource=='mapping' else '',start='2026-09-14',end='2026-09-14')
    p['chunks']=request_chunks(p)
    job=store.create_job('download',p)
    save_plan(store,{'job_id':str(job['id']),'name':resource})
    monkeypatch.setattr(TushareSource,'calendar',lambda *args:[day('2026-09-08'),day('2026-09-09'),day('2026-09-10'),day('2026-09-11'),day('2026-09-14')])
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    tick(worker,datetime(2026,9,14,19,tzinfo=SHANGHAI))
    created=store.query('SELECT payload FROM jobs WHERE schedule_key IS NOT NULL',one=True)['payload']
    assert created['source']=='tushare' and created['resource']==resource and created['symbol']=='CU'
    assert created['start']=='2026-09-08' and created['end']=='2026-09-14'
    assert created['account_id']=='main' and created['endpoint']==p['endpoint']


@pytest.mark.postgres
def test_partial_repair_does_not_hide_other_unresolved_units(store,tmp_path):
    worker=Worker(store,tmp_path,source=Source());job=make_job(store,['000300.SH','000001.SZ'])
    worker.source.fail=ValueError('bad');worker.execute(job)
    child=store.retry_job(job['id'],[0])
    with pytest.raises(ValueError,match='不同范围'):store.retry_job(job['id'],[1])
    worker.source.fail=None;worker.execute(child)
    health=store.operations_health()
    assert any(row['id']==job['id'] for row in health['attention'])
    assert sum(row['count'] for row in health['quality'] if row['quality_state']=='rejected')==1


@pytest.mark.postgres
def test_lost_lease_after_source_request_cannot_commit(store,tmp_path):
    worker=Worker(store,tmp_path,source=Source());job=make_job(store)
    with store.connect() as lease:
        worker.leases.connection=lease
        worker.source.on_download=lease.close
        with pytest.raises(LeaseLost):worker.execute(job)
    assert not store.query('SELECT * FROM bars')
    assert not store.query('SELECT * FROM coverage')
    assert store.job(job['id'])['checkpoint']==0


@pytest.mark.postgres
@pytest.mark.parametrize('missing_account',[True,False])
def test_account_precondition_is_blocked_not_invalid_data(store,tmp_path,missing_account):
    def resolve(identifier):
        if missing_account:raise ValueError('账号未配置Token')
        return dict(account(),endpoint='https://other.example/api')
    job=download_job(store)
    Worker(store,tmp_path,provider='tushare',account_resolver=resolve).execute(job)
    saved=store.job(job['id'])
    assert saved['state']=='blocked' and saved['error_code']=='SOURCE_CONFIGURATION'
    assert saved['checkpoint']==0 and not store.query('SELECT * FROM bars')
