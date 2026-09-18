import json
import re
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .data import codes, day, periods, json_default, FIELDS, MINUTE_PERIODS, AGGREGATE_PERIODS, period_label, filter_values
from .symbols import KINDS, MARKETS, market_of
from .identifiers import provider_name, source_market, identity_key, TS_EXCHANGES
from .reliability import STATES, classify_gaps, quality, redact, issue


def document(value):
    return Jsonb(value, dumps=lambda obj: json.dumps(obj, default=json_default, allow_nan=False))


def job_summary(job):
    result = dict(job)
    result['payload'] = dict(job['payload'])
    result['total_chunks'] = len(result['payload'].pop('chunks', []))
    result['result'] = {key:value for key,value in job['result'].items() if key != 'coverage'}
    return result


class Store:
    def __init__(self, dsn, schema='pfor_qmt'):
        if schema != 'pfor_qmt' and not re.fullmatch(r'pfor_qmt_test_[a-z0-9_]+', schema):
            raise ValueError('禁止使用其他业务 schema')
        self.dsn, self.schema = dsn, schema

    def connect(self):
        if not self.dsn:
            raise ValueError('请先配置 PostgreSQL 连接')
        conn = psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row)
        conn.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(self.schema)))
        conn.execute("SET TIME ZONE 'Asia/Shanghai'")
        return conn

    def migrate(self):
        with self.connect() as conn:
            if conn.info.server_version < 140000:
                raise ValueError('需要 PostgreSQL 14 或更高版本')
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.schema + '.migrations',))
            conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            conn.execute('CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
            applied = {row['version'] for row in conn.execute('SELECT version FROM schema_version')}
            for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
                version = int(path.stem.split('_')[0])
                if version not in applied:
                    conn.execute(path.read_text('utf-8'))
                    conn.execute('INSERT INTO schema_version(version) VALUES(%s) ON CONFLICT DO NOTHING', (version,))

    def query(self, statement, args=(), one=False):
        with self.connect() as conn:
            cursor = conn.execute(statement, args)
            if not cursor.description:
                return None
            return cursor.fetchone() if one else cursor.fetchall()

    def health(self):
        try:
            row = self.query('SELECT max(version) AS version FROM schema_version', one=True)
            return {'connected': True, 'version': row['version']}
        except Exception:
            return {'connected': False, 'message': '数据库未连接或尚未初始化'}

    def securities(self, search='', kind='', source='qmt'):
        return self.query("SELECT * FROM securities WHERE source=%s AND (code ILIKE %s OR name ILIKE %s) AND (%s='' OR kind=%s OR (%s='etf' AND subtype='etf')) ORDER BY code LIMIT 1000",
                          (provider_name(source), '%' + search + '%', '%' + search + '%', kind, kind, kind))

    def catalog_page(self, search='', kind='', limit=50, offset=0, market='', subtype='', source='qmt', active=False, select_all=False):
        limit, offset = int(limit), int(offset)
        kinds = filter_values(kind, (*KINDS, 'etf'), '证券类别')
        markets = filter_values(market, MARKETS, '交易所')
        subtypes = filter_values(subtype, ('contract','continuous','combination','efp','etf'), '合约类型')
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError('无效目录筛选或分页参数')
        query = "FROM securities WHERE source=%s AND (code ILIKE %s OR name ILIKE %s OR metadata->>'product' ILIKE %s) AND (%s OR kind=ANY(%s) OR (%s AND subtype='etf')) AND (%s OR market=ANY(%s)) AND (%s OR subtype=ANY(%s)) AND (NOT %s OR (coalesce(nullif(metadata->>'expiry',''),'99999999') >= to_char(CURRENT_DATE,'YYYYMMDD') AND coalesce(nullif(metadata->>'listed',''),'00000000') <= to_char(CURRENT_DATE,'YYYYMMDD')))"
        args = (provider_name(source), '%' + search + '%', '%' + search + '%', '%' + search + '%', not kinds, kinds, 'etf' in kinds, not markets, markets, not subtypes, subtypes, active)
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            total = conn.execute('SELECT count(*) AS n ' + query, args).fetchone()['n']
            if select_all:
                if total > 10000:
                    raise ValueError(f'筛选结果共{total}个，超过单次10000个上限，请缩小范围；未截断选择')
                limit, offset = max(1, total), 0
            rows = conn.execute('SELECT code,name,kind,market,subtype,metadata,updated_at,source,instrument_id ' + query + ' ORDER BY code LIMIT %s OFFSET %s', args + (limit, offset)).fetchall()
        return dict(rows=rows, total=total, offset=offset, next_offset=offset + len(rows) if offset + len(rows) < total else None)

    def create_catalog_job(self, kinds, source='qmt', account_id=None, endpoint=None, exchanges=None):
        from .catalog import KINDS
        if not isinstance(kinds, list) or not kinds or any(kind not in KINDS for kind in kinds):
            raise ValueError('选择有效的证券或行业概念目录')
        provider_name(source)
        if source == 'tushare' and kinds != ['future']:
            raise ValueError('Tushare当前仅支持期货目录')
        selected_exchanges = filter_values(list(TS_EXCHANGES) if exchanges is None else exchanges, TS_EXCHANGES, '期货交易所') if source=='tushare' else []
        if source=='tushare' and not selected_exchanges:
            raise ValueError('请选择至少一个期货交易所')
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.schema + '.catalog-create',))
            active = conn.execute("SELECT * FROM jobs WHERE kind='catalog' AND coalesce(payload->>'source','qmt')=%s AND state IN ('queued','running') ORDER BY created_at LIMIT 1", (source,)).fetchone()
            if active:
                if not set(kinds).issubset(active['payload']['kinds']) or active['payload'].get('account_id') != account_id:
                    raise ValueError('已有目录同步正在进行，请等待完成后再同步其他类别')
                if source=='tushare' and not set(selected_exchanges).issubset(active['payload'].get('exchanges', list(TS_EXCHANGES))):
                    raise ValueError('已有目录同步正在进行，请等待完成后再同步其他交易所')
                return active
            return conn.execute("INSERT INTO jobs(id,kind,payload) VALUES(%s,'catalog',%s) RETURNING *",
                                (str(uuid.uuid4()), document({'kinds': list(dict.fromkeys(kinds)), 'source':source, 'account_id':account_id, 'endpoint':endpoint, **({'exchanges':selected_exchanges} if source=='tushare' else {})}))).fetchone()

    def save_security(self, code, name, kind, detail, subtype='', metadata=None, conn=None, source='qmt'):
        if kind == 'etf':
            kind, subtype = 'fund', 'etf'
        if conn is None:
            with self.connect() as connection:
                return self.save_security(code, name, kind, detail, subtype, metadata, connection, source)
        code = codes([code], source)[0]
        metadata = dict(metadata or {})
        if source == 'qmt' and kind == 'future' and subtype in ('','contract') and not metadata.get('delivery_month'):
            expiry = str(metadata.get('expiry') or '')
            product = str(metadata.get('product') or '')
            body = code.rsplit('.',1)[0]
            if re.fullmatch(r'\d{8}',expiry) and product and body.upper() in ((product + expiry[2:6]).upper(), (product + expiry[3:6]).upper()):
                metadata['delivery_month'] = expiry[:6]
        key = identity_key(code, source, kind, subtype, metadata or {})
        old = conn.execute('SELECT instrument_id FROM securities WHERE source=%s AND code=%s', (source, code)).fetchone()
        identity = conn.execute('SELECT id FROM instruments WHERE identity_key=%s', (key,)).fetchone()
        if identity is None:
            legacy = conn.execute('SELECT id FROM instruments WHERE identity_key=%s', (source + ':' + code,)).fetchone()
            if legacy:
                identity = conn.execute('UPDATE instruments SET identity_key=%s WHERE id=%s RETURNING id', (key, legacy['id'])).fetchone()
            else:
                identity = conn.execute('INSERT INTO instruments(identity_key) VALUES(%s) ON CONFLICT(identity_key) DO UPDATE SET identity_key=EXCLUDED.identity_key RETURNING id', (key,)).fetchone()
        if old and old['instrument_id'] != identity['id']:
            # Changing identity after bars exist needs explicit reconciliation, never an implicit merge.
            used = conn.execute('SELECT 1 FROM bars WHERE instrument_id=%s LIMIT 1', (old['instrument_id'],)).fetchone()
            if used:
                identity = {'id': old['instrument_id']}
        conn.execute('INSERT INTO securities(code,name,kind,details,market,subtype,metadata,source,instrument_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source,code) DO UPDATE SET name=EXCLUDED.name,kind=EXCLUDED.kind,details=EXCLUDED.details,market=EXCLUDED.market,subtype=EXCLUDED.subtype,metadata=EXCLUDED.metadata,instrument_id=EXCLUDED.instrument_id,updated_at=now()',
                     (code,name,kind,document(detail),source_market(code,source),subtype,document(metadata or {}),source,identity['id']))

    def save_board(self, part, members, conn):
        if not members:
            raise ValueError('板块成员为空，保留原快照')
        members = codes(members)
        conn.execute('INSERT INTO catalog_sectors(name,category,path) VALUES(%s,%s,%s) ON CONFLICT(name) DO UPDATE SET category=EXCLUDED.category,path=EXCLUDED.path,observed_at=now()', (part['name'], part['category'], document(part['path'])))
        conn.execute('INSERT INTO board_snapshots(id,name,category,members) VALUES(%s,%s,%s,%s)', (str(uuid.uuid4()), part['name'], part['category'], document(members)))

    def boards(self, search='', category='', limit=50, offset=0):
        limit, offset = int(limit), int(offset)
        if category not in ('', 'industry', 'concept') or not 1 <= limit <= 200 or offset < 0:
            raise ValueError('无效板块筛选或分页参数')
        query = "FROM catalog_sectors c WHERE category IN ('industry','concept') AND name ILIKE %s AND (%s='' OR category=%s)"
        args = ('%' + search + '%', category, category)
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            total = conn.execute('SELECT count(*) AS n ' + query, args).fetchone()['n']
            rows = conn.execute('SELECT name,category,path,observed_at ' + query + ' ORDER BY name LIMIT %s OFFSET %s', args + (limit, offset)).fetchall()
        return dict(rows=rows, total=total, next_offset=offset + len(rows) if offset + len(rows) < total else None)

    def board(self, name):
        row = self.query('SELECT * FROM board_snapshots WHERE name=%s ORDER BY observed_at DESC LIMIT 1', (name,), one=True)
        if not row:
            raise ValueError('板块尚未同步成员')
        return row

    def snapshot(self, code, sector, name, members):
        identifier = str(uuid.uuid4())
        members = codes(members)
        with self.connect() as conn:
            conn.execute('INSERT INTO index_mapping(code,sector,name) VALUES(%s,%s,%s) ON CONFLICT(code) DO UPDATE SET sector=EXCLUDED.sector,name=EXCLUDED.name,updated_at=now()', (codes([code])[0], sector, name))
            conn.execute('INSERT INTO constituent_snapshots(id,index_code,members) VALUES(%s,%s,%s)', (identifier, code, document(members)))
        return self.query('SELECT * FROM constituent_snapshots WHERE id=%s', (identifier,), one=True)

    def create_dataset(self, payload):
        source = provider_name(payload.get('source', 'qmt'))
        members = codes(payload['members'], source)
        selected = periods(payload.get('periods', ['1d']), source)
        name = str(payload.get('name', '')).strip()
        if not name or len(name) > 100:
            raise ValueError('数据集名称需要 1 至 100 个字符')
        if not isinstance(payload.get('scheduled', False), bool):
            raise ValueError('scheduled 必须为布尔值')
        snapshot = payload.get('snapshot_id')
        index_code = payload.get('index_code')
        board_name = payload.get('board_name')
        board_snapshot = payload.get('board_snapshot_id')
        if board_name:
            row = self.query('SELECT * FROM board_snapshots WHERE id=%s AND name=%s', (board_snapshot, board_name), one=True)
            if index_code or not row or row['members'] != members:
                raise ValueError('数据集成员必须匹配指定板块快照')
        if index_code:
            row = self.query('SELECT * FROM constituent_snapshots WHERE id=%s AND index_code=%s', (snapshot, index_code), one=True)
            if not row or row['members'] != members:
                raise ValueError('数据集成员必须匹配指定成分快照')
        schedule_time = payload.get('schedule_time') or ('19:00' if source == 'tushare' else '17:00')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule_time):
            raise ValueError('更新时间需要HH:MM格式')
        if source == 'tushare':
            found = self.query('SELECT code,subtype FROM securities WHERE source=%s AND code=ANY(%s)', (source,members))
            if len(found) != len(members) or index_code or board_name:
                raise ValueError('请先同步并选择Tushare期货目录')
            if set(selected) & set(MINUTE_PERIODS) and any(row['subtype'] != 'contract' for row in found):
                raise ValueError('分钟数据集只能包含具体月份合约')
        return self.query('INSERT INTO datasets(id,name,members,periods,index_code,snapshot_id,scheduled,board_name,board_snapshot_id,source,account_id,endpoint,schedule_time) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                          (str(uuid.uuid4()), name, document(members), document(selected), index_code, snapshot, bool(payload.get('scheduled')), board_name, board_snapshot,source,payload.get('account_id'),payload.get('endpoint'),schedule_time), one=True)

    def create_job(self, kind, payload, schedule_key=None):
        return self.query('INSERT INTO jobs(id,kind,payload,schedule_key) VALUES(%s,%s,%s,%s) ON CONFLICT(schedule_key) DO UPDATE SET schedule_key=EXCLUDED.schedule_key RETURNING *',
                          (str(uuid.uuid4()), kind, document(payload), schedule_key), one=True)

    def create_jobs(self, kind, payloads):
        if not payloads or len(payloads) > 1000 or sum(len(p.get('chunks', [])) for p in payloads) > 100000:
            raise ValueError('批量范围过大：最多1000个任务或100000个分块，请缩小范围')
        with self.connect() as conn:
            return [conn.execute('INSERT INTO jobs(id,kind,payload) VALUES(%s,%s,%s) RETURNING *',
                                 (str(uuid.uuid4()), kind, document(payload))).fetchone() for payload in payloads]

    def job(self, identifier):
        row = self.query('SELECT * FROM jobs WHERE id=%s', (identifier,), one=True)
        if not row:
            raise ValueError('任务不存在')
        if row['kind'] == 'download' and 'rows' not in row['result']:
            row['result']['rows'] = self.query('SELECT coalesce(sum(row_count),0) AS n FROM coverage WHERE job_id=%s', (identifier,), one=True)['n']
        return row

    def jobs_page(self, payload):
        states = filter_values(payload.get('states', []), (*STATES, 'completed'), '任务状态')
        states = ['succeeded' if state == 'completed' else state for state in states]
        kinds = filter_values(payload.get('kinds', []), ('download','export','catalog'), '任务类型')
        sources = filter_values(payload.get('sources', []), ('qmt','tushare'), '任务来源')
        limit, offset = int(payload.get('limit', 50)), int(payload.get('offset', 0))
        start, end = day(payload.get('start') or '1990-01-01'), day(payload.get('end') or '2100-01-01')
        if not 1 <= limit <= 200 or offset < 0 or start > end:
            raise ValueError('无效任务查询日期或分页')
        where = "FROM jobs WHERE (%s OR state=ANY(%s)) AND (%s OR kind=ANY(%s)) AND (%s OR coalesce(payload->>'source','qmt')=ANY(%s)) AND created_at::date BETWEEN %s AND %s AND (id::text ILIKE %s OR coalesce(error,'') ILIKE %s)"
        search = '%'+str(payload.get('search',''))+'%'
        args = (not states,states,not kinds,kinds,not sources,sources,start,end,search,search)
        fields = "id,kind,state,parent_id,error_code,action,run_number,payload-'chunks' AS payload,jsonb_array_length(coalesce(payload->'chunks','[]'::jsonb)) AS total_chunks,checkpoint,attempts,cancel_requested,CASE WHEN kind='download' THEN jsonb_build_object('rows',(SELECT coalesce(sum(row_count),0) FROM coverage WHERE job_id=jobs.id)) || (result-'coverage') ELSE result-'coverage' END AS result,error,created_at,updated_at"
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            total = conn.execute('SELECT count(*) AS n '+where,args).fetchone()['n']
            rows = conn.execute('SELECT '+fields+' '+where+' ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s',(*args,limit,offset)).fetchall()
        return dict(rows=rows,total=total,offset=offset,next_offset=offset+len(rows) if offset+len(rows)<total else None)

    def update_job(self, identifier, **changes):
        allowed = {'state','checkpoint','attempts','cancel_requested','result','error','payload','error_code','action','run_number'}
        if not changes or set(changes) - allowed:
            raise ValueError('无效任务字段')
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in changes)
        args = [document(value) if key in ('result','payload') else value for key, value in changes.items()]
        self.query(sql.SQL('UPDATE jobs SET {},updated_at=now() WHERE id=%s').format(assignments), args + [identifier])

    def write_chunk(self, job_id, part, rows, gaps, checkpoint, factor=None):
        source = provider_name(part.get('source', 'qmt'))
        with self.connect() as conn:
            stats = {'read':len(rows),'inserted':0,'updated':0,'unchanged_or_older':0}
            if rows:
                existing = conn.execute('SELECT count(*) AS n FROM bars WHERE source=%s AND code=%s AND period=%s AND time=ANY(%s)',(source,part['code'],part['period'],[row['time'] for row in rows])).fetchone()['n']
                with conn.cursor() as cursor:
                    fields = ('code', 'period', 'time', *FIELDS, 'trading_day', 'source', 'normalization_version', 'as_of_date', 'source_fields')
                    columns = sql.SQL(',').join(map(sql.Identifier, fields))
                    placeholders = sql.SQL(',').join(sql.Placeholder(field) for field in fields)
                    updates = sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(field), sql.Identifier(field)) for field in (*FIELDS, 'trading_day','as_of_date','source_fields','normalization_version'))
                    changed_fields = (*FIELDS,'trading_day','as_of_date','source_fields','normalization_version')
                    old_values = sql.SQL(',').join(sql.SQL('bars.{}').format(sql.Identifier(field)) for field in changed_fields)
                    new_values = sql.SQL(',').join(sql.SQL('EXCLUDED.{}').format(sql.Identifier(field)) for field in changed_fields)
                    statement = sql.SQL('INSERT INTO bars({}) VALUES({}) ON CONFLICT(instrument_id,source,period,time) DO UPDATE SET {},updated_at=now() WHERE (EXCLUDED.as_of_date IS NULL OR bars.as_of_date IS NULL OR EXCLUDED.as_of_date >= bars.as_of_date) AND ROW({}) IS DISTINCT FROM ROW({})').format(columns, placeholders, updates,old_values,new_values)
                    cursor.executemany(statement, [dict({field: row.get(field) for field in fields}, source=source, source_fields=document(row.get('source_fields',{})), normalization_version='tushare-futures-v2' if part['period'] in AGGREGATE_PERIODS else 'tushare-futures-v1' if source == 'tushare' else 'qmt-raw-v1') for row in rows])
                    stats.update(inserted=len(rows)-existing,updated=cursor.rowcount-(len(rows)-existing),unchanged_or_older=len(rows)-cursor.rowcount)
            if factor is not None:
                conn.execute('INSERT INTO factors(code,raw) VALUES(%s,%s) ON CONFLICT(code) DO UPDATE SET raw=EXCLUDED.raw,observed_at=now()', (part['code'], document(factor)))
            self.write_coverage(conn,job_id,part,len(rows),gaps,checkpoint,rows[0]['time'] if rows else None,rows[-1]['time'] if rows else None)
            conn.execute('UPDATE job_units SET stats=%s WHERE job_id=%s AND unit_index=%s',(document(stats),job_id,checkpoint-1))
            self.event(job_id,'BARS_WRITE_STATS','行情入库统计',unit_index=checkpoint-1,context=stats,conn=conn)

    def write_coverage(self, conn, job_id, part, count, gaps, checkpoint, actual_start=None, actual_end=None):
        gaps = classify_gaps(gaps)
        conn.execute('INSERT INTO coverage(job_id,code,period,requested_start,requested_end,actual_start,actual_end,row_count,gaps) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,code,period,requested_start) DO UPDATE SET actual_start=EXCLUDED.actual_start,actual_end=EXCLUDED.actual_end,row_count=EXCLUDED.row_count,gaps=EXCLUDED.gaps',
                     (job_id,part['code'],part['period'],part['start'],part['end'],actual_start,actual_end,count,document(gaps)))
        conn.execute("UPDATE jobs SET checkpoint=%s,attempts=0,result=result || jsonb_build_object('rows',(SELECT coalesce(sum(row_count),0) FROM coverage WHERE job_id=%s)),updated_at=now() WHERE id=%s", (checkpoint,job_id,job_id))
        self.write_unit(job_id, checkpoint-1, part, 'succeeded', gaps, count, conn=conn)

    def event(self, job_id, code, message, level='info', unit_index=None, context=None, sample=None, conn=None):
        if conn is None:
            with self.connect() as connection:
                return self.event(job_id, code, message, level, unit_index, context, sample, connection)
        conn.execute('INSERT INTO job_events(job_id,run_number,unit_index,level,code,message,context,sample) VALUES(%s,coalesce((SELECT run_number FROM jobs WHERE id=%s),0),%s,%s,%s,%s,%s,%s)',
                     (job_id,job_id,unit_index,level,code,redact(message),document(redact(context or {})),document(redact(sample)) if sample is not None else None))

    def write_unit(self, job_id, index, part, state, issues, count=0, error_code=None, retryable=None, conn=None, advance=True, sample=None):
        if conn is None:
            with self.connect() as connection:
                return self.write_unit(job_id,index,part,state,issues,count,error_code,retryable,connection,advance,sample)
        issues = classify_gaps(issues)
        retryable = any(item.get('retryable') for item in issues) if retryable is None else retryable
        conn.execute('INSERT INTO job_units(job_id,unit_index,request,state,quality_state,row_count,issues,error_code,retryable) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,unit_index) DO UPDATE SET request=EXCLUDED.request,state=EXCLUDED.state,quality_state=EXCLUDED.quality_state,row_count=EXCLUDED.row_count,issues=EXCLUDED.issues,error_code=EXCLUDED.error_code,retryable=EXCLUDED.retryable,updated_at=now()',
                     (job_id,index,document(part),state,quality(issues),count,document(issues),error_code,retryable))
        if advance:
            conn.execute('UPDATE jobs SET checkpoint=greatest(checkpoint,%s),attempts=0,updated_at=now() WHERE id=%s', (index+1,job_id))
        self.event(job_id,error_code or 'UNIT_COMMITTED','分块已提交' if state == 'succeeded' else '分块未入库',
                   'warning' if issues else 'info',index,dict(request=part,rows=count,quality_state=quality(issues),issues=issues),sample,conn)

    def units_page(self, identifier, limit=50, offset=0):
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError('无效分块分页参数')
        rows = self.query('SELECT * FROM job_units WHERE job_id=%s ORDER BY unit_index LIMIT %s OFFSET %s', (identifier,limit+1,offset))
        return dict(rows=rows[:limit],next_offset=offset+limit if len(rows)>limit else None,offset=offset)

    def events_page(self, payload):
        limit, before = int(payload.get('limit',50)), int(payload.get('before') or 9223372036854775807)
        if not 1 <= limit <= 200 or before < 1:
            raise ValueError('无效日志分页参数')
        levels = filter_values(payload.get('levels',[]), ('info','warning','error'), '日志级别')
        sources = filter_values(payload.get('sources',[]), ('qmt','tushare'), '日志来源')
        job_id = str(payload.get('job_id') or '')
        code = str(payload.get('code') or '')
        start, end = day(payload.get('start') or '1990-01-01'), day(payload.get('end') or '2100-01-01')
        if start > end:
            raise ValueError('日志开始日期不得晚于结束日期')
        rows = self.query("SELECT e.*,coalesce(j.payload->>'source','qmt') AS source FROM job_events e LEFT JOIN jobs j ON j.id=e.job_id WHERE e.id<%s AND (%s='' OR e.job_id::text=%s) AND (%s OR e.level=ANY(%s)) AND (%s OR coalesce(j.payload->>'source','qmt')=ANY(%s)) AND (%s='' OR e.code=%s) AND e.created_at::date BETWEEN %s AND %s ORDER BY e.id DESC LIMIT %s", (before,job_id,job_id,not levels,levels,not sources,sources,code,code,start,end,limit+1))
        return dict(rows=rows[:limit],next_before=rows[limit-1]['id'] if len(rows)>limit else None)

    def retry_job(self, identifier, unit_indices=None, automatic=False):
        with self.connect() as conn:
            job = conn.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE', (identifier,)).fetchone()
            if not job or job['state'] not in ('failed','cancelled','partial','blocked'):
                raise ValueError('任务状态不允许重试')
            payload = dict(job['payload'])
            parts = payload.get('chunks', [])
            units = {row['unit_index']:row for row in conn.execute('SELECT * FROM job_units WHERE job_id=%s', (identifier,))}
            family = conn.execute("WITH RECURSIVE family AS (SELECT id FROM jobs WHERE id=%s UNION ALL SELECT j.id FROM jobs j JOIN family f ON j.parent_id=f.id) SELECT DISTINCT ON (u.request-'source') u.* FROM job_units u JOIN family f ON f.id=u.job_id ORDER BY u.request-'source',u.updated_at DESC",(identifier,)).fetchall()
            request_key = lambda part: json.dumps({key:value for key,value in part.items() if key!='source'},sort_keys=True,default=json_default)
            latest = {request_key(row['request']):row for row in family}
            if unit_indices is not None and (not isinstance(unit_indices,list) or not unit_indices or any(type(index) is not int or index < 0 or index >= len(parts) for index in unit_indices)):
                raise ValueError('无效的重试分块选择')
            if parts:
                selected = []
                coverage = list(conn.execute('SELECT * FROM coverage WHERE job_id=%s', (identifier,)))
                missing_catalog = set(job['result'].get('missing',[]))
                for index, part in enumerate(parts):
                    if unit_indices is not None and index not in unit_indices:
                        continue
                    unit = latest.get(request_key(part),units.get(index))
                    if unit:
                        needed = unit['state'] != 'succeeded' or unit['retryable']
                        if automatic and not unit['retryable']:
                            needed = False
                    else:
                        old = next((row for row in coverage if row['code']==part.get('code') and row['period']==part.get('period') and str(row['requested_start'])==part.get('start')),None)
                        needed = index >= job['checkpoint'] or bool(old and any(gap.get('retryable') for gap in classify_gaps(old['gaps']))) or part.get('code') in missing_catalog or 'board:'+str(part.get('name')) in missing_catalog
                    if needed:
                        selected.append(part)
                if not selected:
                    raise ValueError('没有可重试分块；交易时段或夜盘归属待核验不能靠重复下载解决')
                payload['chunks'] = selected
            existing = conn.execute("SELECT * FROM jobs WHERE parent_id=%s AND state IN ('queued','running','retrying') ORDER BY created_at LIMIT 1", (identifier,)).fetchone()
            if existing:
                if existing['payload'].get('chunks') != payload.get('chunks'):
                    raise ValueError('已有不同范围的重试任务，请等待完成后再选择剩余分块')
                return existing
            if automatic:
                payload['repair_attempt'] = int(payload.get('repair_attempt',0)) + 1
            payload['retry_of'] = str(identifier)
            row = conn.execute('INSERT INTO jobs(id,kind,payload,parent_id) VALUES(%s,%s,%s,%s) RETURNING *', (str(uuid.uuid4()),job['kind'],document(payload),identifier)).fetchone()
            self.event(row['id'],'RETRY_CREATED','已创建关联重试任务',context={'parent_id':str(identifier),'chunks':len(payload.get('chunks',[]))},conn=conn)
            return row

    def housekeeping(self, event_days=90, sample_days=30):
        with self.connect() as conn:
            conn.execute("UPDATE job_events SET sample=NULL WHERE sample IS NOT NULL AND created_at < now() - %s * interval '1 day'", (sample_days,))
            conn.execute("DELETE FROM job_events WHERE created_at < now() - %s * interval '1 day'", (event_days,))

    def operations_health(self, now=None):
        from .freshness import freshness_page
        latest = "WITH latest AS (SELECT DISTINCT ON (coalesce(j.payload->>'source','qmt'),u.request-'source') u.*,coalesce(j.payload->>'source','qmt') AS source FROM job_units u JOIN jobs j ON j.id=u.job_id ORDER BY coalesce(j.payload->>'source','qmt'),u.request-'source',u.updated_at DESC) "
        return dict(
            database=self.health(),
            database_bytes=self.query('SELECT pg_database_size(current_database()) AS bytes',one=True)['bytes'],
            queues=self.query("SELECT coalesce(payload->>'source','qmt') AS source,state,count(*) AS count,min(created_at) AS oldest FROM jobs WHERE state IN ('queued','running','retrying') GROUP BY 1,2 ORDER BY 1,2"),
            quality=self.query(latest+"SELECT source,quality_state,count(*) AS count FROM latest GROUP BY 1,2 ORDER BY 1,2"),
            attention=self.query(latest+"SELECT id,state,error_code,error,action,updated_at,payload->>'source' AS source FROM jobs j WHERE state IN ('blocked','failed','partial') AND (EXISTS(SELECT 1 FROM latest u WHERE u.job_id=j.id AND (u.state!='succeeded' OR u.quality_state NOT IN ('verified','not_applicable'))) OR NOT EXISTS(SELECT 1 FROM job_units u WHERE u.job_id=j.id)) ORDER BY updated_at DESC LIMIT 50"),
            freshness=self.query('SELECT source,period,max(time) AS last_bar,max(updated_at) AS last_write FROM bars GROUP BY source,period ORDER BY source,period'),
            scheduled_freshness=freshness_page(self, {}, now),
            maintenance=self.query('SELECT id,name,source,enabled,last_date,last_error,updated_at FROM maintenance_plans ORDER BY name'),
            database_free_space={'state':'unverified','reason':'远端或容器数据库磁盘不可由应用本地磁盘推断'})

    def history(self, code, period='1d', start='1990-01-01', end='2100-01-01', limit=500, offset=0, conn=None, source='qmt'):
        code = codes([code], source)[0]
        period = periods([period],source)[0]
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 5000 or offset < 0:
            raise ValueError('分页 limit 需要 1 至 5000，offset 不得为负')
        aggregate = period in AGGREGATE_PERIODS
        extra = ',as_of_date,source_fields' if aggregate else ''
        statement = "SELECT code,period,time,open,high,low,close,volume,amount,open_interest,settlement,previous_settlement,trading_day,source" + extra + " FROM bars WHERE source=%s AND code=%s AND period=%s AND coalesce(trading_day,time::date) BETWEEN %s::date AND %s::date ORDER BY time LIMIT %s OFFSET %s"
        args = (source, code, period, period_label(start,period), period_label(end,period), limit, offset)
        rows = conn.execute(statement, args).fetchall() if conn else self.query(statement, args)
        return {'rows': rows, 'offset': offset, 'limit': limit, 'next_offset': offset + len(rows) if len(rows) == limit else None,
                'source': 'postgresql', 'provider': source, 'normalization_version': 'tushare-futures-v2' if aggregate else 'tushare-futures-v1' if source == 'tushare' else 'qmt-raw-v1', 'adjustment': 'none', 'timezone': 'Asia/Shanghai',
                'date_basis': '按日期所在周/月的周期标签查询；time为周五/月末标签，as_of_date为上游计算截至日期，不是历史时点快照；source_fields保留上游原始字段及万元金额' if aggregate else '日线按交易日；分钟按上海自然时间查询，未提供交易日时夜盘归属待核验' if source == 'tushare' else '优先使用终端 trading_day；未提供时按行情时间的上海自然日查询，夜盘归属待核验',
                'units': {'price': '合约报价单位（见合约资料）', 'volume': '手', 'amount': '元', 'open_interest': '手'} if source == 'tushare' else {'price': 'QMT 原始报价', 'volume': 'QMT 原始成交量单位（未换算）', 'amount': 'QMT 原始成交额单位（未换算）', 'open_interest': 'QMT 原始持仓量单位（未换算）'}}

    def history_many(self, payload):
        source = provider_name(payload.get('source', 'qmt'))
        members, selected = codes(payload['members'], source), periods(payload['periods'], source)
        start, end = day(payload['start']), day(payload['end'])
        limit, offset = int(payload.get('limit', 300)), int(payload.get('offset', 0))
        if start > end or not 1 <= limit <= 5000 or offset < 0:
            raise ValueError('无效历史查询范围或分页')
        parts, args = [], [source, members]
        for period in selected:
            parts.append('(period=%s AND coalesce(trading_day,time::date) BETWEEN %s AND %s)')
            args.extend([period, period_label(start, period), period_label(end, period)])
        fields = ('code','period','time',*FIELDS,'trading_day','source','as_of_date','source_fields','normalization_version')
        rows = self.query('SELECT '+','.join(fields)+' FROM bars WHERE source=%s AND code=ANY(%s) AND ('+' OR '.join(parts)+') ORDER BY code,period,time LIMIT %s OFFSET %s', (*args, limit+1, offset))
        return dict(rows=rows[:limit], next_offset=offset+limit if len(rows)>limit else None, offset=offset,
                    source='postgresql', provider=source, timezone='Asia/Shanghai', adjustment='none',
                    normalization_versions={period: 'tushare-futures-v2' if period in AGGREGATE_PERIODS else 'tushare-futures-v1' if source=='tushare' else 'qmt-raw-v1' for period in selected},
                    units={'price':'合约报价单位（见合约资料）','volume':'手','amount':'元','open_interest':'手'} if source=='tushare' else {'price':'QMT 原始报价','volume':'QMT 原始成交量单位（未换算）','amount':'QMT 原始成交额单位（未换算）','open_interest':'QMT 原始持仓量单位（未换算）'},
                    date_basis='日线按交易日；分钟按上海自然时间；周/月按周期标签并保留计算截至日，不混合合约或周期')
