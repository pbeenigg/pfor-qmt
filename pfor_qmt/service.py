import json
import copy
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timedelta

from . import xtdata
from .client import get_client
from .data import codes, periods, chunks, day, SHANGHAI, json_default
from .deploy import inspect_root, prepare, activate
from .protocol import encode_value
from .settings import Settings
from .storage import Store, job_summary
from .tasks import Worker
from .symbols import KINDS, MARKETS


class Application:
    def __init__(self, settings=None, store=None, source=None):
        self.settings = settings or Settings()
        self.store = store or Store(self.settings.dsn)
        self.source = source or xtdata
        self.listeners = set()
        self.lock = threading.RLock()
        self.sessions, self.tickets = {}, {}
        self.worker = Worker(self.store, self.settings.runtime, self.publish, self.source)
        self.ws_port = self.settings.value('ws_port')

    def publish(self, event):
        with self.lock:
            for listener in list(self.listeners):
                try:
                    listener.put_nowait(event)
                except queue.Full:
                    pass

    def dispatch(self, method, path, p):
        store = self.store
        if method == 'GET' and path == '/status':
            return {'settings': self.settings.public(), 'database': store.health(), 'source': 'qmt',
                    'worker': self.worker.last_error, 'export_worker': self.worker.export_error, 'catalog_worker': self.worker.catalog_error,
                    'version': '0.1.0', 'ws_port': self.ws_port}
        if method == 'POST' and path == '/source/test':
            return get_client().request('pfor.ping', timeout=4)
        if method == 'POST' and path == '/source/diagnostics':
            from .diagnostics import check_source
            return check_source(self.source, lambda: get_client().request('pfor.ping', timeout=4), p.get('code', '000300.SH'), qmt_root=self.settings.qmt_root)
        if method == 'GET' and path == '/settings':
            return self.settings.public()
        if method == 'POST' and path == '/settings':
            candidate = copy.deepcopy(self.settings)
            changing_database = bool(p.get('dsn'))
            if changing_database:
                if 'PFOR_QMT_DATABASE_URL' in os.environ:
                    raise ValueError('连接由 PFOR_QMT_DATABASE_URL 管理，请修改环境变量并重启服务')
                candidate.data['dsn'] = p['dsn']
            if 'qmt_root' in p:
                if 'PFOR_QMT_QMT_ROOT' in os.environ:
                    if p['qmt_root'] != self.settings.qmt_root:
                        raise ValueError('QMT 目录由 PFOR_QMT_QMT_ROOT 管理，请修改环境变量并重启服务')
                else:
                    candidate.data['qmt_root'] = p['qmt_root']
            if 'login_enabled' in p and not isinstance(p['login_enabled'], bool):
                raise ValueError('login_enabled 必须为布尔值')
            if p.get('login_enabled') is False:
                candidate.set_password('')
            elif 'password' in p and p['password']:
                candidate.set_password(p['password'])
            elif p.get('login_enabled') and not candidate.data['login_hash']:
                raise ValueError('启用网页登录密码时，需要设置至少 12 位的密码')
            candidate._validate()
            if changing_database:
                Store(candidate.dsn).connect().close()
                self.worker.stop.set()
                if self.worker.thread:
                    self.worker.thread.join(timeout=6)
                    if self.worker.thread.is_alive():
                        raise ValueError('当前任务正在保存检查点，请稍后再次保存连接')
            try:
                candidate.save()
            except Exception:
                if changing_database:
                    self.worker = Worker(store, self.settings.runtime, self.publish, self.source)
                    self.worker.start()
                raise
            self.settings = candidate
            if changing_database:
                store.dsn = candidate.dsn
                self.worker = Worker(store, self.settings.runtime, self.publish, self.source)
                self.worker.start()
            return self.settings.public()
        if method == 'POST' and path == '/database/migrate':
            store.migrate()
            return store.health()
        if method == 'POST' and path.startswith('/deploy/'):
            action = path.rsplit('/', 1)[-1]
            root = p.get('qmt_root') or self.settings.qmt_root
            if action == 'inspect':
                return inspect_root(root)
            if action == 'prepare':
                return prepare(root, pipe_config=self.settings.pipe)
            if action == 'activate':
                return activate(root, p.get('account', ''))
        if method == 'GET' and path == '/securities':
            return store.securities(p.get('search', ''), p.get('kind', ''))
        if method == 'GET' and path == '/catalog/securities':
            return store.catalog_page(p.get('search', ''), p.get('kind', ''), p.get('limit', 50), p.get('offset', 0), p.get('market', ''), p.get('subtype', ''))
        if method == 'POST' and path == '/catalog/resolve':
            return store.query('SELECT code,name,kind,market,subtype,metadata FROM securities WHERE code=ANY(%s) ORDER BY code', (codes(p['members']),))
        if method == 'GET' and path == '/catalog/detail':
            row = store.query('SELECT * FROM securities WHERE code=%s', (codes([p['code']])[0],), one=True)
            if not row:
                raise ValueError('该证券或合约尚未同步资料')
            return row
        if method == 'GET' and path == '/catalog':
            job = store.query("SELECT * FROM jobs WHERE kind='catalog' ORDER BY created_at DESC LIMIT 1", one=True)
            return {'counts': store.query('SELECT kind,count(*) AS count,max(updated_at) AS updated_at FROM securities GROUP BY kind'),
                    'sectors': [row['name'] for row in store.query('SELECT name FROM catalog_sectors ORDER BY name')],
                    'boards': store.query("SELECT category,count(*) AS count FROM catalog_sectors WHERE category IN ('industry','concept') GROUP BY category"),
                    'job': job_summary(job) if job else None, 'error': self.worker.catalog_error}
        if method == 'POST' and path == '/catalog/sync':
            return job_summary(store.create_catalog_job(p.get('kinds', list(KINDS) + ['board'])))
        if method == 'GET' and path == '/boards':
            return store.boards(p.get('search', ''), p.get('category', ''), p.get('limit', 50), p.get('offset', 0))
        if method == 'GET' and path == '/boards/members':
            return store.board(p['name'])
        if method == 'POST' and path == '/boards/refresh':
            board = store.query("SELECT * FROM catalog_sectors WHERE name=%s AND category IN ('industry','concept')", (p['name'],), one=True)
            if not board:
                raise ValueError('请先同步行业概念目录')
            members = self.source.get_stock_list_in_sector(board['name'])
            with store.connect() as conn:
                store.save_board(board, members, conn)
            return store.board(board['name'])
        if method == 'POST' and path == '/securities/sync':
            selected = codes(p['members'])
            kind = p['kind']
            if kind not in KINDS + ('etf',):
                raise ValueError('无效证券类别')
            if len(selected) > 100:
                raise ValueError('一次同步不超过 100 个证券')
            for code in selected:
                detail = self.source.get_instrument_detail(code)
                if not detail:
                    raise ValueError('证券资料为空: ' + code)
                name = detail.get('InstrumentName') or detail.get('StockName')
                if not name:
                    raise ValueError('证券名称为空: ' + code)
                from .catalog import instrument_metadata
                store.save_security(code, name, kind, encode_value(detail), metadata=instrument_metadata(detail))
            return store.securities()
        if method == 'GET' and path == '/quotes':
            return encode_value(self.source.get_full_tick(codes(p['codes'])))
        if method == 'GET' and path == '/sectors':
            return self.source.get_sector_list()
        if method == 'GET' and path == '/indices':
            return store.query('SELECT m.*,s.id AS snapshot_id,s.members,s.observed_at FROM index_mapping m LEFT JOIN LATERAL (SELECT * FROM constituent_snapshots WHERE index_code=m.code ORDER BY observed_at DESC LIMIT 1) s ON true ORDER BY m.code')
        if method == 'POST' and path == '/indices/refresh':
            code = codes([p['code']])[0]
            security = store.query("SELECT name FROM securities WHERE code=%s AND kind='index'", (code,), one=True)
            members = self.source.get_stock_list_in_sector(p['sector'])
            return store.snapshot(code, p['sector'], security['name'] if security else p.get('name') or p['sector'], members)
        if method == 'GET' and path == '/datasets':
            return store.query('SELECT * FROM datasets ORDER BY created_at DESC')
        if method == 'POST' and path == '/datasets':
            return store.create_dataset(p)
        if path.startswith('/datasets/'):
            parts = path.strip('/').split('/')
            dataset = store.query('SELECT * FROM datasets WHERE id=%s', (parts[1],), one=True)
            if not dataset:
                raise ValueError('数据集不存在')
            if method == 'POST' and len(parts) == 3 and parts[2] == 'schedule':
                if not isinstance(p.get('enabled'), bool):
                    raise ValueError('enabled 必须为布尔值')
                store.query('UPDATE datasets SET scheduled=%s,schedule_from=%s WHERE id=%s', (bool(p['enabled']), datetime.now(SHANGHAI).date(), parts[1]))
                return {'scheduled': bool(p['enabled'])}
            if method == 'POST' and len(parts) == 3 and parts[2] == 'refresh':
                if dataset.get('board_name'):
                    snapshot = self.dispatch('POST', '/boards/refresh', {'name': dataset['board_name']})
                    if not snapshot['members']:
                        raise ValueError('板块成员为空，保留原数据集')
                    from .storage import document
                    store.query('UPDATE datasets SET members=%s,board_snapshot_id=%s WHERE id=%s', (document(snapshot['members']), snapshot['id'], parts[1]))
                    return snapshot
                mapping = store.query('SELECT * FROM index_mapping WHERE code=%s', (dataset['index_code'],), one=True)
                if not mapping:
                    raise ValueError('此数据集没有指数映射')
                snapshot = store.snapshot(mapping['code'], mapping['sector'], mapping['name'], self.source.get_stock_list_in_sector(mapping['sector']))
                from .storage import document
                store.query('UPDATE datasets SET members=%s,snapshot_id=%s WHERE id=%s', (document(snapshot['members']), snapshot['id'], parts[1]))
                return snapshot
        if method == 'GET' and path == '/history':
            return store.history(p['code'], p.get('period','1d'), p.get('start','1990-01-01'), p.get('end','2100-01-01'), p.get('limit',500), p.get('offset',0))
        if method == 'GET' and path == '/factors':
            return store.query('SELECT * FROM factors WHERE code=%s', (codes([p['code']])[0],), one=True)
        if method == 'GET' and path == '/calendar':
            market = p.get('market','SH')
            if market not in MARKETS:
                raise ValueError('不支持的交易所代码')
            return store.query('SELECT day FROM trading_dates WHERE market=%s AND day BETWEEN %s AND %s ORDER BY day',
                               (market,day(p.get('start','1990-01-01')),day(p.get('end','2100-01-01'))))
        if method == 'GET' and path == '/jobs':
            return store.query("SELECT id,kind,state,payload-'chunks' AS payload,jsonb_array_length(coalesce(payload->'chunks','[]'::jsonb)) AS total_chunks,checkpoint,attempts,cancel_requested,result-'coverage' AS result,error,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT 200")
        if method == 'POST' and path == '/downloads':
            dataset = store.query('SELECT * FROM datasets WHERE id=%s', (p['dataset_id'],), one=True)
            if not dataset:
                raise ValueError('数据集不存在')
            payload = {'dataset_id': str(dataset['id']), 'members': dataset['members'], 'periods': dataset['periods'], 'start': p.get('start'), 'end': p.get('end')}
            payload['chunks'] = chunks(payload)
            return job_summary(store.create_job('download', payload))
        if method == 'POST' and path == '/exports':
            payload = dict(members=codes(p['members']), period=periods([p['period']])[0], start=day(p['start']).isoformat(), end=day(p['end']).isoformat(), format=p['format'])
            if payload['format'] not in ('csv','parquet') or day(payload['start']) > day(payload['end']):
                raise ValueError('无效导出格式或日期范围')
            return store.create_job('export', payload)
        if path.startswith('/jobs/'):
            parts = path.strip('/').split('/')
            job = store.job(parts[1])
            if method == 'GET' and len(parts) == 2:
                return job
            if method == 'POST' and len(parts) == 3:
                if parts[2] == 'cancel' and job['state'] in ('queued','running'):
                    store.update_job(job['id'], cancel_requested=True, **({'state':'cancelled'} if job['state'] == 'queued' else {}))
                elif parts[2] == 'retry' and job['state'] in ('failed','cancelled','partial'):
                    checkpoint = 0 if job['state'] == 'partial' or job['kind'] == 'export' else job['checkpoint']
                    store.update_job(job['id'], state='queued', cancel_requested=False, error=None, attempts=0, checkpoint=checkpoint)
                else:
                    raise ValueError('任务状态不允许此操作')
                return store.job(job['id'])
        if method == 'POST' and path == '/ws-ticket':
            ticket = secrets.token_urlsafe(32)
            with self.lock:
                self.tickets = {key: until for key, until in self.tickets.items() if until > time.time()}
                self.tickets[ticket] = time.time() + 30
            return {'ticket': ticket, 'port': self.ws_port}
        raise LookupError('接口不存在')
