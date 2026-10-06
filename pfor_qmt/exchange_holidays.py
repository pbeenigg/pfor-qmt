"""Official closure notices, kept separate from QMT and Tushare calendars."""
import hashlib
import re
import time
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from lxml import html

from .data import SHANGHAI, day, filter_values
from .storage import job_summary
from .reliability import issue
from .tasks import Worker

PARSER_VERSION = 'exchange-notice-v1'
EXCHANGES = {
    'SHFE': ('上海期货交易所', 'www.shfe.com.cn', 'SF', 'https://www.shfe.com.cn/publicnotice/notice/'),
    'INE': ('上海国际能源交易中心', 'www.ine.cn', 'INE', 'https://www.ine.cn/publicnotice/notice/'),
    'DCE': ('大连商品交易所', 'www.dce.com.cn', 'DF', 'http://www.dce.com.cn/dalianshangpin/yw/fw/jystz/ywtz/index.html'),
    'CZCE': ('郑州商品交易所', 'www.czce.com.cn', 'ZF', 'https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/'),
    'CFFEX': ('中国金融期货交易所', 'www.cffex.com.cn', 'IF', 'http://www.cffex.com.cn/jystz/'),
    'GFEX': ('广州期货交易所', 'www.gfex.com.cn', 'GF', 'http://www.gfex.com.cn/gfex/tzts/list_yw.shtml'),
}
ANNUAL = {key: 'https://' + EXCHANGES[key][1] + '/services/calenderandholidays/holiday/' for key in ('SHFE', 'INE')}
DATE = r'(?:(?P<year>20\d{2})年)?(?:(?P<month>\d{1,2})月)?(?P<day>\d{1,2})日'
DATE_TOKEN = re.compile(DATE)
DATE_EXPR = re.compile(r'(?:20\d{2}年)?\d{1,2}月\d{1,2}日(?:(?:至|到|、|和|及)(?:20\d{2}年)?(?:\d{1,2}月)?\d{1,2}日)*')
ACTION = re.compile(r'不进行夜盘(?:交易)?|暂停夜盘(?:交易)?|恢复夜盘(?:交易)?|休市(?!安排|期间)|照常开市(?:交易)?|恢复交易')


class NoticeError(ValueError):
    pass


class ForeignNotice(NoticeError):
    pass


def scope(params):
    exchanges = filter_values(params.get('exchanges', list(EXCHANGES)), EXCHANGES, '交易所')
    years = params.get('years', [datetime.now(SHANGHAI).year])
    if not exchanges or not isinstance(years, list) or not 1 <= len(years) <= 5 or any(type(y) is not int or not 2014 <= y <= 2100 for y in years):
        raise ValueError('请选择交易所及1至5个年度（2014至2100）')
    years = sorted(set(years))
    return dict(source='exchange', resource='exchange_holidays', exchanges=exchanges, years=years,
                chunks=[dict(exchange=e, year=y, period='exchange_holidays') for e in exchanges for y in years])


def official_url(url, exchange):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.hostname != EXCHANGES[exchange][1] or parsed.port not in (None, 80, 443) or parsed.username or parsed.password:
        raise NoticeError('公告地址或重定向不属于所选交易所官方站点')
    return url


