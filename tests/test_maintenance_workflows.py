from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from pfor_qmt.data import SHANGHAI, day
from pfor_qmt.maintenance import save_plan, update_plan, set_dataset_schedule, tick
from pfor_qmt.tasks import Worker
from pfor_qmt.tushare import TushareSource, SourceError
from test_storage_tasks import Source, make_job
from test_tushare import seed, account, fake_request

pytestmark=pytest.mark.postgres
NOW=datetime(2026,9,14,19,tzinfo=SHANGHAI)


def dataset(store, **changes):
    value=store.create_dataset(dict(dict(name='scope',members=['000300.SH'],scheduled=True),**changes))
    store.query("UPDATE datasets SET schedule_from='2026-09-14' WHERE id=%s",(value['id'],))
    return value


def bound_plan(store, data):
    job=make_job(store)
    store.update_job(job['id'],payload=dict(job['payload'],dataset_id=str(data['id'])))
    return save_plan(store,dict(job_id=str(job['id']),name='linked'))


def test_schedule_idempotency_and_mutual_exclusion(store):
    data=dataset(store)
    set_dataset_schedule(store,data['id'],True,NOW+timedelta(days=2))
    assert store.query('SELECT schedule_from FROM datasets',one=True)['schedule_from']==day('2026-09-14')
    with pytest.raises(ValueError,match='已开启'):bound_plan(store,data)
    set_dataset_schedule(store,data['id'],False,NOW)
    plan=bound_plan(store,data)
    with pytest.raises(ValueError,match='已有启用'):set_dataset_schedule(store,data['id'],True,NOW)
    paused=update_plan(store,plan['id'],dict(enabled=False))
    changed=update_plan(store,plan['id'],dict(name='renamed',schedule_time='18:30',lookback_days=3))
    assert not changed['enabled'] and changed['payload']==paused['payload']
    assert changed['name']=='renamed' and str(changed['schedule_time'])=='18:30:00'
    assert update_plan(store,plan['id'],dict(enabled=False))['updated_at']==changed['updated_at']
    set_dataset_schedule(store,data['id'],True,NOW)
    with pytest.raises(ValueError,match='已开启'):update_plan(store,plan['id'],dict(enabled=True))
    for values in ({'payload':{}},{'lookback_days':0},{'schedule_time':'25:01'},{'schedule_time':42},{'enabled':'false'}):
        with pytest.raises(ValueError):update_plan(store,plan['id'],values)


def test_concurrent_enable_only_one_path_wins(store):
    data=dataset(store)
    set_dataset_schedule(store,data['id'],False,NOW)
    plan=bound_plan(store,data);update_plan(store,plan['id'],{'enabled':False})
    def call(function):
        try: function(); return True
        except ValueError: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(call,[lambda:set_dataset_schedule(store,data['id'],True,NOW),lambda:update_plan(store,plan['id'],{'enabled':True})]))
    assert sorted(results)==[False,True]
    assert store.query('SELECT scheduled FROM datasets',one=True)['scheduled'] != store.query('SELECT enabled FROM maintenance_plans',one=True)['enabled']


@pytest.mark.parametrize('plan_mode',[False,True])
def test_pause_during_calendar_read_prevents_enqueue(store,tmp_path,plan_mode):
    data=dataset(store)
    if plan_mode:
        set_dataset_schedule(store,data['id'],False,NOW);plan=bound_plan(store,data)
    source=Source();original=source.get_trading_dates
    def calendar(*args):
        if plan_mode:update_plan(store,plan['id'],{'enabled':False})
        else:set_dataset_schedule(store,data['id'],False,NOW)
        return original(*args)
    source.get_trading_dates=calendar
    Worker(store,tmp_path,source=source).schedule(NOW)
    assert not store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL')


def test_market_holidays_use_independent_cursors_and_legacy_keys(store,tmp_path):
    data=dataset(store,members=['000300.SH','000001.SZ'])
    old=dict(dataset_id=str(data['id']),members=data['members'],periods=['1d'],start='2026-09-07',end='2026-09-10')
    store.create_job('download',old,str(data['id'])+':2026-09-10')
    source=Source()
    source.get_trading_dates=lambda code,*args:['20260907','20260908','20260909','20260910','20260911']+(['20260914'] if code.endswith('.SH') else [])
    worker=Worker(store,tmp_path,source=source);worker.schedule(NOW);worker.schedule(NOW)
    jobs=store.query("SELECT payload FROM jobs WHERE payload ? 'schedule_market'")
    assert len(jobs)==2
    keyed={row['payload']['schedule_market']:row['payload'] for row in jobs}
    assert keyed['SH']['end']=='2026-09-14' and keyed['SZ']['end']=='2026-09-11'
    assert keyed['SH']['members']==['000300.SH'] and keyed['SZ']['members']==['000001.SZ']
    Worker(store,tmp_path,source=source).schedule(NOW)
    assert len(store.query('SELECT * FROM jobs'))==3


def test_concurrent_scheduler_deduplicates_queue_and_events(store,tmp_path):
    dataset(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:Worker(store,tmp_path,source=Source()).schedule(NOW),range(2)))
    assert len(store.query('SELECT * FROM jobs'))==1
    assert len(store.events_page({'code':'MAINTENANCE_QUEUED'})['rows'])==1


