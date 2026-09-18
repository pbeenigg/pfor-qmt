from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from pfor_qmt.data import day, timestamp
from pfor_qmt.quality_checks import repair_ranges
from pfor_qmt.reliability import issue
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tasks import Worker
from pfor_qmt.tushare import TushareSource
from test_tushare import seed, account, daily, fake_request, download_job


def missing_verification(store,tmp_path,monkeypatch):
    seed(store)
    monkeypatch.setattr(TushareSource,'request',fake_request)
    job=download_job(store,start='2026-09-14',end='2026-09-16')
    Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(job)
    verification=store.create_verification(job['id'])
    Worker(store,tmp_path,source=object()).execute(verification)
    return store.job(job['id']),store.job(verification['id'])


def repaired_response(self,api,params,fields=''):
    if api=='fut_daily':
        first,last=timestamp(params['start_date']).date(),timestamp(params['end_date']).date()
        return [dict(daily(params['ts_code']),trade_date=(first+timedelta(days=i)).strftime('%Y%m%d')) for i in range((last-first).days+1)]
    return fake_request(self,api,params,fields)


def test_repair_ranges_only_explicit_dates_and_no_uncertain_minutes():
    parts=[dict(code='CU2610.SHF',period='1d',start='2026-09-01',end='2026-09-18'),dict(code='CU2610.SHF',period='1m',start='2026-09-01',end='2026-09-18')]
    job={'payload':{'chunks':parts}}
    findings=[issue('BARS_MISSING','missing','missing','retry',True,day=date) for date in ['2026-08-31','2026-09-14','2026-09-15','2026-09-17','2026-09-19']]
    units=[dict(unit_index=0,issues=findings),dict(unit_index=1,issues=findings)]
    ranges=repair_ranges(job,units,now=timestamp('20260918'))
    assert [(r['request']['start'],r['request']['end']) for r in ranges]==[('2026-09-14','2026-09-15'),('2026-09-17','2026-09-17')]
    for indices in ([True],[-1],[2],[],['0']):
        with pytest.raises(ValueError):repair_ranges(job,units,indices)
    assert repair_ranges(job,[dict(unit_index=0,issues=[issue('SESSION_UNVERIFIED','pending_verification','unknown','check')])])==[]


def test_aggregate_repairs_stay_in_original_range_and_wait_for_period_end():
    job={'payload':{'chunks':[dict(code='CU2610.SHF',period='1w',start='2026-09-07',end='2026-09-09')]}}
    units=[dict(unit_index=0,issues=[issue('PERIOD_STALE','stale','old','retry',True,day='2026-09-11')])]
    assert repair_ranges(job,units,now=timestamp('20260911'))==[]
    part=repair_ranges(job,units,now=timestamp('20260912'))[0]['request']
    assert part['start']=='2026-09-07' and part['end']=='2026-09-09'


@pytest.mark.postgres
def test_verify_repair_reverify_and_navigation_preserve_original_evidence(store,tmp_path,monkeypatch):
    original,verification=missing_verification(store,tmp_path,monkeypatch)
    before=store.query("SELECT * FROM bars WHERE time::date='2026-09-14'")
    preview=store.verification_repair(verification['id'])
    assert preview['total']==1 and preview['rows'][0]['request']['start']=='2026-09-15'
    assert preview['rows'][0]['request']['end']=='2026-09-16'
    assert len(store.query('SELECT * FROM jobs'))==2
    created=store.verification_repair(verification['id'],preview['unit_indices'],preview['preview_key'],True)
    assert store.verification_repair(verification['id'],preview['unit_indices'],preview['preview_key'],True)['id']==created['id']
    assert created['kind']=='download' and created['parent_id'] is None
    assert created['payload']['account_id']=='main' and created['payload']['endpoint']==account()['endpoint']
    assert 'maintenance_id' not in created['payload'] and 'dataset_id' not in created['payload']
    assert created['payload']['members']==['CU2610.SHF']
    monkeypatch.setattr(TushareSource,'request',repaired_response)
    Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(created)
    assert store.job(created['id'])['state']=='succeeded'
    assert len(store.history('CU2610.SHF',source='tushare')['rows'])==3
    assert store.query("SELECT * FROM bars WHERE time::date='2026-09-14'")==before
    assert store.job(original['id'])==original and store.job(verification['id'])==verification
    assert store.verification_repair(verification['id'])['total']==0
    with pytest.raises(ValueError,match='没有明确'):store.verification_repair(verification['id'],preview['unit_indices'],preview['preview_key'],True)
    recheck=store.create_verification(verification['id'])
    Worker(store,tmp_path,source=object()).execute(recheck)
    assert store.job(recheck['id'])['state']=='succeeded'
    links=store.job_links(verification['id'],limit=2)
    following=store.job_links(verification['id'],limit=2,offset=links['next_offset'])
    assert {row['id'] for row in links['rows']+following['rows']}=={original['id'],created['id'],recheck['id']}
    assert store.events_page({'code':'REPAIR_CREATED'})['rows']
    child_check=store.create_verification(created['id'])
    assert child_check['payload']['verification_of']==str(created['id']) and 'repair_of' not in child_check['payload']


