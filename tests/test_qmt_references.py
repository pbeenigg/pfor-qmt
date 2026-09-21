import csv
from datetime import datetime, timedelta
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from pfor_qmt.data import day, SHANGHAI
from pfor_qmt.futures import request_chunks
from pfor_qmt.market_bridge import MarketBridge
from pfor_qmt.policy import validate
from pfor_qmt.qmt_references import calendar, snapshot, history, probe
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.maintenance import schedule_scope


class Source:
    def get_main_contract(self,code): return 'a2611.DF'
    def get_instrument_detail(self,code): return {'TradingDay':20260921}
    def get_trading_calendar(self,market,start,end): return ['20260918']
    def download_main_contract_history(self,*args): return None
    def get_main_contract_history(self,code,*args): return {code:[{'time':'20260918','main_contract':'a2611.DF'}]}


@pytest.fixture
def qmt_app(store,tmp_path):
    store.save_security('a00.DF','豆一连续','future',{},subtype='continuous',metadata={'product':'a'})
    store.save_security('a2611.DF','豆一2611','future',{},subtype='contract',metadata={'product':'a'})
    return Application(Settings(config_path=tmp_path/'config.toml'),store,Source())


def choice(resource='mapping',mode='current'):
    return dict(source='qmt',resource=resource,exchange='DCE',code='a00.DF',mapping_mode=mode,start='2026-09-18',end='2026-09-20')


def run(app,payload):
    job=app.dispatch('POST','/futures/sync',payload)
    app.worker.execute(app.store.job(job['id']))
    return app.store.job(job['id'])


def test_native_calls_are_direct_market_only_and_old_bridge_explicit():
    calls=[]
    bridge=MarketBridge(SimpleNamespace(get_main_contract=lambda code:calls.append(code) or 'a2611.DF'),tx=object())
    assert bridge.dispatch('xtdata.get_main_contract',{'stock_code':'a00.DF'})=='a2611.DF'
    assert calls==['a00.DF']
    with pytest.raises(NotImplementedError): bridge.dispatch('xtdata.get_trading_calendar',dict(market='DF',start_time='20260918',end_time='20260920'))
    with pytest.raises(ValueError): validate('xtdata.get_main_contract',{'stock_code':'a2611.DF'})
    with pytest.raises(ValueError): validate('xtdata.get_market_data_ex',{'period':'historymaincontract'})
    with pytest.raises(ValueError): validate('xttrader.order_stock',{})
    assert probe(object(),{'resource':'mapping'})['results'][0]['state']=='bridge_outdated'


def test_snapshots_never_guess_trade_day_or_month():
    source=Source();source.get_instrument_detail=lambda code:{}
    assert snapshot(source,'a00.DF')[0]['trading_day'] is None
    source.get_main_contract=lambda code:'cu2610.SF'
    with pytest.raises(ValueError,match='跨市场'):snapshot(source,'a00.DF')
    source.get_main_contract=lambda code:'b2611.DF'
    with pytest.raises(ValueError,match='不同品种'):snapshot(source,'a00.DF')
    source.get_main_contract=lambda code:''
    assert probe(source,{'resource':'mapping'})['results'][0]['state']=='empty'


def test_calendar_evidence_empty_future_and_history_conflict():
    rows=calendar(Source(),'DF','2026-09-18','2026-09-20')
    assert [row['is_open'] for row in rows]==[True,False,False]
    assert all(row['evidence']=='calendar' for row in rows)
    source=Source();source.get_trading_calendar=lambda *args:[]
    assert calendar(source,'DF','2026-09-18','2026-09-20')==[]
    future=datetime.now(SHANGHAI).date()+timedelta(days=365)
    source.get_trading_calendar=lambda *args:[future.strftime('%Y%m%d')]
    assert calendar(source,'DF',future,future+timedelta(days=2))[0]['evidence']=='calendar_open'
    source.get_main_contract_history=lambda code,*args:{code:[{'time':'20260918','main_contract':'a2611.DF'},{'time':'20260918','main_contract':'a2701.DF'}]}
    with pytest.raises(ValueError,match='冲突'):history(source,'a00.DF','2026-09-18','2026-09-20')


