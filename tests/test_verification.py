from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pfor_qmt.data import SHANGHAI, day, timestamp
from pfor_qmt.futures import normalize_report
from pfor_qmt.maintenance import save_plan, repair
from pfor_qmt.quality_checks import aggregate_issues, minute_issues
from pfor_qmt.reliability import issue
from pfor_qmt.tasks import Worker
from pfor_qmt.tushare import TushareSource, SourceError
from test_storage_tasks import Source, make_job
from test_tushare import account, seed, download_job, fake_request, daily


def test_aggregate_revisions_order_independent_and_equal_date_conflicts(monkeypatch):
    old=dict(daily(),trade_date='20260911',end_date='20260909',freq='week')
    new=dict(old,end_date='20260910',close='80000.000000000000123')
    source=TushareSource(account())
    for rows in ([old,new],[new,old],[old,new,new]):
        monkeypatch.setattr(source,'bounded',lambda *args:rows)
        result=source.history('CU2610.SHF','1w',day('2026-09-07'),day('2026-09-11'))
        assert len(result)==1 and result[0]['close']==Decimal(new['close'])
        assert result[0]['as_of_date']==day('2026-09-10')
    monkeypatch.setattr(source,'bounded',lambda *args:[new,dict(new,close='80001')])
    with pytest.raises(SourceError,match='冲突'):source.history('CU2610.SHF','1w',day('2026-09-07'),day('2026-09-11'))


def test_aggregate_holiday_finality_incomplete_calendar_and_stale():
    dates=[dict(day=day('2026-09-07')+timedelta(days=i),is_open=i<4) for i in range(5)]
    row=dict(period='1w',time=timestamp('20260911'),as_of_date=day('2026-09-10'))
    now=timestamp('20260912')
    assert aggregate_issues([row],dates,now)==[]
    assert aggregate_issues([row],dates,timestamp('20260911'))[0]['quality_state']=='not_published'
    assert aggregate_issues([row],dates[:-1],now)[0]['code']=='PERIOD_CALENDAR_UNKNOWN'
    assert aggregate_issues([dict(row,as_of_date=day('2026-09-09'))],dates,now)[0]['code']=='PERIOD_STALE'


def test_minute_intervals_are_observations_not_fake_missing_or_trade_days():
    rows=[dict(time=timestamp(value),trading_day=None) for value in ('2026-09-14 09:31','2026-09-14 09:34','2026-09-14 11:30','2026-09-14 13:01','2026-09-14 21:01')]
    issues=minute_issues(rows,'1m',{'trade_time_desc':'9:30-11:30,13:00-15:00'})
    gap=next(item for item in issues if item['code']=='MINUTE_INTERVAL_GAPS')
    assert gap['samples'][0]['after'].endswith('09:31:00+08:00')
    assert all('13:01' not in sample['before'] and '21:01' not in sample['before'] for sample in gap['samples'])
    assert all(item['quality_state']=='pending_verification' and not item['retryable'] for item in issues)
    assert all(row['trading_day'] is None for row in rows)


@pytest.mark.parametrize('resource,record',[('warehouse',dict(warehouse='test',unit='吨',vol=-1)),('holding',dict(broker='test',long_hld=-1))])
def test_report_negative_quantity_rejected(resource,record):
    with pytest.raises(ValueError,match='不得为负'):
        normalize_report(resource,[dict(record,trade_date='20260914',symbol='CU',exchange='SHFE')],'SHFE','CU',day('2026-09-14'),day('2026-09-14'))


@pytest.mark.postgres
def test_full_calendar_persists_closed_days_for_maintenance(store,tmp_path,monkeypatch):
    monkeypatch.setattr(TushareSource,'request',fake_request)
    worker=Worker(store,tmp_path,provider='tushare')
    dates=worker.calendar('SF','20260911','20260914',adapter=TushareSource(account()))
    assert dates==['20260911','20260914']
    rows=store.query("SELECT day,is_open FROM trading_dates WHERE source='tushare' ORDER BY day")
    assert [row['is_open'] for row in rows]==[True,False,False,True]


