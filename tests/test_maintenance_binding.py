"""Maintenance must retain the scope approved by the user, including archived jobs."""
import copy

import pytest

from test_console import console_app
from test_tushare import account, seed


def test_job_to_plan_keeps_original_members_periods_and_account(console_app):
    app=console_app;store=app.store;seed(store)
    app.settings.accounts=[account(),account(id='other')];app.settings.default_account_id='main'
    dataset=app.dispatch('POST','/datasets',dict(source='tushare',name='原数据集',members=['CU2610.SHF'],periods=['1d','5m']))
    payload=app.prepare_download(dict(dataset_id=str(dataset['id']),periods=['1d'],start='2026-09-14',end='2026-09-14'))
    original=store.create_job('download',payload)
    original=store.job(original['id'])
    store.save_security('A2610.DCE','豆一2610','future',{},source='tushare',subtype='contract')
    app.dispatch('POST','/console/datasets/'+str(dataset['id']),dict(revision=dataset['revision'],members=['A2610.DCE'],account_id='other'))
    app.settings.default_account_id='other'
    saved=app.dispatch('POST','/console/maintenance',dict(name='原任务范围',job_id=str(original['id'])))
    assert saved['payload']['members']==['CU2610.SHF']
    assert saved['payload']['periods']==['1d']
    assert saved['payload']['account_id']=='main'
    assert saved['payload']['endpoint']==original['payload']['endpoint']
    assert not saved['enabled'] and store.job(original['id'])==original
    current=app.dispatch('POST','/console/maintenance',dict(name='明确采用当前数据集',payload={'source':'tushare','dataset_id':str(dataset['id'])}))
    assert current['payload']['members']==['A2610.DCE'] and current['payload']['account_id']=='other'


def test_dataset_rename_or_disable_never_rebinds_endpoint(console_app):
    app=console_app;seed(app.store);app.settings.accounts=[account()];app.settings.default_account_id='main'
    row=app.dispatch('POST','/datasets',dict(source='tushare',name='原端点',members=['CU2610.SHF']))
    app.settings.accounts[0]['endpoint']='https://alternate.example/api'
    updated=app.dispatch('POST','/console/datasets/'+str(row['id']),dict(revision=row['revision'],name='只改名'))
    assert updated['endpoint']==row['endpoint']
    app.settings.accounts[0]['enabled']=False
    stopped=app.dispatch('POST','/console/datasets/'+str(row['id']),dict(revision=updated['revision'],scheduled=False))
    assert not stopped['scheduled'] and stopped['endpoint']==row['endpoint']


def test_bound_plan_edit_copy_and_execution_reject_endpoint_drift(console_app):
    app=console_app;seed(app.store);app.settings.accounts=[account()];app.settings.default_account_id='main'
    plan=app.dispatch('POST','/console/maintenance',dict(name='目录',kind='catalog',payload={'source':'tushare','account_id':'main','kinds':['future'],'exchanges':['SHFE']}))
    app.settings.accounts[0]['endpoint']='https://alternate.example/api'
    copied=app.dispatch('POST','/console/maintenance',dict(name='目录副本',kind=plan['kind'],payload=copy.deepcopy(plan['payload'])))
    assert copied['payload']['endpoint']==plan['payload']['endpoint']
    with pytest.raises(ValueError,match='端点'):
        app.dispatch('POST','/console/maintenance/'+str(copied['id'])+'/preview',{})
    assert app.store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==0


def test_calendar_bootstrap_explicit_range_and_permission_recheck(console_app):
    app=console_app;app.settings.accounts=[account()];app.settings.default_account_id='main'
    plan=app.dispatch('POST','/console/maintenance',dict(name='交易日历',kind='download',payload={'source':'tushare','resource':'calendar','exchange':'SHFE','account_id':'main'}))
    url='/console/maintenance/'+str(plan['id'])
    with pytest.raises(ValueError,match='日历'):
        app.dispatch('POST',url+'/preview',{})
    preview=app.dispatch('POST',url+'/preview',dict(start='2026-09-01',end='2026-09-30'))
    assert preview['ranges'][0]['start']=='2026-09-01' and preview['ranges'][0]['end']=='2026-09-30'
    app.capabilities['main']={'capabilities':{'calendar':{'state':'permission'}}}
    with pytest.raises(ValueError,match='权限'):
        app.dispatch('POST',url+'/run',{'preview_key':preview['preview_key']})
    assert app.store.query('SELECT count(*) AS n FROM jobs',one=True)['n']==0


def test_maintenance_scope_and_preview_validation(console_app):
    app=console_app
    for value in ([], 'not an object', {'members':[]}, {'source':'tushare','account_id':'main','endpoint':'https://example.test/?token=not-a-secret','resource':'calendar','exchange':'SHFE'}):
        with pytest.raises(ValueError):
            app.dispatch('POST','/console/maintenance',dict(name='无效范围',payload=value))
    plan=app.dispatch('POST','/console/maintenance',dict(name='目录',kind='catalog',payload={'source':'qmt','kinds':['stock']}))
    url='/console/maintenance/'+str(plan['id'])
    for params in ({'start':'2026-10-01','end':'2026-09-01'},{'unknown':True}):
        with pytest.raises(ValueError):app.dispatch('POST',url+'/preview',params)


def test_explicit_dataset_rebinding_and_chart_scope_boundaries(console_app):
    from pfor_qmt.analytics import history_chart
    app=console_app;seed(app.store);app.settings.accounts=[account()];app.settings.default_account_id='main'
    row=app.dispatch('POST','/datasets',dict(name='重新绑定',source='tushare',members=['CU2610.SHF']))
    app.settings.accounts[0]['endpoint']='https://alternate.example/api'
    changed=app.dispatch('POST','/console/datasets/'+str(row['id']),dict(revision=row['revision'],rebind_account=True))
    assert changed['endpoint']=='https://alternate.example/api'
    original=dict(source='qmt',members=['000001.SZ'],periods=['1d'],start='2026-09-01',end='2026-09-02')
    for changes in ({'periods':['1m']},{'end':'2026-09-03'}):
        with pytest.raises(ValueError,match='查询范围'):
            history_chart(app.store,dict(original,**changes,query_scope=original))
