from datetime import datetime

import pytest

from pfor_qmt.data import SHANGHAI, day
from pfor_qmt.exchange_holidays import (ANNUAL, EXCHANGES, ExchangeWorker, NoticeError, closure_issue,
                                       document, evidence, official_url, page, parse_notice, save_notice, scope, sync_exchange)
from pfor_qmt.freshness import freshness_page
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tasks import Worker
from test_qmt_period_readiness import make_range
from test_storage_tasks import Source

BODY = ('一、2026年9月24日晚上不进行夜盘交易。'
        '9月25日（星期五）至9月27日（星期日）休市。'
        '9月28日至9月30日照常开市交易，9月30日晚上不进行夜盘交易。'
        '10月1日至10月7日休市。10月8日08:55-09:00所有期货、期权合约进行集合竞价，当晚恢复夜盘交易。')


def notice(body=BODY, url=ANNUAL['SHFE'], published_at=None):
    return dict(title='关于2026年休市安排的公告', body=body, url=url, published_at=published_at)


def test_precise_dates_cross_year_weekend_and_night():
    body='元旦：1月1日至1月3日休市，1月5日起照常开市。1月4日为周末休市。2025年12月31日晚上不进行夜盘交易。'
    rows,state,_=parse_notice('2026年休市安排',body)
    assert state=='parsed' and len(rows)==4
    assert rows[-1]['start_day']==day('2025-12-31') and rows[-1]['kind']=='night_closed'
    assert rows[1]['start_day']==rows[1]['end_day']==day('2026-01-05')
    rows,state,_=parse_notice('2026年节日期间安排',BODY)
    assert state=='parsed' and [(r['kind'],r['start_day'],r['end_day']) for r in rows][-1]==('night_open',day('2026-10-08'),day('2026-10-08'))


@pytest.mark.parametrize('title,body',[
    ('休市安排','1月1日休市。'),
    ('2026年休市安排','另行通知休市日期。'),
    ('2026年休市安排','部分品种9月25日休市。'),
    ('2026年休市安排','9月25日可能休市。'),
    ('2026年休市安排','9月25日休市。其余时间暂停夜盘交易。'),
    ('2026年休市安排','12月30日至1月3日休市。'),
    ('2026年休市安排','9月25日休市。仅供参考。'),
])
def test_unknown_or_limited_arrangements_never_certify_full_exchange(title,body):
    assert parse_notice(title,body)[1]=='pending_verification'


def test_article_extraction_ignores_scripts_and_styles_and_reposted_issuer():
    value='<title>关于2026年中秋节安排的通知</title><div class="TRS_Editor"><style>ignore</style><p>'+BODY+'</p><p>2026年9月21日</p></div>'
    row=document(value,ANNUAL['SHFE'],'SHFE')
    assert row['published_at']==day('2026-09-21') and 'ignore' not in row['body']
    with pytest.raises(NoticeError,match='转发'):
        document(value.replace(BODY,'上能发〔2026〕107号'+BODY),ANNUAL['SHFE'],'SHFE')
    with pytest.raises(NoticeError):document('<title>Access Denied</title>',ANNUAL['SHFE'],'SHFE')


@pytest.mark.parametrize('url',['http://127.0.0.1/a','https://www.shfe.com.cn.evil.test/a','file:///etc/passwd','https://www.shfe.com.cn:8080/a','https://user@www.shfe.com.cn/a'])
def test_official_url_boundary(url):
    with pytest.raises(NoticeError):official_url(url,'SHFE')


def test_scope_keeps_sources_and_years_fixed():
    assert scope(dict(exchanges=['SHFE'],years=[2025,2026]))['chunks']==[
        dict(exchange='SHFE',year=2025,period='exchange_holidays'),dict(exchange='SHFE',year=2026,period='exchange_holidays')]
    for p in [dict(exchanges=[]),dict(years=[]),dict(years=['2026']),dict(years=[True]),dict(exchanges=['SF'])]:
        with pytest.raises(ValueError):scope(p)


def test_idempotent_notice_version_and_unparsed_revision_disables_old_evidence(store):
    original,_=save_notice(store,'SHFE',notice())
    repeated,_=save_notice(store,'SHFE',notice())
    assert original['id']==repeated['id'] and original['published_at'] is None
    part=dict(source='qmt',code='cu00.SF',period='1m',start='2026-09-25',end='2026-09-27')
    assert closure_issue(part,[],evidence(store,'SF',part['start'],part['end']))['quality_state']=='not_applicable'
    changed,_=save_notice(store,'SHFE',notice('9月25日休市。其余安排另行通知休市。'))
    assert changed['parse_state']=='pending_verification' and changed['id']!=original['id']
    assert closure_issue(part,[],evidence(store,'SF',part['start'],part['end'])) is None
    assert len(page(store,dict(exchanges=['SHFE'],years=[2026],versions=True))['rows'])>1
    assert store.query('SELECT is_current FROM exchange_notices WHERE id=%s',(original['id'],),one=True)['is_current'] is False
    assert store.query('SELECT count(*) AS n FROM exchange_holiday_events WHERE notice_id=%s',(original['id'],),one=True)['n']>0