def fetch(url, exchange, check=lambda: None):
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            official_url(newurl, exchange)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    official_url(url, exchange)
    for attempt in range(3):
        check()
        try:
            with build_opener(Redirect()).open(Request(url, headers={'User-Agent': 'Mozilla/5.0 pfor-qmt'}), timeout=10) as response:
                official_url(response.url, exchange)
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise NoticeError('公告页面超过2MB读取上限')
                # Prefer the declared HTML encoding; several exchanges omit HTTP charset.
                encoding = re.search(br'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
                charset = encoding.group(1).decode('ascii') if encoding else response.headers.get_content_charset() or 'utf-8'
                try:
                    text = raw.decode(charset)
                except UnicodeDecodeError:
                    text = raw.decode('gb18030')
                if any(s in text for s in ('solveChallenge', 'WEB 应用防火墙', 'Access Denied', '访问被拦截')):
                    raise NoticeError('官网返回访问验证或防火墙页面，未读到公告')
                return text
        except HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise NoticeError(f'官网HTTP {error.code}，缓存已保留') from None
        except (URLError, TimeoutError, OSError) as error:
            if attempt == 2:
                raise NoticeError('官网网络请求失败，缓存已保留：' + str(error)[:200]) from None
        check()
        time.sleep(attempt + 1)


def document(text, url, exchange):
    tree = html.fromstring(text)
    for node in tree.xpath('//script|//style|//noscript'):
        node.drop_tree()
    titles = tree.xpath('//*[contains(@class,"article_title") or contains(@class,"table_select_title") or contains(@class,"title_new") or @class="InfoTitle"]')
    title = next((n.text_content().strip() for n in titles if re.search(r'20\d{2}年', n.text_content())), '')
    if not title:
        title = ''.join(tree.xpath('//title/text()')).strip().split('|')[0].strip()
    title = next((line.strip() for line in title.splitlines() if re.search(r'20\d{2}年',line)), title)
    nodes = tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," TRS_Editor ") or @class="InfoContent" or @id="content" or @class="news_content"]')
    body = next((n.text_content().strip() for n in nodes if len(n.text_content().strip()) > 30), '')
    if not body or not any(word in title + body for word in ('休市', '夜盘', '开市')):
        raise NoticeError('未识别到休市公告正文，未据空页面更新缓存')
    if exchange == 'SHFE' and re.search(r'上能发|上能公告', body[:400]):
        raise ForeignNotice('上期所页面转发能源中心公告，不能归入上期所安排')
    if exchange == 'INE' and re.search(r'上期发', body[:150]):
        raise ForeignNotice('公告发布机构与所选交易所不一致')
    published = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日\s*$', body)
    published_at = date(*map(int, published.groups())) if published else None
    if published_at is None:
        meta = tree.xpath('//meta[@name="PubDate" or @name="publishdate" or @name="pubdate"]/@content')
        if meta and re.match(r'20\d{2}-\d{2}-\d{2}', meta[0]): published_at = day(meta[0][:10])
    return dict(url=url, title=title, body=body, published_at=published_at)