@pytest.mark.postgres
def test_offline_verification_preserves_prices_original_job_and_account_binding(store,tmp_path,monkeypatch):
    seed(store);monkeypatch.setattr(TushareSource,'request',fake_request)
    original=download_job(store)
    Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(original)
    before=store.job(original['id']);bars=store.query('SELECT * FROM bars')
    verification=store.create_verification(original['id'])
    assert store.create_verification(original['id'])['id']==verification['id']
    assert verification['kind']=='verify' and verification['parent_id'] is None
    monkeypatch.setattr(TushareSource,'request',lambda *args:pytest.fail('Verification must be offline'))
    worker=Worker(store,tmp_path,source=object(),account_resolver=lambda _:pytest.fail('No account needed'))
    worker.execute(verification)
    assert store.job(verification['id'])['state']=='succeeded'
    assert store.job(verification['id'])['result']['rule_version']=='coverage-v2'
    assert store.job(original['id'])==before and store.query('SELECT * FROM bars')==bars
    assert store.jobs_page({'kinds':['verify']})['total']==1


@pytest.mark.postgres
def test_verification_cancellation_and_resume_do_not_write_bars(store,tmp_path):
    original=make_job(store,['000300.SH','000001.SZ'])
    Worker(store,tmp_path,source=Source()).execute(original)
    bars=store.query('SELECT * FROM bars ORDER BY code')
    job=store.create_verification(original['id'])
    worker=Worker(store,tmp_path,source=object())
    worker.publish=lambda event:worker.stop.set() if event['data']['checkpoint']==1 else None
    worker.execute(job)
    assert store.job(job['id'])['state']=='queued' and store.job(job['id'])['checkpoint']==1
    worker.stop.clear();worker.publish=lambda _:None
    worker.execute(store.job(job['id']))
    assert store.job(job['id'])['state']=='succeeded'
    cancelled=store.create_verification(original['id'])
    store.update_job(cancelled['id'],cancel_requested=True)
    worker.execute(cancelled)
    assert store.job(cancelled['id'])['state']=='cancelled'
    assert store.query('SELECT * FROM bars ORDER BY code')==bars


@pytest.mark.postgres
def test_auto_repair_continues_other_units_after_manual_partial_child(store,tmp_path):
    worker=Worker(store,tmp_path,source=Source())
    original=make_job(store,['000300.SH','000001.SZ'])
    plan=save_plan(store,dict(job_id=str(original['id']),name='repair'))
    store.update_job(original['id'],payload=dict(original['payload'],maintenance_id=str(plan['id'])),state='partial')
    for index,part in enumerate(original['payload']['chunks']):
        store.write_unit(original['id'],index,part,'succeeded',[issue('BARS_MISSING','missing','missing','retry',True)])
    child=store.retry_job(original['id'],[0]);worker.execute(child)
    now=datetime.now(SHANGHAI)+timedelta(hours=2)
    repair(worker,now);repair(worker,now)
    children=store.query('SELECT * FROM jobs WHERE parent_id=%s ORDER BY created_at',(original['id'],))
    assert len(children)==2 and children[1]['payload']['chunks']==[original['payload']['chunks'][1]]
    assert children[1]['payload']['repair_attempt']==1
    worker.execute(children[1]);repair(worker,now+timedelta(days=2))
    assert len(store.query('SELECT * FROM jobs WHERE parent_id=%s',(original['id'],)))==2


@pytest.mark.postgres
@pytest.mark.parametrize('permission',[False,True])
def test_open_period_does_not_block_other_repairable_units(store,tmp_path,permission):
    job=make_job(store,['000300.SH','000001.SZ']);plan=save_plan(store,dict(job_id=str(job['id']),name='repair'))
    store.update_job(job['id'],payload=dict(job['payload'],maintenance_id=str(plan['id'])),state='partial',error_code='SOURCE_PERMISSION' if permission else None)
    gap=issue('SOURCE_PERMISSION','pending_verification','permission','enable permission',False) if permission else issue('PERIOD_OPEN','not_published','open','wait',True,day='2099-12-31')
    store.write_unit(job['id'],0,job['payload']['chunks'][0],'blocked' if permission else 'succeeded',[gap])
    store.write_unit(job['id'],1,job['payload']['chunks'][1],'succeeded',[issue('BARS_MISSING','missing','missing','retry',True)])
    repair(Worker(store,tmp_path),datetime.now(SHANGHAI)+timedelta(hours=2))
    child=store.query('SELECT * FROM jobs WHERE parent_id=%s',(job['id'],),one=True)
    assert child['payload']['chunks']==[job['payload']['chunks'][1]]


