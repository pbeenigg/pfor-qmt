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

    def futures_options(self, exchange='DCE'):
        return self.request('/futures/options',source='tushare',exchange=exchange)

    def futures_records(self, resource, start, end, **options):
        return self.request('/futures/records',source='tushare',resource=resource,start=start,end=end,**options)

    def select_catalog(self, **filters):
        return self.request('/catalog/select', filters)

    def query_history(self, members, periods, start, end, source='qmt', **paging):
        return self.request('/history/query', dict(members=members,periods=periods,start=start,end=end,source=source,**paging))

    def query_futures(self, selections, start, end, **paging):
        return self.request('/futures/records', dict(source='tushare',selections=selections,start=start,end=end,**paging))

    def sync_futures_batch(self, selections, start, end, account_id=None):
        return self.request('/futures/sync', dict(source='tushare',selections=selections,start=start,end=end,account_id=account_id))

    def download_batch(self, dataset_ids, periods, start=None, end=None, source='qmt'):
        return self.request('/downloads/batch',dict(dataset_ids=dataset_ids,periods=periods,start=start,end=end,source=source))

    def export_batch(self, members, periods, start, end, format='csv', source='qmt'):
        return self.request('/exports/batch',dict(members=members,periods=periods,start=start,end=end,format=format,source=source))

    def export_futures_batch(self, selections, start, end, format='csv'):
        return self.request('/futures/export',dict(source='tushare',selections=selections,start=start,end=end,format=format))

    def query_jobs(self, **filters):
        return self.request('/jobs/query', filters)

    def sync_futures(self, resource, start, end, account_id=None, **options):
        return self.request('/futures/sync',dict(source='tushare',resource=resource,start=start,end=end,account_id=account_id,**options))

    def export_futures(self, resource, start, end, format='csv', **options):
        return self.request('/futures/export',dict(source='tushare',resource=resource,start=start,end=end,format=format,**options))

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

    def job_units(self, identifier, limit=50, offset=0):
        return self.request('/jobs/' + identifier + '/units', limit=limit, offset=offset)

    def job_events(self, identifier, limit=50, before=None):
        return self.request('/jobs/' + identifier + '/events', limit=limit, **({'before':before} if before is not None else {}))

    def query_events(self, **filters):
        return self.request('/events/query', filters)

    def health(self):
        return self.request('/health')

    def freshness(self, **filters):
        return self.request('/freshness/query', filters)

    def maintenance_plans(self):
        return self.request('/maintenance')

    def create_maintenance(self, job_id, name, schedule_time=None, lookback_days=5):
        return self.request('/maintenance', dict(job_id=job_id,name=name,schedule_time=schedule_time,lookback_days=lookback_days))

    def set_maintenance(self, identifier, enabled):
        return self.request('/maintenance/'+identifier, {'enabled':enabled})

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