def parse_notice(title, body):
    years = re.findall(r'(20\d{2})年', title)
    if len(set(years)) != 1:
        return [], 'pending_verification', '标题未明确唯一安排年度'
    year = int(years[0])
    text = re.sub(r'《[^》]*》', '', body)
    text = re.sub(r'[（(][^）)]*[）)]', '', text)
    text = re.sub(r'\s+', '', text)
    events, unknown = [], []
    for sentence in re.split(r'[。；;\n]', text):
        last_date = None
        for clause in re.split(r'[，,]', sentence):
            expression = list(DATE_EXPR.finditer(clause))
            actions = list(ACTION.finditer(clause))
            if not actions:
                if expression:
                    token = DATE_TOKEN.match(expression[-1].group())
                    try: last_date = date(int(token['year'] or year), int(token['month']), int(token['day']))
                    except (ValueError, TypeError): last_date = None
                continue
            for action in actions:
                candidates = [m for m in expression if m.end() <= action.start()]
                selected = candidates[-1] if candidates else None
                phrase = action.group()
                kind = 'night_closed' if '夜盘' in phrase and ('不进行' in phrase or '暂停' in phrase) else 'night_open' if '夜盘' in phrase else 'closed' if phrase == '休市' else 'open'
                # "起" states the first reopening date, not all subsequent dates.
                try:
                    if re.search(r'部分|品种|仿真|测试|另行|可能|拟', clause): raise ValueError()
                    if selected:
                        suffix = clause[selected.end():action.start()]
                        if len(suffix) > 30 or re.search(r'品种|合约|部分|可能|拟|另行', suffix): raise ValueError()
                        dates, month, current_year = [], None, year
                        for token in DATE_TOKEN.finditer(selected.group()):
                            month = int(token['month'] or month)
                            current_year = int(token['year'] or current_year)
                            dates.append(date(current_year, month, int(token['day'])))
                        pairs = [(dates[0], dates[-1])] if re.search('至|到', selected.group()) else [(d, d) for d in dates]
                    elif kind.startswith('night_') and re.match(r'当晚|当天晚上', clause) and last_date:
                        pairs = [(last_date, last_date)]
                    else: raise ValueError()
                    for first, last in pairs:
                        if first > last or (last-first).days > 31: raise ValueError()
                        events.append(dict(start_day=first, end_day=last, kind=kind, evidence=clause))
                        last_date = last
                except (ValueError, TypeError):
                    unknown.append(clause)
    if not events and not unknown and re.search(r'休市期间.*(?:风险|保证金|涨跌停板)',text):
        return [], 'parsed', '风险通知未陈述明确交易时间安排，仅保存原文，不用于日期判定'
    if not events or unknown:
        return events, 'pending_verification', '未完整识别日期安排：' + '；'.join(unknown[:5]) if unknown else '未识别明确日期安排'
    if '仅做参考' in body or '仅供参考' in body:
        return events, 'pending_verification', '页面标明仅供参考，需正式公告确认'
    return events, 'parsed', f'已提取{len(events)}项明确安排；未列日期保持未知'


def save_notice(store, exchange, notice, conn=None):
    if conn is None:
        with store.connect() as connection: return save_notice(store, exchange, notice, connection)
    events, state, detail = parse_notice(notice['title'], notice['body'])
    digest = hashlib.sha256((notice['title'] + str(notice['published_at']) + notice['body']).encode('utf-8')).hexdigest()
    conn.execute('UPDATE exchange_notices SET is_current=false WHERE exchange=%s AND url=%s AND content_hash!=%s', (exchange, notice['url'], digest))
    record = conn.execute('INSERT INTO exchange_notices(exchange,url,content_hash,title,body,published_at,parse_state,parse_detail,parser_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(exchange,url,content_hash) DO UPDATE SET is_current=true,checked_at=now(),parse_state=EXCLUDED.parse_state,parse_detail=EXCLUDED.parse_detail,parser_version=EXCLUDED.parser_version RETURNING *',
                          (exchange, notice['url'], digest, notice['title'], notice['body'], notice['published_at'], state, detail, PARSER_VERSION)).fetchone()
    conn.execute('DELETE FROM exchange_holiday_events WHERE notice_id=%s', (record['id'],))
    for index, event in enumerate(events):
        conn.execute('INSERT INTO exchange_holiday_events(notice_id,ordinal,start_day,end_day,kind,evidence) VALUES(%s,%s,%s,%s,%s,%s)',
                     (record['id'], index, event['start_day'], event['end_day'], event['kind'], event['evidence']))
    return record, events