@pytest.mark.parametrize('plan_mode',[False,True])
@pytest.mark.parametrize('limited',[False,True])
def test_permission_isolated_cached_and_recovers_with_updated_token(store,tmp_path,monkeypatch,plan_mode,limited):
    seed(store);store.save_security('SI2611.GFE','sample','future',{},source='tushare',subtype='contract')
    data=store.create_dataset(dict(name='two markets',source='tushare',members=['CU2610.SHF','SI2611.GFE'],periods=['1d'],scheduled=not plan_mode,account_id='main',endpoint=account()['endpoint']))
    store.query("UPDATE datasets SET schedule_from='2026-09-14'")
    if plan_mode:
        payload={key:data[key] for key in ('source','members','periods','account_id','endpoint')}
        job=store.create_job('download',payload);save_plan(store,dict(job_id=str(job['id']),name='markets'))
    credential=account();calls=[];allowed=False
    def request(self,api,params,fields=''):
        calls.append(params.get('exchange'))
        if params.get('exchange')=='GFEX' and not allowed:raise SourceError('calendar denied','rate_limit' if limited else 'permission')
        return fake_request(self,api,params,fields)
    monkeypatch.setattr(TushareSource,'request',request)
    worker=Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:credential)
    worker.schedule(NOW);worker.schedule(NOW)
    scheduled=store.query('SELECT payload FROM jobs WHERE schedule_key IS NOT NULL')
    assert len(scheduled)==1 and scheduled[0]['payload']['members']==['CU2610.SHF']
    assert calls.count('GFEX')==1
    events=store.events_page({'code':'SCHEDULE_BLOCKED','sources':['tushare']})['rows']
    assert len(events)==1 and events[0]['source']=='tushare' and 'calendar denied' not in events[0]['message']
    allowed=True;credential=account(requests_per_minute=30) if limited else account(token='updated-synthetic-token')
    worker.schedule(NOW)
    scheduled=store.query('SELECT payload FROM jobs WHERE schedule_key IS NOT NULL')
    assert len(scheduled)==2 and {p['payload']['schedule_market'] for p in scheduled}=={'SF','GF'}
    assert len(store.events_page({'code':'SCHEDULE_RECOVERED','sources':['tushare']})['rows'])==1


def test_edit_during_read_defers_to_new_settings(store,tmp_path):
    job=make_job(store);plan=save_plan(store,dict(job_id=str(job['id']),name='plan'))
    source=Source();calendar=source.get_trading_dates
    def edit(*args):
        update_plan(store,plan['id'],{'lookback_days':3})
        return calendar(*args)
    source.get_trading_dates=edit
    worker=Worker(store,tmp_path,source=source);tick(worker,NOW)
    assert not store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL')
    source.get_trading_dates=calendar;tick(worker,NOW)
    payload=store.query('SELECT payload FROM jobs WHERE schedule_key IS NOT NULL',one=True)['payload']
    assert payload['start']=='2026-09-10'


def test_plan_cannot_resume_duplicate_scope(store):
    job=make_job(store)
    first=save_plan(store,dict(job_id=str(job['id']),name='first'))
    update_plan(store,first['id'],{'enabled':False})
    second=save_plan(store,dict(job_id=str(job['id']),name='second'))
    with pytest.raises(ValueError,match='相同范围'):update_plan(store,first['id'],{'enabled':True})
    assert store.query('SELECT count(*) AS n FROM maintenance_plans WHERE enabled',one=True)['n']==1
    assert second['enabled']
    scheduled=store.create_job('download',dict(job['payload'],maintenance_id=str(second['id'])))
    with pytest.raises(ValueError,match='原始'):save_plan(store,dict(job_id=str(scheduled['id']),name='clone'))


def test_api_sdk_updates_plan_without_changing_scope(store,tmp_path,monkeypatch):
    from pfor_qmt.service import Application
    from pfor_qmt.settings import Settings
    from pfor_qmt.sdk import DataClient
    job=make_job(store);plan=save_plan(store,dict(job_id=str(job['id']),name='plan'))
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    client=DataClient()
    monkeypatch.setattr(client,'request',lambda path,payload=None: app.dispatch('POST',path,payload))
    result=client.update_maintenance(str(plan['id']),name='edited',lookback_days=7)
    assert result['payload']==plan['payload'] and result['lookback_days']==7
    assert client.set_maintenance(str(plan['id']),False)['enabled'] is False


def test_service_stop_during_calendar_read_does_not_enqueue_or_log_network_error(store,tmp_path,monkeypatch):
    data=dataset(store)
    store.query("UPDATE datasets SET schedule_from='2020-01-01',schedule_time='00:00' WHERE id=%s",(data['id'],))
    source=Source();worker=Worker(store,tmp_path,source=source)
    calendar=source.get_trading_dates
    def stop(*args):
        worker.stop.set()
        return calendar(*args)
    source.get_trading_dates=stop
    worker.run_queue('download')
    assert not store.query('SELECT * FROM jobs')
    assert not (tmp_path/'worker-qmt.jsonl').exists()