def test_snapshot_roundtrip_idempotence_history_separation_and_exports(qmt_app):
    app=qmt_app;store=app.store;p=choice()
    job=run(app,p);assert job['state']=='succeeded',job
    assert store.query('SELECT count(*) AS n FROM contract_mappings',one=True)['n']==0
    row=app.dispatch('POST','/futures/records',p)['rows'][0]
    assert row['trading_day']==day('2026-09-21') and row['member_code']=='a2611.DF'
    store.update_job(job['id'],checkpoint=0);app.worker.execute(store.job(job['id']))
    assert store.query('SELECT count(*) AS n FROM contract_mapping_snapshots',one=True)['n']==1
    check=store.create_verification(job['id']);app.worker.execute(check)
    assert store.job(check['id'])['state']=='succeeded'
    repeated=store.create_verification(check['id']);app.worker.execute(repeated)
    assert store.job(repeated['id'])['state']=='succeeded'
    app.source=object();app.worker.source=object()
    result=app.dispatch('POST','/futures/summary',p)
    assert result['total']==1 and result['instrument_names']['a2611.DF']=='豆一2611'
    assert app.dispatch('POST','/futures/records',dict(p,mapping_mode='history'))['rows']==[]
    for fmt in ('csv','parquet'):
        exported=app.dispatch('POST','/futures/export',dict(p,format=fmt));app.worker.execute(store.job(exported['id']))
        saved=store.job(exported['id']);assert saved['state']=='succeeded',saved
        path=app.settings.runtime/'exports'/saved['result']['file']
        if fmt=='csv':
            assert path.read_bytes().startswith(b'\xef\xbb\xbf')
            with path.open(encoding='utf-8-sig') as stream: data=list(csv.DictReader(stream))
        else: data=pq.read_table(path).to_pylist()
        assert data[0]['member_code']==row['member_code'] and data[0]['trading_day']=='2026-09-21'


def test_calendar_and_history_share_existing_tables_and_source_isolation(qmt_app):
    app=qmt_app;store=app.store
    assert run(app,choice('calendar'))['state']=='succeeded'
    assert run(app,choice(mode='history'))['state']=='succeeded'
    assert run(app,choice(mode='history'))['result']['rows']==1
    assert store.query('SELECT count(*) AS n FROM contract_mappings',one=True)['n']==1
    assert app.dispatch('POST','/futures/records',choice('calendar'))['rows'][0]['evidence']=='calendar'
    assert app.dispatch('POST','/futures/records',dict(source='tushare',resource='mapping',code='A.DCE',start='2026-09-18',end='2026-09-20'))['rows']==[]
    app.worker.source=object()
    assert app.worker.calendar('a2611.DF','20260918','20260920')==['20260918']


def test_missing_capability_empty_response_cancel_and_natural_schedule(qmt_app):
    app=qmt_app;store=app.store
    app.worker.source=object();job=run(app,choice());assert job['state']=='blocked' and job['error_code']=='QMT_BRIDGE_OUTDATED'
    app.worker.source=Source();app.worker.source.get_main_contract=lambda code:''
    job=run(app,choice());assert job['state']=='partial' and job['result']['rows']==0
    pending=app.dispatch('POST','/futures/sync',choice());store.update_job(pending['id'],cancel_requested=True);app.worker.execute(store.job(pending['id']))
    assert store.job(pending['id'])['state']=='cancelled'
    plan=app.dispatch('POST','/console/maintenance',dict(name='主力快照',payload=choice(),kind='download'))
    assert not plan['enabled']
    app.dispatch('POST','/console/maintenance/'+str(plan['id']),dict(revision=plan['revision'],enabled=True))
    plan=store.query('SELECT * FROM maintenance_plans WHERE id=%s',(plan['id'],),one=True)
    now=datetime(2026,9,20,18,tzinfo=SHANGHAI)
    schedule_scope(app.worker,plan,'maintenance',now);schedule_scope(app.worker,plan,'maintenance',now)
    queued=store.query("SELECT * FROM jobs WHERE payload->>'maintenance_id'=%s",(str(plan['id']),))
    assert len(queued)==1 and len(queued[0]['payload']['chunks'])==1
    assert not store.query('SELECT * FROM trading_dates')