def test_conflicts_unknown_dates_and_night_boundary_are_conservative(store):
    save_notice(store,'SHFE',notice('9月25日至9月27日休市。'))
    part=dict(source='qmt',code='cu00.SF',period='1m',start='2026-09-25',end='2026-09-27')
    rows=evidence(store,'SF',part['start'],part['end'])
    assert closure_issue(part,[],rows) is None
    assert closure_issue(dict(part,period='1d'),[],rows)
    assert closure_issue(dict(part,start='2026-09-26'),[],rows)
    assert closure_issue(dict(part,period='1d',end='2026-09-28'),[],rows) is None
    save_notice(store,'SHFE',notice('9月24日晚上不进行夜盘交易。9月25日至9月27日休市。',url='https://www.shfe.com.cn/extra.html'))
    rows=evidence(store,'SF',part['start'],part['end'])
    assert closure_issue(part,[],rows)
    assert closure_issue(part,[dict(day=day('2026-09-26'),is_open=True)],rows) is None
    save_notice(store,'SHFE',notice('9月26日照常开市。',url='https://www.shfe.com.cn/correction.html'))
    assert closure_issue(part,[],evidence(store,'SF',part['start'],part['end'])) is None


def test_dynamic_revision_does_not_fall_back_to_static_czce_rules(store):
    from pfor_qmt.quality_checks import exchange_holiday_issue
    part=dict(source='qmt',code='AP001.ZF',period='1d',start='2026-10-01',end='2026-10-07')
    url='https://www.czce.com.cn/annual.html'
    save_notice(store,'CZCE',notice('10月1日至10月7日休市。',url=url))
    assert exchange_holiday_issue(part,[],evidence(store,'ZF',part['start'],part['end']))
    save_notice(store,'CZCE',notice('9月25日至9月27日休市。',url=url))
    assert exchange_holiday_issue(part,[],evidence(store,'ZF',part['start'],part['end'])) is None


def test_risk_notice_without_schedule_does_not_block_explicit_closure(store):
    save_notice(store,'SHFE',notice())
    record,events=save_notice(store,'SHFE',notice('2026年中秋节休市期间，我所各品种期货合约涨跌停板幅度和交易保证金标准保持不变。',url='https://www.shfe.com.cn/risk.html'))
    assert record['parse_state']=='parsed' and not events
    part=dict(source='qmt',code='cu00.SF',period='1m',start='2026-09-25',end='2026-09-27')
    assert closure_issue(part,[],evidence(store,'SF',part['start'],part['end']))


def test_previous_day_conflict_cannot_certify_absent_night_session(store):
    save_notice(store,'SHFE',notice('9月25日至9月27日休市。'))
    part=dict(source='qmt',code='cu00.SF',period='1m',start='2026-09-26',end='2026-09-27')
    rows=evidence(store,'SF',part['start'],part['end'])
    assert closure_issue(part,[],rows)
    assert closure_issue(part,[dict(day=day('2026-09-25'),is_open=True)],rows) is None
    save_notice(store,'SHFE',notice('9月25日照常开市。',url='https://www.shfe.com.cn/previous.html'))
    assert closure_issue(part,[],evidence(store,'SF',part['start'],part['end'])) is None


def test_reposted_foreign_notice_is_ignored_without_mixing_exchange(store):
    def reader(url,exchange,check):
        if url==EXCHANGES['SHFE'][3]:return '<a href="/202609/ine.html">关于2026年节期间安排</a>'
        if url.endswith('ine.html'):return '<title>关于2026年中秋节安排</title><div class="TRS_Editor">上能发〔2026〕107号'+BODY+'</div>'
        return '<title>2026年休市安排</title><div class="TRS_Editor">'+BODY+'</div>'
    rows,issues,state=sync_exchange(store,'SHFE',2026,reader=reader)
    assert state=='succeeded' and not issues and rows>0
    assert not store.query("SELECT * FROM exchange_notices WHERE exchange='INE' OR url LIKE '%%ine.html'")


