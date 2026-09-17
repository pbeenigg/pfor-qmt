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

    def securities(self, search='', kind=''):
        return self.request('/securities', search=search, kind=kind)

    def catalog(self, search='', kind='', limit=50, offset=0, market='', subtype=''):
        return self.request('/catalog/securities', search=search, kind=kind, limit=limit, offset=offset, market=market, subtype=subtype)

    def sync_catalog(self, kinds=('future', 'option', 'stock', 'index', 'fund', 'bond', 'board')):
        return self.request('/catalog/sync', {'kinds': list(kinds)})

    def instrument(self, code):
        return self.request('/catalog/detail', code=code)

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

    def history(self, code, period='1d', start='1990-01-01', end='2100-01-01', limit=500, offset=0):
        return self.request('/history', code=code, period=period, start=start, end=end, limit=limit, offset=offset)

    def download(self, dataset_id, start=None, end=None):
        return self.request('/downloads', dict(dataset_id=dataset_id, start=start, end=end))

    def job(self, identifier):
        return self.request('/jobs/' + identifier)

    def cancel(self, identifier):
        return self.request('/jobs/' + identifier + '/cancel', {})

    def retry(self, identifier):
        return self.request('/jobs/' + identifier + '/retry', {})

    def export(self, members, period, start, end, format='csv'):
        return self.request('/exports', dict(members=members, period=period, start=start, end=end, format=format))

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