def sync_exchange(store, exchange, year, check=lambda: None, reader=None):
    reader = reader or fetch
    deadline = time.monotonic()+180
    def checked():
        check()
        if time.monotonic()>=deadline: raise NoticeError('本次公告同步达到180秒上限，缓存保留，请核对官网后重试')
    def read(url):
        checked()
        return reader(url,exchange,checked)
    urls = {r['url'] for r in store.query('SELECT DISTINCT n.url FROM exchange_notices n JOIN exchange_holiday_events e ON e.notice_id=n.id WHERE n.exchange=%s AND extract(year FROM e.start_day)=%s', (exchange, year))}
    failures = []
    if exchange in ANNUAL: urls.add(ANNUAL[exchange])
    index_url = EXCHANGES[exchange][3]
    # Discovery is bounded; it never certifies that an unmentioned day is open or closed.
    try:
        tree = html.fromstring(read(index_url))
        links = tree.xpath('//a[@href]')
        if not links: raise NoticeError('未识别官网公告列表，未据空页面确认同步成功')
        relevant = [a for a in links if re.search(r'休市|节期间|交易时间.*调整|临时.*交易|恢复.*交易', a.text_content())]
        for a in relevant:
            url = urljoin(index_url, a.get('href'))
            official_url(url, exchange)
            if re.search(fr'{year}年', a.text_content()) or re.search(fr'/{year}\d{{2}}', url): urls.add(url)
    except NoticeError as error: failures.append(str(error))
    if len(urls)>40:
        failures.append('公告发现超过40篇上限，本范围未继续请求，请缩小年度范围')
        urls=set()
    found, rows = 0, 0
    for url in sorted(urls):
        check()
        try:
            notice = document(read(url), url, exchange)
            check()
            record, events = save_notice(store, exchange, notice)
            if not re.search(fr'{year}年', notice['title']): continue
            found += 1
            rows += len(events)
            if record['parse_state'] != 'parsed': failures.append(record['parse_detail'])
        except ForeignNotice:
            continue
        except NoticeError as error: failures.append(str(error))
    if not found: failures.append('当前官网列表及已知公告未取得所选年度安排；未确认历史列表完整性')
    detail = '；'.join(dict.fromkeys(failures)) if failures else '已更新发现的官方公告；列表发现有40篇边界，不表示全年日历完整'
    state = 'partial' if found and failures else 'failed' if failures else 'succeeded'
    store.query('INSERT INTO exchange_holiday_sync(exchange,year,state,detail,notice_count,succeeded_at) VALUES(%s,%s,%s,%s,%s,CASE WHEN %s THEN now() END) ON CONFLICT(exchange,year) DO UPDATE SET attempted_at=now(),state=EXCLUDED.state,detail=EXCLUDED.detail,notice_count=EXCLUDED.notice_count,succeeded_at=CASE WHEN %s THEN now() ELSE exchange_holiday_sync.succeeded_at END',
                (exchange, year, state, detail, found, state == 'succeeded', state == 'succeeded'))
    return rows, [] if state == 'succeeded' else [issue('EXCHANGE_NOTICE_SYNC', 'pending_verification', detail, '查看官网与公告版本；可靠缓存继续保留，未确认日期不推断休市', False)], state


def evidence(store, market, start, end, conn=None):
    exchange = next((e for e, info in EXCHANGES.items() if info[2] == market), None)
    if not exchange: return []
    statement = "SELECT n.id AS notice_id,n.url,n.title,n.published_at,n.content_hash,n.parse_state,e.start_day,e.end_day,e.kind,e.evidence FROM exchange_notices n LEFT JOIN exchange_holiday_events e ON e.notice_id=n.id AND e.start_day<=%s AND e.end_day>=%s WHERE n.exchange=%s AND n.is_current AND (e.notice_id IS NOT NULL OR n.title ~ %s) ORDER BY n.id,e.ordinal"
    args = (day(end), day(start)-timedelta(days=1), exchange, str(day(start).year)+'年')
    return list(conn.execute(statement, args)) if conn else store.query(statement, args)


