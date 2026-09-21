from types import SimpleNamespace

import pytest

from pfor_qmt.client import CfquantError
from pfor_qmt.qmt_references import snapshot, call
from pfor_qmt.reliability import failure
from test_qmt_references import qmt_app, choice, run


class NativeSource:
    def __init__(self):self.dates=[]
    def get_main_contract(self,code):return 'a2611'
    def get_instrument_detail(self,code):return {'TradingDay':20260921}
    def get_trading_calendar(self,*args):
        raise CfquantError('requires QMT callable: get_trading_calendar','NotImplementedError')
    def get_trading_dates(self,code,start,end,count,period):
        self.dates.append((code,start,end,count,period))
        return ['20260914','20260918']


@pytest.mark.parametrize('continuous,raw,expected',[
    ('a00.DF','a2611','a2611.DF'),('cu00.SF','cu2610','cu2610.SF'),
    ('IF00.IF','IF2612','IF2612.IF'),('AP00.ZF','AP701','AP701.ZF'),
    ('a00.DF','a2611.DF','a2611.DF'),
    ('PL00.ZF','PL611','PL611.ZF'),('TL00.IF','TL2612','TL2612.IF'),
])
def test_native_main_contract_may_omit_market(continuous,raw,expected):
    requested=[]
    def details(code):
        requested.append(code)
        return {'TradingDay':20260921}
    source=SimpleNamespace(get_main_contract=lambda code:raw,get_instrument_detail=details)
    row=snapshot(source,continuous)[0]
    assert row['member_code']==expected and requested==[expected]
    assert row['source_fields']['value']==raw


@pytest.mark.parametrize('raw',['cu2610','a2611.SF','a00','a2611..DF','a2611.DCE'])
def test_response_suffix_completion_does_not_guess_product_or_replace_market(raw):
    source=SimpleNamespace(get_main_contract=lambda code:raw,get_instrument_detail=lambda code:{})
    with pytest.raises(ValueError):snapshot(source,'a00.DF')


def test_missing_terminal_calendar_does_not_say_update_model():
    def missing(*args):raise CfquantError('requires QMT callable: get_trading_calendar','NotImplementedError')
    with pytest.raises(NotImplementedError) as caught:
        call(SimpleNamespace(get_trading_calendar=missing),'get_trading_calendar','DF','20260914','20260918')
    detail=failure(caught.value)
    assert detail['code']=='QMT_TERMINAL_UNSUPPORTED'
    assert '更新' not in detail['message'] and '更新' not in detail['action']


def test_old_bridge_and_terminal_missing_are_different():
    def old(*args):raise CfquantError('Unsupported market action: xtdata.get_trading_calendar','ValueError')
    with pytest.raises(NotImplementedError) as caught:
        call(SimpleNamespace(get_trading_calendar=old),'get_trading_calendar','DF','20260914','20260918')
    detail=failure(caught.value)
    assert detail['code']=='QMT_BRIDGE_OUTDATED' and 'PFOR_MARKET' in detail['action']


def test_near_month_continuous_uses_explicit_product_evidence(qmt_app):
    from pfor_qmt.qmt_references import member
    app=qmt_app
    app.store.save_security('IFL00.IF','股指近月连续','future',{},subtype='continuous',metadata={'product':'IF'})
    app.worker.source=SimpleNamespace(get_main_contract=lambda code:'IF2610',get_instrument_detail=lambda code:{'ProductID':'IF','TradingDay':20260921})
    job=run(app,dict(choice(),code='IFL00.IF'))
    assert job['state']=='succeeded'
    saved=app.store.query('SELECT * FROM contract_mapping_snapshots',one=True)
    assert saved['member_code']=='IF2610.IF' and saved['source_fields']['product_evidence']==dict(request='IF',member='IF')
    verify=app.store.create_verification(job['id']);app.worker.source=object();app.worker.execute(verify)
    assert app.store.job(verify['id'])['state']=='succeeded'
    with pytest.raises(ValueError):member(None,'IFL00.IF','IF2610.IF')
    with pytest.raises(ValueError):member(None,'a00.DF','b2611.DF',dict(request='a',member='b'))


def test_fallback_only_records_observed_days_and_is_never_full_calendar(qmt_app):
    app=qmt_app;source=NativeSource();app.source=app.worker.source=source;store=app.store
    params=dict(choice('calendar'),start='2026-09-14',end='2026-09-20')
    job=run(app,params)
    assert job['state']=='partial' and job['result']['rows']==2 and job['error_code'] is None
    rows=app.dispatch('POST','/futures/records',params)['rows']
    assert len(rows)==2 and all(row['is_open'] and row['evidence']=='bar_observation' for row in rows)
    assert store.units_page(job['id'])['rows'][0]['issues'][0]['code']=='CALENDAR_OBSERVATIONS_ONLY'
    assert all(item[-2:]==(-1,'1d') for item in source.dates)
    proof=store.query('SELECT source_fields FROM trading_dates ORDER BY day')[0]['source_fields']
    assert proof['samples']==['a00.DF'] and proof['observations'][0]['code']=='a00.DF'
    check=store.create_verification(job['id']);app.worker.execute(check)
    assert store.job(check['id'])['state']=='partial'
    tested=app.dispatch('POST','/sources/qmt/references/test',dict(resource='calendar',start='2026-09-14',end='2026-09-20'))
    dce=next(row for row in tested['results'] if row['target']['exchange']=='DCE')
    assert dce['state']=='limited' and '未知' in dce['reason']
    assert run(app,choice())['state']=='succeeded'


def test_calendar_fallback_preserves_authoritative_dates_and_empty_is_not_closed(qmt_app):
    app=qmt_app;store=app.store;source=NativeSource();app.source=app.worker.source=source
    store.query("INSERT INTO trading_dates(source,market,day,is_open,evidence) VALUES('qmt','DF','2026-09-18',false,'calendar')")
    job=run(app,dict(choice('calendar'),start='2026-09-14'))
    assert job['state']=='failed' and job['error_code']=='CALENDAR_EVIDENCE_CONFLICT'
    assert store.query('SELECT count(*) AS n FROM trading_dates',one=True)['n']==1
    row=store.query("SELECT is_open,evidence FROM trading_dates WHERE day='2026-09-18'",one=True)
    assert row==dict(is_open=False,evidence='calendar')
    before=store.query('SELECT * FROM trading_dates ORDER BY day')
    source.get_trading_dates=lambda *args:[]
    empty=run(app,dict(choice('calendar'),start='2026-09-14'))
    assert empty['state']=='partial' and empty['result']['rows']==0
    assert store.query('SELECT * FROM trading_dates ORDER BY day')==before


@pytest.mark.parametrize('kind',['permission','old_bridge','minute','range','cross_market'])
def test_calendar_fallback_does_not_mask_errors_or_accept_bad_evidence(kind):
    from pfor_qmt.qmt_references import calendar
    source=NativeSource()
    if kind in ('permission','old_bridge'):
        def denied(*args):raise CfquantError('denied' if kind=='permission' else 'Unsupported market action','PermissionError' if kind=='permission' else 'ValueError')
        source.get_trading_calendar=denied
    else:source.get_trading_dates=lambda *args:['20260914100000' if kind=='minute' else '20260913' if kind=='range' else '20260914']
    with pytest.raises((ValueError,NotImplementedError)):
        calendar(source,'DF','2026-09-14','2026-09-20',['a00.SF' if kind=='cross_market' else 'a00.DF'])
    assert not source.dates