def test_legacy_dates_not_promoted_and_confirmed_holiday_not_blocked(store,tmp_path):
    from pfor_qmt.tasks import Worker
    from pfor_qmt.quality_checks import stored_issues
    from test_storage_tasks import Source as BarsSource
    from test_qmt_period_readiness import make_range
    store.query("INSERT INTO trading_dates(source,market,day,is_open) VALUES('qmt','DF','2026-09-18',true)")
    records=store.query('SELECT * FROM trading_dates')
    assert records[0]['evidence']=='legacy'
    issues=stored_issues([{'trading_day':day('2026-09-18')}],dict(source='qmt',code='a2611.DF',period='1d',start='2026-09-18',end='2026-09-18'),records)
    assert any(item['code']=='CALENDAR_INCOMPLETE' for item in issues)
    store.query("INSERT INTO trading_dates(source,market,day,is_open,evidence) VALUES('qmt','DF','2026-05-01',false,'calendar')")
    source=BarsSource();source.empty=True
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda seconds:False
    job=make_range(store,'a2611.DF','1d','2026-05-01','2026-05-01');worker.execute(job)
    assert store.job(job['id'])['state']=='succeeded' and store.job(job['id'])['result']['rows']==0
    assert store.units_page(job['id'])['rows'][0]['issues'][0]['code']=='NO_OPEN_DAY'


def test_sdk_sources_are_consistent_and_permissions_not_guessed(monkeypatch):
    from pfor_qmt.sdk import DataClient
    from pfor_qmt.tushare import SourceError
    client=DataClient();calls=[]
    monkeypatch.setattr(client,'request',lambda path,payload=None,**query:calls.append(payload or query))
    client.sync_futures('mapping',source='qmt',code='a00.DF')
    client.futures_records('mapping','2026-09-18','2026-09-20',source='qmt',code='a00.DF')
    client.export_futures('mapping','2026-09-18','2026-09-20',source='qmt',code='a00.DF')
    client.query_futures([choice()],'2026-09-18','2026-09-20',source='qmt')
    client.sync_futures_batch([choice()],'2026-09-18','2026-09-20',source='qmt')
    client.export_futures_batch([choice()],'2026-09-18','2026-09-20',source='qmt')
    assert all(row['source']=='qmt' for row in calls)
    class Denied(Source):
        def get_main_contract(self,code):raise SourceError('明确权限拒绝','permission')
    assert probe(Denied(),{'resource':'mapping'})['results'][0]['state']=='permission'
    class Truncated(Source):
        def get_main_contract_history(self,code,*args):return {code:[{'time':'20260918','main_contract':'a2611.DF'}]*10000}
    with pytest.raises(SourceError,match='上限'):history(Truncated(),'a00.DF','2026-09-18','2026-09-20')


def test_current_snapshot_cancel_during_read_and_restart_resume(qmt_app):
    app=qmt_app;store=app.store;payload=choice()
    job=app.dispatch('POST','/futures/sync',payload)
    def cancel(code):
        store.update_job(job['id'],cancel_requested=True)
        return 'a2611.DF'
    app.worker.source.get_main_contract=cancel
    app.worker.execute(store.job(job['id']))
    assert store.job(job['id'])['state']=='cancelled' and not store.query('SELECT * FROM contract_mapping_snapshots')
    app.worker.source=Source()
    job=app.dispatch('POST','/futures/sync',payload)
    app.worker.stop.set();app.worker.execute(store.job(job['id']))
    assert store.job(job['id'])['state']=='queued' and store.job(job['id'])['checkpoint']==0
    app.worker.stop.clear();app.worker.execute(store.job(job['id']))
    assert store.job(job['id'])['state']=='succeeded'
    assert store.query('SELECT count(*) AS n FROM contract_mapping_snapshots',one=True)['n']==1
