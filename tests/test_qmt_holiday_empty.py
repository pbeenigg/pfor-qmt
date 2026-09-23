import pytest

from pfor_qmt.data import day, timestamp
from pfor_qmt.quality_checks import stored_issues
from pfor_qmt.tasks import Worker
from test_qmt_period_readiness import make_range
from test_storage_tasks import Source


@pytest.mark.parametrize('period',['1d','1m','5m'])
def test_czce_spring_festival_empty_is_not_applicable_and_can_verify(store,tmp_path,period):
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda _:False
    job=make_range(store,'AP001.ZF',period,'2026-02-16','2026-02-22');worker.execute(job)
    saved=store.job(job['id']);unit=store.units_page(job['id'])['rows'][0]
    assert saved['state']=='succeeded' and saved['checkpoint']==1 and saved['result']['rows']==0
    assert unit['quality_state']=='not_applicable' and unit['issues'][0]['code']=='EXCHANGE_HOLIDAY_CLOSED'
    assert unit['issues'][0]['notice']=='郑商函〔2025〕939号'
    assert unit['issues'][0]['holiday']=='春节'
    assert not store.query('SELECT * FROM bars') and not store.query('SELECT * FROM trading_dates')
    check=store.create_verification(job['id']);worker.source=object();worker.execute(check)
    assert store.job(check['id'])['state']=='succeeded'
    assert store.units_page(check['id'])['rows'][0]['quality_state']=='not_applicable'


@pytest.mark.parametrize('code,start,end',[
    ('AP001.ZF','2026-02-13','2026-02-16'),
    ('AP001.ZF','2026-02-22','2026-02-24'),
    ('a00.DF','2026-02-16','2026-02-22'),
    ('AP001.ZF','2025-02-16','2025-02-22'),
])
def test_holiday_rule_does_not_infer_other_markets_years_or_open_boundaries(store,tmp_path,code,start,end):
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda _:False
    job=make_range(store,code,'1m',start,end);worker.execute(job)
    saved=store.job(job['id'])
    assert saved['state']=='blocked' and saved['checkpoint']==0
    assert '原因尚未确认' in saved['error']


def test_czce_holiday_does_not_hide_saved_open_evidence_or_returned_bars(store,tmp_path):
    store.query("INSERT INTO trading_dates(source,market,day,is_open,evidence) VALUES('qmt','ZF','2026-02-16',true,'bar_observation')")
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda _:False
    job=make_range(store,'AP001.ZF','1m','2026-02-16','2026-02-22');worker.execute(job)
    assert store.job(job['id'])['state']=='blocked'
    part=dict(source='qmt',code='AP001.ZF',period='1m',start='2026-02-16',end='2026-02-22')
    opened=[{'day':day('2026-02-16'),'is_open':True}]
    assert all(row['quality_state']!='not_applicable' for row in stored_issues([],part,opened))
    rows=[dict(time=timestamp('2026-02-16T09:01:00'),trading_day=None)]
    assert all(row['quality_state']!='not_applicable' for row in stored_issues(rows,part,[]))
    assert all(row['quality_state']!='not_applicable' for row in stored_issues([],dict(part,source='tushare',code='AP.ZCE'),[]))


def test_source_exception_during_holiday_remains_failure(store,tmp_path):
    source=Source();source.fail=ConnectionError('unavailable')
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda _:False
    job=make_range(store,'AP001.ZF','1m','2026-02-16','2026-02-22');worker.execute(job)
    assert store.job(job['id'])['state']=='failed' and store.job(job['id'])['checkpoint']==0
    assert store.job(job['id'])['error_code']=='NETWORK_ERROR'


def test_preexisting_holiday_bar_is_not_hidden_by_empty_terminal_response(store,tmp_path):
    from pfor_qmt.data import normalize_bars
    p=dict(source='qmt',code='AP001.ZF',period='1m',start='2026-02-16',end='2026-02-22')
    original=make_range(store,p['code'],p['period'],p['start'],p['end'])
    rows=normalize_bars([dict(time='20260216090100',open=1,high=2,low=1,close=2)],p['code'],p['period'],day(p['start']),day(p['end']))
    store.write_chunk(original['id'],p,rows,[],1)
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    worker=Worker(store,tmp_path,source=source);worker.stop.wait=lambda _:False
    job=make_range(store,p['code'],p['period'],p['start'],p['end']);worker.execute(job)
    assert store.job(job['id'])['state']=='blocked' and store.job(job['id'])['checkpoint']==0
    assert store.history(p['code'],p['period'],p['start'],p['end'])['rows']


def test_holiday_chunk_does_not_stop_next_open_day(store,tmp_path):
    class HolidaySource(Source):
        def get_local_data(self,**params):
            code=params['stock_list'][0]
            return {code:[dict(time='20260224090100',open=1,high=2,low=1,close=2,volume=1)]}
        def get_trading_dates(self,code,start,end,*args):
            return ['20260224'] if start<='20260224'<=end else []
    worker=Worker(store,tmp_path,source=HolidaySource());worker.stop.wait=lambda _:False
    payload=dict(source='qmt',members=['AP001.ZF'],periods=['1m'],chunks=[
        dict(code='AP001.ZF',period='1m',start='2026-02-16',end='2026-02-22'),
        dict(code='AP001.ZF',period='1m',start='2026-02-24',end='2026-02-24')])
    job=store.create_job('download',payload);worker.execute(job)
    saved=store.job(job['id']);units=store.units_page(job['id'])['rows']
    assert saved['checkpoint']==2 and saved['result']['rows']==1 and saved['error_code'] is None
    assert units[0]['quality_state']=='not_applicable' and units[1]['row_count']==1
