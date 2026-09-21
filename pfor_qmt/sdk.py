import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DataClient:
    @classmethod
    def from_config(cls, config_path=None):
        from .settings import Settings
        settings = Settings(config_path=config_path)
        return cls(base_url='http://127.0.0.1:' + str(settings.value('port')), api_key=settings.api_key)

    def __init__(self, base_url='http://127.0.0.1:8766', api_key='', timeout=30):
        self.base_url, self.api_key, self.timeout = base_url.rstrip('/'), api_key, timeout

    def request(self, path, payload=None, **query):
        url = self.base_url + '/api/v1' + path + ('?' + urlencode(query) if query else '')
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=data, headers={'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'})
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def securities(self, search='', kind='', source='qmt'):
        return self.request('/securities', search=search, kind=kind,source=source)

    def catalog(self, search='', kind='', limit=50, offset=0, market='', subtype='', source='qmt', active=False):
        return self.request('/catalog/securities', search=search, kind=kind, limit=limit, offset=offset, market=market, subtype=subtype,source=source,active=str(active).lower())

    def sync_catalog(self, kinds=None, source='qmt', account_id=None, exchanges=None):
        return self.request('/catalog/sync', {'kinds': list(kinds or (('future',) if source=='tushare' else ('future', 'option', 'stock', 'index', 'fund', 'bond', 'board'))), 'source':source,'account_id':account_id, **({'exchanges':exchanges} if exchanges is not None else {})})

    def instrument(self, code, source='qmt'):
        return self.request('/catalog/detail', code=code, source=source)

    def sources(self):
        return self.request('/sources')

    def tushare_accounts(self):
        return self.request('/sources/tushare/accounts')

    def save_tushare_account(self, identifier, **options):
        return self.request('/sources/tushare/accounts', dict(id=identifier, **options))

    def test_tushare_account(self, identifier):
        return self.request('/sources/tushare/accounts/' + identifier + '/test', {})

    def contract_mappings(self, code, start, end, source='tushare'):
        return self.request('/contract-mappings',code=code,start=start,end=end,source=source)

    def calendar(self, market, start, end, source='qmt'):
        return self.request('/calendar',market=market,start=start,end=end,source=source)

    def futures_options(self, exchange='DCE', source='tushare'):
        return self.request('/futures/options',source=source,exchange=exchange)

    def test_qmt_references(self, resource='mapping', mapping_mode='current', **options):
        return self.request('/sources/qmt/references/test',dict(resource=resource,mapping_mode=mapping_mode,**options))

    def futures_records(self, resource, start, end, **options):
        source=options.pop('source','tushare')
        if 'dimensions' in options:
            return self.request('/futures/records',dict(source=source,resource=resource,start=start,end=end,**options))
        return self.request('/futures/records',source=source,resource=resource,start=start,end=end,**options)

    def futures_filter_options(self, selections, start, end, source='tushare'):
        return self.request('/futures/filter-options',dict(source=source,selections=selections,start=start,end=end))

    def select_catalog(self, **filters):
        return self.request('/catalog/select', filters)

    def query_history(self, members, periods, start, end, source='qmt', **paging):
        return self.request('/history/query', dict(members=members,periods=periods,start=start,end=end,source=source,**paging))

    def history_summary(self, members, periods, start, end, source='qmt', **options):
        return self.request('/history/summary',dict(members=members,periods=periods,start=start,end=end,source=source,**options))

    def history_chart(self, code, period, start, end, source='qmt', **window):
        return self.request('/history/chart',dict(code=code,period=period,start=start,end=end,source=source,**window))

    def futures_summary(self, selections, start, end, **options):
        return self.request('/futures/summary',dict(selections=selections,start=start,end=end,source=options.pop('source','tushare'),**options))

    def operations_summary(self, **filters):
        return self.request('/operations/summary',filters)

    def update_dataset(self, identifier, revision, **changes):
        return self.request('/console/datasets/'+identifier,dict(revision=revision,**changes))

    def query_datasets(self, **filters):
        return self.request('/datasets/query',filters)

    def query_maintenance(self, **filters):
        return self.request('/maintenance/query',filters)

    def update_maintenance_scope(self, identifier, revision, **changes):
        return self.request('/console/maintenance/'+identifier,dict(revision=revision,**changes))

    def save_maintenance_scope(self, **params):
        return self.request('/console/maintenance',params)

    def preview_maintenance(self, identifier, **params):
        return self.request('/console/maintenance/'+identifier+'/preview',params)

    def run_maintenance(self, identifier, preview_key, **params):
        return self.request('/console/maintenance/'+identifier+'/run',dict(preview_key=preview_key,**params))

    def query_futures(self, selections, start, end, **paging):
        return self.request('/futures/records', dict(source=paging.pop('source','tushare'),selections=selections,start=start,end=end,**paging))

    def sync_futures_batch(self, selections, start, end, account_id=None, source='tushare'):
        return self.request('/futures/sync', dict(source=source,selections=selections,start=start,end=end,account_id=account_id))

    def download_batch(self, dataset_ids, periods, start=None, end=None, source='qmt'):
        return self.request('/downloads/batch',dict(dataset_ids=dataset_ids,periods=periods,start=start,end=end,source=source))

    def export_batch(self, members, periods, start, end, format='csv', source='qmt'):
        return self.request('/exports/batch',dict(members=members,periods=periods,start=start,end=end,format=format,source=source))

    def export_futures_batch(self, selections, start, end, format='csv', **options):
        return self.request('/futures/export',dict(source=options.pop('source','tushare'),selections=selections,start=start,end=end,format=format,**options))

    def query_jobs(self, **filters):
        return self.request('/jobs/query', filters)

    def delete_dataset(self, identifier, revision):
        return self.request('/console/datasets/'+identifier+'/delete', {'revision':revision})

    def restore_dataset(self, identifier, revision):
        return self.request('/console/datasets/'+identifier+'/restore', {'revision':revision})

    def delete_maintenance(self, identifier, revision):
        return self.request('/console/maintenance/'+identifier+'/delete', {'revision':revision})

    def restore_maintenance(self, identifier, revision):
        return self.request('/console/maintenance/'+identifier+'/restore', {'revision':revision})

    def delete_job(self, identifier):
        return self.request('/jobs/'+identifier+'/delete', {})

    def restore_job(self, identifier):
        return self.request('/jobs/'+identifier+'/restore', {})

    def sync_futures(self, resource, start=None, end=None, account_id=None, **options):
        from .data import SHANGHAI
        from datetime import datetime
        today=datetime.now(SHANGHAI).date().isoformat()
        return self.request('/futures/sync',dict(source=options.pop('source','tushare'),resource=resource,start=start or today,end=end or today,account_id=account_id,**options))

    def export_futures(self, resource, start, end, format='csv', **options):
        return self.request('/futures/export',dict(source=options.pop('source','tushare'),resource=resource,start=start,end=end,format=format,**options))

    def boards(self, search='', category='', limit=50, offset=0):
        return self.request('/boards', search=search, category=category, limit=limit, offset=offset)

    def board_members(self, name):
        return self.request('/boards/members', name=name)

    def refresh_board(self, name):
        return self.request('/boards/refresh', {'name': name})

    def indices(self):
        return self.request('/indices')

    def refresh_index(self, code, sector, name=''):
        return self.request('/indices/refresh', dict(code=code, sector=sector, name=name))

    def datasets(self):
        return self.request('/datasets')

    def create_dataset(self, name, members, periods=('1d',), **options):
        return self.request('/datasets', dict(name=name, members=members, periods=list(periods), **options))

    def history(self, code, period='1d', start='1990-01-01', end='2100-01-01', limit=500, offset=0, source='qmt'):
        return self.request('/history', code=code, period=period, start=start, end=end, limit=limit, offset=offset,source=source)

    def download(self, dataset_id, start=None, end=None, periods=None):
        payload = dict(dataset_id=dataset_id, start=start, end=end)
        if periods is not None:
            payload['periods'] = list(periods)
        return self.request('/downloads', payload)

    def job(self, identifier):
        return self.request('/jobs/' + identifier)

    def cancel(self, identifier):
        return self.request('/jobs/' + identifier + '/cancel', {})

    def retry(self, identifier, unit_indices=None):
        return self.request('/jobs/' + identifier + '/retry', {} if unit_indices is None else {'unit_indices':unit_indices})

    def verify(self, identifier):
        return self.request('/jobs/' + identifier + '/verify', {})

    def preview_repair(self, identifier, unit_indices=None):
        return self.request('/jobs/'+identifier+'/repair-preview',{} if unit_indices is None else {'unit_indices':unit_indices})

    def repair(self, identifier, preview_key, unit_indices=None):
        return self.request('/jobs/'+identifier+'/repair',dict(preview_key=preview_key,**({'unit_indices':unit_indices} if unit_indices is not None else {})))

    def job_links(self, identifier, limit=50, offset=0):
        return self.request('/jobs/'+identifier+'/links',limit=limit,offset=offset)

    def job_units(self, identifier, limit=50, offset=0, **filters):
        return self.request('/jobs/' + identifier + '/units', limit=limit, offset=offset,**{key:','.join(value) if isinstance(value,(list,tuple)) else value for key,value in filters.items() if value is not None})

    def job_events(self, identifier, limit=50, before=None, **filters):
        return self.request('/jobs/' + identifier + '/events', limit=limit, **({'before':before} if before is not None else {}),**{key:','.join(value) if isinstance(value,(list,tuple)) else value for key,value in filters.items() if value is not None})

    def query_events(self, **filters):
        return self.request('/events/query', filters)

    def health(self):
        return self.request('/health')

    def runtime_events(self):
        return self.request('/runtime/events')

    def freshness(self, **filters):
        return self.request('/freshness/query', filters)

    def maintenance_plans(self):
        return self.request('/maintenance')

    def create_maintenance(self, job_id, name, schedule_time=None, lookback_days=5):
        return self.request('/maintenance', dict(job_id=job_id,name=name,schedule_time=schedule_time,lookback_days=lookback_days))

    def set_maintenance(self, identifier, enabled):
        return self.request('/maintenance/'+identifier, {'enabled':enabled})

    def update_maintenance(self, identifier, **changes):
        return self.request('/maintenance/'+identifier, changes)

    def export(self, members, period, start, end, format='csv', source='qmt'):
        return self.request('/exports', dict(members=members, period=period, start=start, end=end, format=format,source=source))

    def save_export(self, identifier, destination, metadata=False):
        from pathlib import Path
        request = Request(self.base_url + '/api/v1/files/' + identifier + ('/metadata' if metadata else ''), headers={'Authorization':'Bearer ' + self.api_key})
        with urlopen(request, timeout=self.timeout) as response, Path(destination).open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)

    def events(self, watch=None):
        from websockets.sync.client import connect
        from urllib.parse import urlsplit
        ticket = self.request('/ws-ticket', {})
        host = urlsplit(self.base_url).hostname
        with connect('ws://%s:%s/?ticket=%s' % (host, ticket['port'], ticket['ticket'])) as socket:
            if watch:
                socket.send(json.dumps({'action':'watch','codes':watch}))
            for message in socket:
                yield json.loads(message)