def test_sync_failure_preserves_cache_and_latest_status(store):
    save_notice(store,'SHFE',notice())
    def unavailable(*args):raise NoticeError('官网HTTP 412，缓存已保留')
    rows,issues,state=sync_exchange(store,'SHFE',2026,reader=unavailable)
    assert state=='failed' and issues and rows==0 and not issues[0]['retryable']
    assert len(evidence(store,'SF','2026-09-25','2026-09-27'))>0
    assert page(store,dict(exchanges=['SHFE'],years=[2026]))['status'][0]['state']=='failed'
    assert not store.query('SELECT * FROM trading_dates')


def test_worker_partial_retry_cancel_and_qmt_queue_are_independent(store,tmp_path,monkeypatch):
    def reader(url,exchange,check):
        check()
        if exchange=='DCE':raise NoticeError('官网HTTP 412')
        if url==EXCHANGES[exchange][3]:return '<a href="/notice.html">关于2026年休市安排的公告</a>'
        return '<title>关于2026年休市安排的公告</title><div class="TRS_Editor">'+BODY+'</div>'
    monkeypatch.setattr('pfor_qmt.exchange_holidays.fetch',reader)
    qmt=make_range(store,'cu00.SF','1m','2026-09-25','2026-09-27')
    worker=ExchangeWorker(store,tmp_path)
    job=store.create_job('download',scope(dict(exchanges=['SHFE','DCE'],years=[2026])))
    worker.execute(job)
    saved=store.job(job['id'])
    assert saved['state']=='partial' and saved['checkpoint']==2 and saved['result']['rows']>0
    assert store.job(qmt['id'])['state']=='queued'
    retry=store.retry_job(job['id'])
    assert retry['payload']['chunks']==[dict(exchange='DCE',year=2026,period='exchange_holidays')]
    assert retry['parent_id']==job['id']
    source=Source();source.empty=True;source.get_trading_dates=lambda *args:[]
    collector=Worker(store,tmp_path,source=source);collector.stop.wait=lambda _:False;collector.execute(qmt)
    assert store.job(qmt['id'])['state']=='succeeded'
    check=store.create_verification(qmt['id']);collector.source=object();collector.execute(check)
    assert store.job(check['id'])['state']=='succeeded'
    canceled=store.create_job('download',scope(dict(exchanges=['SHFE'],years=[2026])))
    store.update_job(canceled['id'],cancel_requested=True);worker.execute(canceled)
    assert store.job(canceled['id'])['state']=='cancelled'
    verify_job=store.create_verification(job['id']);collector.execute(verify_job)
    assert store.job(verify_job['id'])['state']=='partial'
    assert store.jobs_page(dict(sources=['exchange']))['total']==4


def test_explicit_maintenance_scope_dedup_and_freshness(store,tmp_path,monkeypatch):
    app=Application(Settings(config_path=tmp_path/'config.toml'),store)
    p=app.dispatch('POST','/exchange-holidays/maintenance',dict(exchanges=['SHFE'],years=[2026],name='公告维护',enabled=True,schedule_time='08:00'))
    now=datetime(2026,9,30,9,tzinfo=SHANGHAI)
    app.exchange_worker.schedule(now);app.exchange_worker.schedule(now)
    jobs=store.jobs_page(dict(sources=['exchange']))['rows']
    assert len(jobs)==1 and jobs[0]['payload']['years']==[2026] and jobs[0]['payload']['exchanges']==['SHFE']
    assert freshness_page(store,dict(sources=['exchange']),now)['rows'][0]['freshness_state']=='pending_verification'
    changed=app.dispatch('POST','/exchange-holidays/maintenance/'+str(p['id']),dict(revision=p['revision'],enabled=False))
    assert changed['enabled'] is False
    preview=app.dispatch('POST','/exchange-holidays/maintenance/'+str(p['id'])+'/preview',{})
    assert preview['ranges'][0]['years']==[2026]
    with pytest.raises(ValueError,match='活动任务'):
        app.dispatch('POST','/exchange-holidays/maintenance/'+str(p['id'])+'/delete',dict(revision=changed['revision']))
    app.dispatch('POST','/jobs/'+str(jobs[0]['id'])+'/cancel',{})
    app.dispatch('POST','/exchange-holidays/maintenance/'+str(p['id'])+'/delete',dict(revision=changed['revision']))
    deleted=store.query('SELECT * FROM maintenance_plans WHERE id=%s',(p['id'],),one=True)
    restored=app.dispatch('POST','/exchange-holidays/maintenance/'+str(p['id'])+'/restore',dict(revision=deleted['revision']))
    assert restored['enabled'] is False
    with pytest.raises(ValueError):app.dispatch('GET','/history',dict(source='exchange',code='cu00.SF'))