def closure_issue(part, calendar, records):
    first, last = day(part['start']), day(part['end'])
    if (last-first).days > 366 or not records or any(r['is_open'] and first <= r['day'] <= last for r in calendar): return None
    if any(r['parse_state'] != 'parsed' for r in records): return None
    def kinds(d): return {r['kind'] for r in records if r['start_day'] and r['start_day'] <= d <= r['end_day']}
    for i in range((last-first).days+1):
        states = kinds(first+timedelta(days=i))
        if 'closed' not in states or 'open' in states or 'night_open' in states: return None
    if part['period'] in ('1m', '5m') and not part['code'].endswith('.IF'):
        previous_day=first-timedelta(days=1)
        previous = kinds(previous_day)
        if 'night_open' in previous: return None
        if 'night_closed' not in previous and ('closed' not in previous or 'open' in previous or any(r['is_open'] and r['day']==previous_day for r in calendar)): return None
    used = [r for r in records if r['kind'] in ('closed', 'night_closed')]
    return issue('EXCHANGE_HOLIDAY_CLOSED', 'not_applicable', f'交易所公告确认{first}至{last}休市，空行情符合明确安排',
                 '本分块无需补数，继续后续范围；此结论不代表行情连接已经验证',
                 rule=PARSER_VERSION, evidence_origin='交易所官方公告', notices=[dict(notice_id=r['notice_id'], url=r['url'], title=r['title'], published_at=r['published_at'].isoformat() if r['published_at'] else None, content_hash=r['content_hash'], evidence=r['evidence']) for r in used])


def page(store, params):
    selected = scope(params)
    limit, offset = int(params.get('limit', 50)), int(params.get('offset', 0))
    if not 1 <= limit <= 200 or offset < 0: raise ValueError('无效公告分页')
    where = 'n.exchange=ANY(%s) AND (extract(year FROM e.start_day)::integer=ANY(%s) OR e.notice_id IS NULL AND n.title ~ %s)'
    args = (selected['exchanges'], selected['years'], '|'.join(str(y)+'年' for y in selected['years']))
    if not params.get('versions'): where += ' AND n.is_current'
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        total = conn.execute('SELECT count(*) AS n FROM exchange_notices n LEFT JOIN exchange_holiday_events e ON e.notice_id=n.id WHERE '+where, args).fetchone()['n']
        rows = conn.execute('SELECT n.id,n.exchange,n.url,n.title,n.published_at,n.checked_at,n.content_hash,n.is_current,n.parse_state,n.parse_detail,e.start_day,e.end_day,e.kind,e.evidence FROM exchange_notices n LEFT JOIN exchange_holiday_events e ON e.notice_id=n.id WHERE '+where+' ORDER BY n.exchange,e.start_day,n.id DESC,e.ordinal LIMIT %s OFFSET %s', (*args, limit, offset)).fetchall()
        status = conn.execute('SELECT * FROM exchange_holiday_sync WHERE exchange=ANY(%s) AND year=ANY(%s) ORDER BY exchange,year', args[:2]).fetchall()
    return dict(rows=rows, total=total, offset=offset, next_offset=offset+len(rows) if offset+len(rows)<total else None, status=status)


def verify(worker, job):
    def process(index, part):
        records=worker.store.query("SELECT n.id,n.title,n.body,n.parse_state FROM exchange_notices n WHERE n.exchange=%s AND n.is_current AND n.title ~ %s",(part['exchange'],str(part['year'])+'年'))
        valid=bool(records) and all(parse_notice(r['title'],r['body'])[1]=='parsed' and r['parse_state']=='parsed' for r in records)
        gaps=[] if valid else [issue('EXCHANGE_NOTICE_PARSE','pending_verification','本地公告缺失或日期安排尚未完整识别','核对原文与版本；不据空结果推断休市')]
        worker.check(job['id'])
        worker.store.write_unit(job['id'],index,part,'succeeded',gaps,len(records))
    worker.process_parts(job,process)
    result=worker.download_result(job['id'])
    result['rule_version']=PARSER_VERSION
    result['message']='只读核验本地公告解析；不证明官网发现范围完整，原文版本未改写'
    return result


