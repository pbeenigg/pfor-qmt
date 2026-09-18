from datetime import datetime
from types import SimpleNamespace

import pytest

from pfor_qmt.data import SHANGHAI, day
from pfor_qmt.freshness import freshness_page
from pfor_qmt.maintenance import save_plan
from test_storage_tasks import make_job

pytestmark = pytest.mark.postgres


def at(date='2026-09-18', hour=18):
    return datetime.fromisoformat(date).replace(hour=hour,tzinfo=SHANGHAI)


def setup_scope(store, members=None, periods=None):
    dataset = store.create_dataset(dict(name='tracked',members=members or ['000300.SH'],periods=periods or ['1d'],scheduled=True))
    store.query("UPDATE datasets SET schedule_from='2026-09-14' WHERE id=%s",(dataset['id'],))
    for date,opened in [('2026-09-17',True),('2026-09-18',True),('2026-09-19',False),('2026-09-20',False)]:
        store.query("INSERT INTO trading_dates(source,market,day,is_open) VALUES('qmt','SH',%s,%s)",(date,opened))
    return dataset


def insert_bar(store, code='000300.SH', date='2026-09-17', source='qmt', period='1d'):
    store.query('INSERT INTO bars(source,code,period,time,open,high,low,close) VALUES(%s,%s,%s,%s,1,2,1,2)',(source,code,period,at(date,0)))


def test_cutoff_weekend_calendar_gaps_lifecycle_and_archive(store):
    dataset=setup_scope(store)
    store.create_dataset(dict(name='archive',members=['000001.SZ'],scheduled=False))
    insert_bar(store)
    before=freshness_page(store,{},at(hour=16))
    assert before['total']==1 and before['rows'][0]['freshness_state']=='current'
    after=freshness_page(store,{},at())['rows'][0]
    assert after['freshness_state']=='stale' and after['expected_day']==day('2026-09-18')
    assert after['actual_day']==day('2026-09-17')
    assert freshness_page(store,{},at('2026-09-20'))['rows'][0]['freshness_state']=='stale'
    store.query("DELETE FROM trading_dates WHERE day='2026-09-19'")
    assert freshness_page(store,{},at('2026-09-20'))['rows'][0]['reason_code']=='CALENDAR_UNKNOWN'
    store.save_security('000300.SH','sample','index',{},subtype='contract',metadata={'listed':'20260921'})
    assert freshness_page(store,{},at())['rows'][0]['freshness_state']=='not_applicable'
    store.query('UPDATE datasets SET schedule_from=%s WHERE id=%s',(day('2026-09-18'),dataset['id']))
    assert freshness_page(store,{},at(hour=16))['rows'][0]['freshness_state']=='not_due'
    store.query('UPDATE datasets SET scheduled=false')
    assert freshness_page(store,{},at())['total']==0


def test_expiry_clips_target_and_unknown_minutes_never_claim_current(store):
    setup_scope(store,periods=['1d','1m'])
    store.save_security('000300.SH','sample','index',{},subtype='contract',metadata={'expiry':'20260917'})
    insert_bar(store)
    insert_bar(store,date='2026-09-18',period='1m')
    rows=freshness_page(store,{},at())['rows']
    assert rows[0]['freshness_state']=='current' and rows[0]['expected_day']==day('2026-09-17')
    assert rows[1]['reason_code']=='SESSION_UNVERIFIED' and rows[1]['freshness_state']=='pending_verification'


def test_per_contract_pagination_source_isolation_and_no_false_current(store):
    setup_scope(store,members=['000300.SH','000301.SH'])
    insert_bar(store,date='2026-09-18')
    insert_bar(store,code='CU2610.SHF',source='tushare',date='2026-09-18')
    first=freshness_page(store,{'limit':1},at())
    second=freshness_page(store,{'limit':1,'offset':first['next_offset']},at())
    assert first['rows'][0]['freshness_state']=='current'
    assert second['rows'][0]['code']=='000301.SH' and second['rows'][0]['freshness_state']=='missing'
    assert second['next_offset'] is None and first['total']==2
    assert freshness_page(store,{'sources':['tushare']},at())['total']==0
    assert freshness_page(store,{'search':'000301'},at())['total']==1
    store.query("DELETE FROM trading_dates WHERE day='2026-09-18'")
    assert freshness_page(store,{},at())['rows'][0]['reason_code']=='CALENDAR_UNKNOWN'
    with pytest.raises(ValueError):freshness_page(store,{'limit':201},at())
    with pytest.raises(ValueError):freshness_page(store,{'sources':['bad']},at())


def test_manual_maintenance_reports_and_calendar_offline(store):
    setup_scope(store)
    store.query('UPDATE datasets SET scheduled=false')
    job=make_job(store)
    plan=save_plan(store,dict(job_id=str(job['id']),name='explicit'))
    assert freshness_page(store,{},at())['rows'][0]['scope_id']==str(plan['id'])
    assert freshness_page(store,{},at())['rows'][0]['freshness_state']=='missing'
    store.query('UPDATE maintenance_plans SET enabled=false')
    for resource in ['calendar','mapping','warehouse','holding']:
        payload=dict(resource=resource,source='tushare',account_id='main',endpoint='https://api.tushare.pro',exchange='INE',symbol='SC',code='SC.INE' if resource=='mapping' else '')
        job=store.create_job('download',payload)
        save_plan(store,dict(job_id=str(job['id']),name=resource))
    store.query("INSERT INTO futures_holdings(source,exchange,symbol,trade_date,broker) VALUES('tushare','SHFE','SC','2026-09-18','test')")
    rows={row['resource']:row for row in freshness_page(store,{},at(hour=20))['rows']}
    assert rows['calendar']['freshness_state']=='missing'
    assert rows['mapping']['reason_code']=='CALENDAR_UNKNOWN'
    assert rows['holding']['actual_day']==day('2026-09-18')
    assert rows['holding']['reason_code']==rows['warehouse']['reason_code']=='REPORT_PUBLICATION_UNVERIFIED'
    assert len(store.query('SELECT * FROM jobs'))==5
    store.query("INSERT INTO trading_dates(source,market,day,is_open) VALUES('tushare','INE','2026-09-18',true)")
    rows={row['resource']:row for row in freshness_page(store,{},at(hour=20))['rows']}
    assert rows['calendar']['freshness_state']=='current'
    assert rows['mapping']['freshness_state']=='missing'


def test_health_and_sdk_use_same_readonly_page(store,tmp_path,monkeypatch):
    from pfor_qmt import freshness
    from pfor_qmt.sdk import DataClient
    from pfor_qmt.service import Application
    from pfor_qmt.settings import Settings
    setup_scope(store)
    insert_bar(store)
    monkeypatch.setattr(freshness,'datetime',SimpleNamespace(now=lambda zone:at()))
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    client=DataClient('http://127.0.0.1:8766','test')
    monkeypatch.setattr(client,'request',lambda path,payload=None: app.dispatch('POST' if payload is not None else 'GET',path,payload or {}))
    assert client.freshness()['rows']==client.health()['scheduled_freshness']['rows']
    assert client.freshness()['rows'][0]['freshness_state']=='stale'
    assert not store.query('SELECT * FROM jobs')
