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
from .identifiers import provider_name
from .accounts import profile
from .tushare import TushareSource


class Application:
    def __init__(self, settings=None, store=None, source=None):
        self.settings = settings or Settings()
        self.store = store or Store(self.settings.dsn)
        self.source = source or xtdata
        self.listeners = set()
        self.lock = threading.RLock()
        self.sessions, self.tickets = {}, {}
        self.worker = Worker(self.store, self.settings.runtime, self.publish, self.source)
        self.tushare_worker = Worker(self.store,self.settings.runtime,self.publish,provider='tushare',account_resolver=lambda identifier: self.settings.account(identifier))
        self.capabilities = {}
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
        source = provider_name(p.get('source', 'qmt'))
        if source != 'qmt' and path in ('/quotes','/securities/sync','/indices','/indices/refresh','/boards','/boards/members','/boards/refresh','/source/test','/source/diagnostics','/sectors','/factors'):
            raise ValueError('Tushare本期不支持该接口，请选择期货目录或历史行情')
        if path == '/sources' and method == 'GET':
            return {'sources':[{'id':'qmt','realtime':True,'kinds':list(KINDS)},
                               {'id':'tushare','realtime':False,'kinds':['future'],'periods':['1d','1m','5m'],'continuous_minutes':False}],
                    'tushare':self.settings.public_accounts(),'capabilities':self.capabilities}
        if path == '/sources/tushare/accounts' and method == 'GET':
            return self.settings.public_accounts()
        if path == '/sources/tushare/accounts' and method == 'POST':
            with self.lock:
                for field in ('enabled','default','clear_token'):
                    if field in p and not isinstance(p[field],bool):
                        raise ValueError('账号开关必须是布尔值')
                profile({'id':p.get('id','')})
                candidate = copy.deepcopy(self.settings)
                identifier = p.get('id','')
                previous = next((row for row in candidate.accounts if row['id'] == identifier), None)
                updated = dict(previous or {'id':identifier})
                for key in ('name','endpoint','enabled','timeout','requests_per_minute'):
                    if key in p:
                        if key in ('endpoint','timeout','requests_per_minute') and 'PFOR_QMT_TUSHARE_' + identifier.upper() + '_' + key.upper() in os.environ:
                            raise ValueError('账号配置由环境变量管理，请修改环境变量并重启')
                        updated[key] = p[key]
                if p.get('token') or p.get('clear_token'):
                    if 'PFOR_QMT_TUSHARE_' + identifier.upper() + '_TOKEN' in os.environ:
                        raise ValueError('Token由环境变量管理，请修改环境变量并重启')
                    updated['token'] = '' if p.get('clear_token') else p['token']
                updated = profile(updated, effective=False)
                if previous and (profile(previous)['endpoint'] != profile(updated)['endpoint'] or not updated['enabled']):
                    active = store.query("SELECT 1 FROM jobs WHERE payload->>'source'='tushare' AND payload->>'account_id'=%s AND state IN ('queued','running') LIMIT 1", (identifier,), one=True)
                    if active:
                        raise ValueError('请先取消该账号的排队或运行中任务，再修改端点或停用账号')
                candidate.accounts = [updated if row['id']==identifier else row for row in candidate.accounts] if previous else candidate.accounts + [updated]
                if p.get('default') or not candidate.default_account_id:
                    candidate.default_account_id = identifier
                candidate.save()
                self.settings = candidate
                self.capabilities.pop(identifier, None)
                return candidate.public_accounts()
        if path.startswith('/sources/tushare/accounts/') and method == 'POST':
            parts = path.strip('/').split('/')
            if len(parts) != 5 or parts[4] not in ('test','delete'):
                raise LookupError('接口不存在')
            identifier, operation = parts[3:]
            if operation == 'test':
                account = self.settings.account(identifier)
                result = TushareSource(account).probe()
                with self.lock:
                    if self.settings.account(identifier) == account:
                        self.capabilities[identifier] = result
                return result
            with self.lock:
                if store.query('SELECT 1 FROM datasets WHERE account_id=%s LIMIT 1',(identifier,),one=True) or store.query("SELECT 1 FROM jobs WHERE payload->>'account_id'=%s AND state IN ('queued','running') LIMIT 1",(identifier,),one=True):
                    raise ValueError('账号被数据集或活动任务引用，请先解绑或取消任务')
                candidate = copy.deepcopy(self.settings)
                candidate.accounts = [row for row in candidate.accounts if row['id'] != identifier]
                if candidate.default_account_id == identifier:
                    candidate.default_account_id = candidate.accounts[0]['id'] if candidate.accounts else ''
                candidate.save()
                self.settings = candidate
                self.capabilities.pop(identifier,None)
                return candidate.public_accounts()
        if method == 'GET' and path == '/status':
            return {'settings': self.settings.public(), 'database': store.health(), 'source': 'qmt',
                    'worker': self.worker.last_error, 'export_worker': self.worker.export_error, 'catalog_worker': self.worker.catalog_error,
                    'tushare_worker':self.tushare_worker.last_error,'tushare_catalog_worker':self.tushare_worker.catalog_error,
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
                self.tushare_worker.stop.set()
                if self.tushare_worker.thread:
                    self.tushare_worker.thread.join(timeout=6)
                    if self.tushare_worker.thread.is_alive():
                        raise ValueError('Tushare任务尚未停止，请稍后重试')
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
                    self.tushare_worker = Worker(store,self.settings.runtime,self.publish,provider='tushare',account_resolver=lambda identifier: self.settings.account(identifier))
                    self.tushare_worker.start()
                raise
            self.settings = candidate
            if changing_database:
                store.dsn = candidate.dsn
                self.worker = Worker(store, self.settings.runtime, self.publish, self.source)
                self.worker.start()
                self.tushare_worker = Worker(store,candidate.runtime,self.publish,provider='tushare',account_resolver=lambda identifier: self.settings.account(identifier))
                self.tushare_worker.start()
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
            return store.securities(p.get('search', ''), p.get('kind', ''),source)
        if method == 'GET' and path == '/catalog/securities':
            return store.catalog_page(p.get('search', ''), p.get('kind', ''), p.get('limit', 50), p.get('offset', 0), p.get('market', ''), p.get('subtype', ''),source,str(p.get('active','false')).lower()=='true')
        if method == 'POST' and path == '/catalog/resolve':
            return store.query('SELECT code,name,kind,market,subtype,metadata,source,instrument_id FROM securities WHERE source=%s AND code=ANY(%s) ORDER BY code', (source,codes(p['members'],source)))
        if method == 'GET' and path == '/catalog/detail':
            row = store.query('SELECT * FROM securities WHERE source=%s AND code=%s', (source,codes([p['code']],source)[0]), one=True)
            if not row:
                raise ValueError('该证券或合约尚未同步资料')
            return row
        if method == 'GET' and path == '/catalog':
            job = store.query("SELECT * FROM jobs WHERE kind='catalog' AND coalesce(payload->>'source','qmt')=%s ORDER BY created_at DESC LIMIT 1",(source,),one=True)
            return {'counts': store.query('SELECT kind,count(*) AS count,max(updated_at) AS updated_at FROM securities WHERE source=%s GROUP BY kind',(source,)),
                    'sectors': [row['name'] for row in store.query('SELECT name FROM catalog_sectors ORDER BY name')] if source=='qmt' else [],
                    'boards': store.query("SELECT category,count(*) AS count FROM catalog_sectors WHERE category IN ('industry','concept') GROUP BY category") if source=='qmt' else [],
                    'job': job_summary(job) if job else None, 'error': (self.tushare_worker if source=='tushare' else self.worker).catalog_error}
        if method == 'POST' and path == '/catalog/sync':
            account = self.settings.account(p.get('account_id')) if source=='tushare' else {}
            return job_summary(store.create_catalog_job(p.get('kinds', ['future'] if source=='tushare' else list(KINDS)+['board']),source,account.get('id'),account.get('endpoint')))
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
            if source != 'qmt':
                raise ValueError('Tushare本期不提供实时行情，请查询历史库')
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
            if source == 'tushare':
                account = self.settings.account(p.get('account_id'))
                self.check_minutes(account['id'],p.get('periods',['1d']))
                p = dict(p,account_id=account['id'],endpoint=account['endpoint'])
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
            if method == 'POST' and len(parts) == 3 and parts[2] == 'account':
                if dataset['source'] != 'tushare':
                    raise ValueError('仅Tushare数据集支持账号切换')
                account = self.settings.account(p.get('account_id'))
                store.query('UPDATE datasets SET account_id=%s,endpoint=%s WHERE id=%s',(account['id'],account['endpoint'],parts[1]))
                return {'account_id':account['id']}
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
            return store.history(p['code'], p.get('period','1d'), p.get('start','1990-01-01'), p.get('end','2100-01-01'), p.get('limit',500), p.get('offset',0),source=source)
        if method == 'GET' and path == '/contract-mappings':
            return store.query('SELECT * FROM contract_mappings WHERE source=%s AND code=%s AND trading_day BETWEEN %s AND %s ORDER BY trading_day LIMIT 2000', (source,codes([p['code']],source)[0],day(p.get('start','1990-01-01')),day(p.get('end','2100-01-01'))))
        if method == 'GET' and path == '/factors':
            return store.query('SELECT * FROM factors WHERE code=%s', (codes([p['code']])[0],), one=True)
        if method == 'GET' and path == '/calendar':
            market = p.get('market','SH')
            if market not in MARKETS:
                raise ValueError('不支持的交易所代码')
            return store.query('SELECT day FROM trading_dates WHERE source=%s AND market=%s AND day BETWEEN %s AND %s ORDER BY day',
                               (source,market,day(p.get('start','1990-01-01')),day(p.get('end','2100-01-01'))))
        if method == 'GET' and path == '/jobs':
            return store.query("SELECT id,kind,state,payload-'chunks' AS payload,jsonb_array_length(coalesce(payload->'chunks','[]'::jsonb)) AS total_chunks,checkpoint,attempts,cancel_requested,CASE WHEN kind='download' THEN jsonb_build_object('rows',(SELECT coalesce(sum(row_count),0) FROM coverage WHERE job_id=jobs.id)) || (result-'coverage') ELSE result-'coverage' END AS result,error,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT 200")
        if method == 'POST' and path == '/downloads':
            dataset = store.query('SELECT * FROM datasets WHERE id=%s', (p['dataset_id'],), one=True)
            if not dataset:
                raise ValueError('数据集不存在')
            selected = p.get('periods', dataset['periods'])
            if not isinstance(selected, (list, tuple)):
                raise ValueError('回补周期需要非空数组')
            selected = periods(selected)
            if set(selected) - set(dataset['periods']):
                raise ValueError('回补周期必须属于所选数据集')
            payload = {'dataset_id': str(dataset['id']), 'members': dataset['members'], 'periods': selected, 'start': p.get('start'), 'end': p.get('end')}
            payload.update(source=dataset['source'],account_id=dataset['account_id'],endpoint=dataset['endpoint'])
            if dataset['source'] == 'tushare':
                self.settings.account(dataset['account_id'])
                self.check_minutes(dataset['account_id'],selected)
            payload['chunks'] = chunks(payload)
            return job_summary(store.create_job('download', payload))
        if method == 'POST' and path == '/exports':
            payload = dict(members=codes(p['members'],source),source=source, period=periods([p['period']])[0], start=day(p['start']).isoformat(), end=day(p['end']).isoformat(), format=p['format'])
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

    def check_minutes(self, account_id, selected):
        capability = self.capabilities.get(account_id,{}).get('capabilities',{}).get('minutes',{})
        if set(selected) - {'1d'} and capability.get('state') in ('permission','authentication','unsupported'):
            raise ValueError('该账号分钟接口权限不可用，请选择日线或更新账号后重新检测')
