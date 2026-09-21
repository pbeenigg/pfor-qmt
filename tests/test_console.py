from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pfor_qmt import analytics
from pfor_qmt.data import SHANGHAI
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings


@pytest.fixture
def console_app(store,tmp_path):
    store.save_security('000001.SZ','平安银行','stock',{})
    return Application(Settings(tmp_path,config_path=tmp_path/'config.toml'),store=store)


def test_full_summary_chart_windows_precision_and_snapshot(console_app):
    app=console_app;store=app.store
    start=datetime(2026,1,1,tzinfo=SHANGHAI)
    price=Decimal('12.1234567890123456789')
    with store.connect() as conn:
        conn.execute("INSERT INTO bars(instrument_id,code,source,period,time,open,high,low,close,volume,amount,normalization_version) SELECT instrument_id,code,'qmt','1m',%s::timestamptz + n*interval '1 minute',%s,%s,%s,%s,1,0.01,'qmt-raw-v1' FROM securities CROSS JOIN generate_series(0,5100) n WHERE source='qmt' AND code='000001.SZ'",(start,price,price,price,price))
    p=dict(source='qmt',members=['000001.SZ'],periods=['1m'],start='2026-01-01',end='2026-01-05')
    summary=analytics.history_summary(store,p)
    assert summary['total']==5101 and summary['groups'][0]['high']==price
    assert summary['groups'][0]['amount']==Decimal('51.01')
    assert summary['groups'][0]['latest_open_interest'] is None
    chart=analytics.history_chart(store,p)
    assert len(chart['rows'])==1000 and chart['has_previous'] and not chart['has_next']
    assert chart['rows'][0]['time']==start+timedelta(minutes=4101)
    older=analytics.history_chart(store,dict(p,before=chart['actual_start'].isoformat(),snapshot=chart['snapshot']))
    assert older['actual_end']<chart['actual_start'] and older['has_next']
    page=analytics.history_page(store,dict(p,limit=50,snapshot=summary['snapshot']))
    assert page['total']==5101 and len(page['rows'])==50 and page['snapshot']==summary['snapshot']
    with pytest.raises(ValueError):analytics.history_chart(store,dict(p,limit=5001))
    with pytest.raises(ValueError):analytics.history_page(store,dict(p,sort='time;DROP TABLE bars'))
    store.query("UPDATE bars SET close=13 WHERE time=%s",(start,))
    with pytest.raises(ValueError,match='已更新'):analytics.history_page(store,dict(p,snapshot=summary['snapshot']))


def test_dataset_edit_conflict_and_queued_snapshot(console_app):
    app=console_app;store=app.store
    item=store.create_dataset(dict(name='原配置',members=['000001.SZ']))
    queued=store.create_job('download',app.prepare_download(dict(dataset_id=str(item['id']),start='2026-09-01',end='2026-09-02')))
    updated=app.dispatch('POST','/console/datasets/'+str(item['id']),dict(revision=item['revision'],name='已编辑',periods=['1d','5m'],schedule_time='18:00'))
    assert updated['revision']==2 and updated['name']=='已编辑'
    assert store.job(queued['id'])['payload']['periods']==['1d']
    with pytest.raises(ValueError,match='版本|已变更'):app.dispatch('POST','/console/datasets/'+str(item['id']),dict(revision=1,name='旧表单'))
    with pytest.raises(ValueError):app.dispatch('POST','/console/datasets/'+str(item['id']),dict(revision=2,source='tushare'))


def test_maintenance_draft_preview_run_dedup_and_edit(console_app):
    app=console_app;store=app.store
    dataset=store.create_dataset(dict(name='维护范围',members=['000001.SZ']))
    p=dict(name='每日更新',kind='download',payload={'dataset_id':str(dataset['id'])},schedule_time='17:00',lookback_days=5)
    plan=app.dispatch('POST','/console/maintenance',p)
    assert not plan['enabled'] and store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==0
    url='/console/maintenance/'+str(plan['id'])
    preview=app.dispatch('POST',url+'/preview',dict(start='2026-09-01',end='2026-09-02'))
    first=app.dispatch('POST',url+'/run',{'preview_key':preview['preview_key']})
    assert app.dispatch('POST',url+'/run',{'preview_key':preview['preview_key']})==first
    assert store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==1
    assert store.query('SELECT last_date FROM maintenance_plans WHERE id=%s',(plan['id'],),one=True)['last_date'] is None
    enabled=app.dispatch('POST',url,dict(revision=plan['revision'],enabled=True))
    with pytest.raises(ValueError,match='停用'):app.dispatch('POST',url,dict(p,revision=enabled['revision']))
    with pytest.raises(ValueError,match='版本|已变更'):app.dispatch('POST',url,dict(revision=1,name='并发覆盖'))


def test_operations_summary_matches_filtered_pages(console_app):
    store=console_app.store
    for i in range(55):store.create_job('export',dict(source='qmt',format='csv'))
    store.create_job('download',dict(source='tushare'))
    p={'sources':['qmt'],'kinds':['export']}
    stats=analytics.operations_summary(store,p)
    page=store.jobs_page(dict(p,limit=50))
    assert stats['total']==page['total']==55 and len(page['rows'])==50
    assert stats['snapshot']==page['snapshot']
    assert sum(row['count'] for row in stats['counts'])==55
    assert sum(row['count'] for row in stats['trend'])==55
    assert len(store.jobs_page(dict(p,select_all=True))['rows'])==55