@pytest.mark.postgres
def test_preview_guard_concurrency_and_retry_child_completion(store,tmp_path,monkeypatch):
    _,verification=missing_verification(store,tmp_path,monkeypatch)
    preview=store.verification_repair(verification['id'])
    with pytest.raises(ValueError,match='重新预览'):store.verification_repair(verification['id'],[0],'not-the-preview',True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(lambda _:store.verification_repair(verification['id'],[0],preview['preview_key'],True),range(2)))
    assert jobs[0]['id']==jobs[1]['id']
    store.update_job(jobs[0]['id'],state='failed')
    retry=store.retry_job(jobs[0]['id'])
    monkeypatch.setattr(TushareSource,'request',repaired_response)
    Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(retry)
    assert store.verification_repair(verification['id'])['total']==0


@pytest.mark.postgres
def test_changed_findings_require_new_confirmation_and_unit_selection(store,tmp_path,monkeypatch):
    _,verification=missing_verification(store,tmp_path,monkeypatch)
    before=store.verification_repair(verification['id'])
    part=verification['payload']['chunks'][0]
    store.write_unit(verification['id'],0,part,'succeeded',[issue('BARS_MISSING','missing','missing','retry',True,day='2026-09-16')])
    with pytest.raises(ValueError,match='重新预览'):store.verification_repair(verification['id'],before['unit_indices'],before['preview_key'],True)
    after=store.verification_repair(verification['id'],[0])
    assert after['rows'][0]['request']['start']=='2026-09-16'
    assert len(store.query('SELECT * FROM jobs'))==2


@pytest.mark.postgres
def test_api_sdk_fixed_account_and_preview_does_not_need_token(store,tmp_path,monkeypatch):
    from pfor_qmt.sdk import DataClient
    _,verification=missing_verification(store,tmp_path,monkeypatch)
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    client=DataClient()
    monkeypatch.setattr(client,'request',lambda path,payload=None,**query:app.dispatch('POST' if payload is not None else 'GET',path,payload if payload is not None else query))
    preview=client.preview_repair(str(verification['id']))
    assert preview['total']==1
    with pytest.raises(ValueError):client.repair(str(verification['id']),preview['preview_key'],preview['unit_indices'])
    app.settings.accounts=[account(endpoint='https://other.example/api')]
    with pytest.raises(ValueError,match='固定端点'):client.repair(str(verification['id']),preview['preview_key'],preview['unit_indices'])
    app.settings.accounts=[account(),account(id='other')];app.settings.default_account_id='other'
    created=client.repair(str(verification['id']),preview['preview_key'],preview['unit_indices'])
    assert created['payload']['account_id']=='main'
    assert created['id'] in {row['id'] for row in client.job_links(str(verification['id']))['rows']}


@pytest.mark.postgres
def test_calendar_missing_days_reuse_report_pipeline(store,tmp_path,monkeypatch):
    from pfor_qmt.futures import request_chunks
    payload=dict(source='tushare',account_id='main',endpoint=account()['endpoint'],resource='calendar',exchange='DCE',code='',symbol='',start='2026-09-12',end='2026-09-14')
    payload['chunks']=request_chunks(payload)
    original=store.create_job('download',payload);store.update_job(original['id'],state='failed')
    verification=store.create_verification(original['id']);Worker(store,tmp_path,source=object()).execute(verification)
    preview=store.verification_repair(verification['id'])
    created=store.verification_repair(verification['id'],None,preview['preview_key'],True)
    monkeypatch.setattr(TushareSource,'request',fake_request)
    Worker(store,tmp_path,provider='tushare',account_resolver=lambda _:account()).execute(created)
    assert store.job(created['id'])['state']=='succeeded'
    assert [r['is_open'] for r in store.query('SELECT is_open FROM trading_dates ORDER BY day')]==[False,False,True]


@pytest.mark.postgres
def test_report_selection_and_single_unit_are_preserved(store,tmp_path):
    from pfor_qmt.futures import request_chunks
    payload=dict(source='tushare',account_id='main',endpoint=account()['endpoint'],resource='calendar',start='2026-09-14',end='2026-09-14',
                 selections=[dict(resource='calendar',exchange='DCE',code='',symbol=''),dict(resource='calendar',exchange='SHFE',code='',symbol='')])
    payload['chunks']=request_chunks(payload)
    original=store.create_job('download',payload);store.update_job(original['id'],state='failed')
    check=store.create_verification(original['id']);Worker(store,tmp_path,source=object()).execute(check)
    preview=store.verification_repair(check['id'],[1])
    assert preview['total']==1 and preview['rows'][0]['request']['selection']['exchange']=='SHFE'
    result=store.verification_repair(check['id'],[1],preview['preview_key'],True)
    assert len(result['payload']['selections'])==1 and result['payload']['selections'][0]['exchange']=='SHFE'


@pytest.mark.postgres
def test_large_preview_reports_truncation_without_cutting_created_scope(store):
    from pfor_qmt.data import chunks
    payload=dict(source='qmt',members=['000300.SH'],periods=['1d'],start='2025-01-01',end='2025-12-31')
    payload['chunks']=chunks(payload)
    job=store.create_job('verify',payload);store.update_job(job['id'],state='partial')
    gaps=[issue('BARS_MISSING','missing','missing','retry',True,day=(day('2025-01-01')+timedelta(days=i*2)).isoformat()) for i in range(110)]
    store.write_unit(job['id'],0,payload['chunks'][0],'succeeded',gaps)
    preview=store.verification_repair(job['id'])
    assert preview['total']==110 and len(preview['rows'])==100 and preview['truncated']
    created=store.verification_repair(job['id'],preview['unit_indices'],preview['preview_key'],True)
    assert len(created['payload']['chunks'])==110