def test_runtime_logs_and_health_work_without_database(tmp_path):
    from pfor_qmt.service import Application
    from pfor_qmt.settings import Settings
    from pfor_qmt.reliability import runtime_events
    import json
    settings=Settings(config_path=tmp_path/'config.toml')
    path=settings.runtime/'worker-qmt.jsonl'
    path.write_text('\n'.join(json.dumps({'time':str(i),'source':'qmt','message':'token=do-not-expose','code':'DATABASE_ERROR'}) for i in range(110)),encoding='utf-8')
    app=Application(settings)
    health=app.dispatch('GET','/health',{})
    assert not health['database']['connected'] and health['database_bytes'] is None
    logs=app.dispatch('GET','/runtime/events',{})
    assert logs==runtime_events(settings.runtime) and len(logs['rows'])==100 and logs['truncated']
    assert 'do-not-expose' not in json.dumps(logs)


@pytest.mark.postgres
def test_catalog_freshness_requires_new_unit_evidence(store):
    from pfor_qmt.freshness import freshness_page
    job=store.create_catalog_job(['stock'])
    plan=save_plan(store,dict(job_id=str(job['id']),name='catalog'))
    part={'code':'000001.SZ','kind':'stock'}
    store.update_job(job['id'],payload=dict(job['payload'],maintenance_id=str(plan['id']),chunks=[part]),state='succeeded',checkpoint=1)
    now=datetime.now(SHANGHAI)
    assert freshness_page(store,{},now)['rows'][0]['freshness_state']=='pending_verification'
    store.write_unit(job['id'],0,part,'succeeded',[],1)
    assert freshness_page(store,{},now)['rows'][0]['freshness_state']=='current'
    assert freshness_page(store,{},now+timedelta(days=2))['rows'][0]['freshness_state']=='stale'


@pytest.mark.postgres
def test_maintenance_weekend_and_restart_catchup_same_rows(store,tmp_path,monkeypatch):
    from pfor_qmt.maintenance import tick
    seed(store)
    def request(self,api,params,fields=''):
        if api=='fut_daily':
            first,last=timestamp(params['start_date']).date(),timestamp(params['end_date']).date()
            return [dict(daily(params['ts_code']),trade_date=(first+timedelta(days=i)).strftime('%Y%m%d')) for i in range((last-first).days+1) if (first+timedelta(days=i)).weekday()<5]
        return fake_request(self,api,params,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    original=download_job(store,start='2026-08-31',end='2026-08-31');worker.execute(original)
    save_plan(store,dict(job_id=str(original['id']),name='cross-day'))
    friday=timestamp('2026-09-04 19:00')
    tick(worker,friday)
    first=store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL',one=True);worker.execute(first)
    assert store.job(first['id'])['state']=='succeeded'
    tick(worker,friday+timedelta(days=1))
    assert len(store.query('SELECT * FROM jobs'))==2
    restarted=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account())
    tick(restarted,friday+timedelta(days=4))
    second=store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL ORDER BY created_at DESC LIMIT 1',one=True)
    restarted.execute(second);tick(restarted,friday+timedelta(days=4))
    assert store.job(second['id'])['state']=='succeeded'
    assert len(store.query('SELECT * FROM jobs'))==3
    assert store.query('SELECT count(*) AS n FROM bars',one=True)['n']==7


@pytest.mark.postgres
def test_cancelled_repair_is_not_automatically_recreated(store,tmp_path):
    job=make_job(store);plan=save_plan(store,dict(job_id=str(job['id']),name='cancel repair'))
    store.update_job(job['id'],payload=dict(job['payload'],maintenance_id=str(plan['id'])),state='partial')
    store.write_unit(job['id'],0,job['payload']['chunks'][0],'succeeded',[issue('BARS_MISSING','missing','missing','retry',True)])
    child=store.retry_job(job['id'])
    store.update_job(child['id'],state='cancelled',cancel_requested=True)
    repair(Worker(store,tmp_path),datetime.now(SHANGHAI)+timedelta(days=2))
    assert len(store.query('SELECT * FROM jobs'))==2
    assert store.job(child['id'])['state']=='cancelled'