def test_futures_summary_never_adds_incompatible_or_cumulative_values(console_app):
    from pfor_qmt.futures import write_records,page,REPORTS
    store=console_app.store
    rows=[dict(source='tushare',exchange='SHFE',symbol='CU',trade_date='2026-09-01',row_key=str(i),warehouse=name,vol=value,unit=unit) for i,(name,value,unit) in enumerate([('总计',100,'吨'),('仓库甲',60,'吨'),('仓库乙',40,'吨'),('仓库丙',1,'手')])]
    rows=[{**dict.fromkeys(REPORTS['warehouse']['fields']),**row} for row in rows]
    with store.connect() as conn:write_records(conn,'warehouse',rows)
    p=dict(resource='warehouse',exchange='SHFE',symbol='CU',start='2026-09-01',end='2026-09-01')
    result=analytics.futures_summary(store,p)
    assert result['total']==4 and 'volume' not in result and 'sum' not in result
    assert result['snapshot']==page(store,p,limit=2)['snapshot']
    class StreamingConnection:
        def __init__(self,connection): self.connection=connection
        def execute(self,statement,args):
            assert 'count(*)' not in statement.lower(), 'Export batches must not recount the full range'
            return self.connection.execute(statement,args)
    with store.connect() as connection:
        first=page(store,p,limit=2,conn=StreamingConnection(connection))
        last=page(store,p,limit=2,offset=first['next_offset'],conn=StreamingConnection(connection))
        assert len(first['rows'])==len(last['rows'])==2 and last['next_offset'] is None


def test_configuration_versions_pagination_and_secret_redaction(console_app):
    app=console_app;store=app.store
    old=app.settings.revision
    saved=app.dispatch('POST','/sources/tushare/accounts',dict(id='main',name='账号',token='synthetic-console-token',config_revision=old))
    assert 'synthetic-console-token' not in str(saved)
    with pytest.raises(ValueError,match='其他操作'):
        app.dispatch('POST','/sources/tushare/accounts',dict(id='main',name='过期表单',config_revision=old))
    result=app.dispatch('POST','/settings',dict(port=18866,config_revision=app.settings.revision))
    assert result['restart_required'] and result['port']==18866
    for i in range(55):store.create_dataset(dict(name=f'集合{i:02}',members=['000001.SZ']))
    result=app.dispatch('POST','/datasets/query',dict(search='集合',sort='name',direction='desc',limit=50))
    assert result['total']==55 and result['next_offset']==50 and result['rows'][0]['name']=='集合54'
    with pytest.raises(ValueError):app.dispatch('POST','/datasets/query',dict(sort='name;DELETE FROM datasets'))


def test_operational_maintenance_updates_keep_configuration_revision(console_app):
    app=console_app;store=app.store
    plan=app.dispatch('POST','/console/maintenance',dict(name='目录维护',kind='catalog',payload={'source':'qmt','kinds':['stock']}))
    store.query("UPDATE maintenance_plans SET last_error='offline',last_date=CURRENT_DATE,updated_at=now() WHERE id=%s",(plan['id'],))
    row=store.query('SELECT revision FROM maintenance_plans WHERE id=%s',(plan['id'],),one=True)
    assert row['revision']==plan['revision']
    url='/console/maintenance/'+str(plan['id'])
    assert not app.dispatch('POST',url+'/scope-preview',dict(revision=row['revision'],kind='catalog',payload=plan['payload']))['changed']
    with pytest.raises(ValueError):
        app.dispatch('POST','/console/maintenance',dict(name='错误来源',kind='download',payload={'source':'qmt','resource':'settle','code':'CU2610.SHF'}))


def test_public_sorting_and_summary_options_reach_shared_queries(console_app):
    app=console_app;store=app.store
    store.save_security('000002.SZ','万科A','stock',{})
    page=app.dispatch('GET','/catalog/securities',dict(source='qmt',sort='code',direction='desc',limit=1))
    assert page['total']==2 and page['rows'][0]['code']=='000002.SZ'
    with pytest.raises(ValueError):app.dispatch('GET','/catalog/securities',dict(sort='unknown'))
    rows=app.dispatch('POST','/history/query',dict(members=['000001.SZ'],periods=['1d'],start='2026-09-01',end='2026-09-02',include_total=True))
    assert rows['total']==0 and rows['snapshot']
    with pytest.raises(ValueError):analytics.history_filter([])


def test_name_edit_does_not_drop_existing_minutes_when_permission_is_unavailable(console_app):
    from test_tushare import account,seed
    app=console_app;seed(app.store);app.settings.accounts=[account()];app.settings.default_account_id='main'
    row=app.store.create_dataset(dict(name='原范围',source='tushare',account_id='main',endpoint=account()['endpoint'],members=['CU2610.SHF'],periods=['1d','1m']))
    app.capabilities['main']={'capabilities':{'minutes':{'state':'permission'}}}
    saved=app.dispatch('POST','/console/datasets/'+str(row['id']),dict(revision=row['revision'],name='只改名称',periods=['1d','1m']))
    assert saved['periods']==['1d','1m'] and saved['name']=='只改名称'


def test_database_configuration_does_not_deadlock_worker_progress(console_app):
    import threading
    app=console_app;published=threading.Event();old=app.worker
    def finish():
        old.stop.wait(10)
        app.publish({'event':'job','data':{}})
        published.set()
    old.thread=threading.Thread(target=finish,daemon=True);old.thread.start()
    try:
        result=app.dispatch('POST','/settings',dict(dsn=app.store.dsn,config_revision=app.settings.revision))
        assert result['database_configured'] and published.is_set()
        assert app.store.schema.startswith('pfor_qmt_test_')
    finally:
        for worker in (old,app.worker,app.tushare_worker):
            worker.stop.set()
            if worker.thread: worker.thread.join(10)