class ExchangeWorker(Worker):
    def __init__(self, store, runtime, publish=None, options=None):
        super().__init__(store, runtime, publish, provider='exchange', options=options)

    def run(self):
        self.run_queue('download')

    def schedule(self, now=None):
        from .maintenance import enqueue, repair
        now = now or datetime.now(SHANGHAI)
        for plan in self.store.query("SELECT * FROM maintenance_plans WHERE source='exchange' AND enabled AND deleted_at IS NULL", ()):
            if not plan['last_date'] and now.time()<plan['schedule_time']: continue
            cutoff = now.date() if now.time() >= plan['schedule_time'] else now.date()-timedelta(days=1)
            if plan['last_date'] and plan['last_date'] >= cutoff: continue
            payload = dict(plan['payload'], maintenance_id=str(plan['id']))
            payload['chunks'] = scope(payload)['chunks']
            enqueue(self, plan, 'maintenance', payload, 'announcements', cutoff)
        repair(self, now)

    def download(self, job):
        def process(index, part):
            count, issues, state = sync_exchange(self.store, part['exchange'], part['year'], lambda: self.check(job['id']))
            self.check(job['id'])
            with self.store.connect() as conn:
                self.store.write_unit(job['id'], index, part, 'succeeded' if state != 'failed' else 'failed', issues, count, retryable=bool(issues), conn=conn)
        self.process_parts(job, process)
        units = self.store.query('SELECT state,quality_state,row_count FROM job_units WHERE job_id=%s', (job['id'],))
        count = sum(u['row_count'] for u in units)
        incomplete = len(units) < len(job['payload']['chunks']) or any(u['quality_state'] != 'verified' or u['state'] != 'succeeded' for u in units)
        return dict(state='partial' if count and incomplete else 'failed' if incomplete else 'succeeded', rows=count,
                    quality_summary=self.store.query('SELECT state,quality_state,count(*) AS count FROM job_units WHERE job_id=%s GROUP BY state,quality_state',(job['id'],)),rule_version=PARSER_VERSION,
                    message='公告安排已保存；未知日期不当休市' if not incomplete else '部分官网访问或解析未完成，已有缓存保留')


def dispatch(app, method, path, params):
    store = app.store
    if method == 'GET' and path == '/exchange-holidays/options':
        return dict(exchanges=[dict(id=e, name=v[0]) for e, v in EXCHANGES.items()], year=datetime.now(SHANGHAI).year)
    if method == 'POST' and path == '/exchange-holidays/query': return page(store, params)
    if method == 'POST' and path == '/exchange-holidays/sync': return job_summary(store.create_job('download', scope(params)))
    if method == 'POST' and path == '/exchange-holidays/jobs': return store.jobs_page(dict(params, sources=['exchange']))
    if method == 'GET' and path.startswith('/exchange-holidays/notices/'):
        record = store.query('SELECT * FROM exchange_notices WHERE id=%s', (int(path.rsplit('/', 1)[-1]),), one=True)
        if not record: raise ValueError('公告版本不存在')
        return dict(record, versions=store.query('SELECT id,content_hash,published_at,fetched_at,is_current,parse_state FROM exchange_notices WHERE exchange=%s AND url=%s ORDER BY fetched_at DESC,id DESC', (record['exchange'], record['url'])))
    if method == 'POST' and path == '/exchange-holidays/maintenance/query':
        from .analytics import configuration_page
        return configuration_page(store, 'maintenance', dict(params, sources=['exchange']))
    if method == 'POST' and path == '/exchange-holidays/maintenance':
        from .workspace import save_maintenance
        settings={key:params[key] for key in ('name','schedule_time','enabled') if key in params}
        return save_maintenance(app, None, dict(settings, payload=scope(params), kind='download'))
    if method == 'POST' and path.startswith('/exchange-holidays/maintenance/'):
        from .workspace import manage
        identifier=path.split('/')[3]
        if not store.query("SELECT 1 FROM maintenance_plans WHERE id=%s AND source='exchange'",(identifier,),one=True):
            raise ValueError('公告维护计划不存在')
        return manage(app, path.replace('/exchange-holidays', '/console', 1), params)
    raise LookupError('公告接口不存在')
