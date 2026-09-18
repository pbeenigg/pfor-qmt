"""Stable execution/quality vocabulary; no provider credentials in diagnostics."""
import re
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_loggers = {}

STATES = ('queued', 'running', 'retrying', 'succeeded', 'partial', 'failed', 'blocked', 'cancelled')
QUALITY_STATES = ('verified', 'pending_verification', 'not_published', 'not_applicable', 'missing', 'rejected', 'stale')
ACTIVE = ('queued', 'running', 'retrying')


class DataRejected(ValueError):
    def __init__(self, message, code='INVALID_BAR', sample=None):
        super().__init__(message)
        self.code, self.sample = code, sample


class SourceUnavailable(ValueError):
    pass


def fallback_log(runtime, provider, detail):
    path = Path(runtime) / ('worker-' + provider + '.jsonl')
    key = str(path.resolve())
    if key not in _loggers:
        path.parent.mkdir(parents=True,exist_ok=True)
        logger = logging.getLogger('pfor_qmt.worker.' + key)
        logger.propagate = False
        logger.setLevel(logging.WARNING)
        handler = RotatingFileHandler(path,maxBytes=2*1024*1024,backupCount=5,encoding='utf-8',delay=True)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        _loggers[key] = logger
    from datetime import datetime, timezone
    _loggers[key].warning(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(),source=provider,**redact(detail)),ensure_ascii=False))


def redact(value):
    if isinstance(value, dict):
        return {key: '[redacted]' if re.search(r'token|password|secret|api.?key|dsn|authorization', str(key), re.I) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(?:postgres(?:ql)?|https?)://[^\s]+', '[endpoint]', value)
        return re.sub(r'(?i)\b(token|password|api[_-]?key|authorization)\s*[:=]\s*\S+', r'\1=[redacted]', value)[:800]
    return value


def runtime_events(runtime):
    rows,truncated=[],False
    for provider in ('qmt','tushare'):
        path=Path(runtime)/('worker-'+provider+'.jsonl')
        if not path.exists(): continue
        try:
            with path.open('rb') as stream:
                stream.seek(0,2)
                size=stream.tell()
                truncated=truncated or size>65536
                stream.seek(max(0,size-65536))
                lines=stream.read().decode('utf-8',errors='replace').splitlines()
            for line in lines:
                try:
                    item=json.loads(line)
                    if isinstance(item,dict):
                        rows.append(redact({key:item.get(key) for key in ('time','source','lane','code','message','action')}))
                except ValueError:
                    continue
        except OSError:
            rows.append(dict(source=provider,code='LOG_READ_FAILED',message='运行日志暂不可读取'))
    rows.sort(key=lambda row:str(row.get('time') or ''),reverse=True)
    return dict(rows=rows[:100],truncated=truncated or len(rows)>100)


def issue(code, state, reason, action, retryable=False, **details):
    return dict(code=code, quality_state=state, reason=reason, action=action, retryable=retryable, **details)


def quality(issues):
    states = {item.get('quality_state', 'pending_verification') for item in issues}
    return next((state for state in ('rejected', 'missing', 'stale', 'pending_verification', 'not_published', 'not_applicable') if state in states), 'verified')


def failure(error):
    from .client import CfquantError
    from .tushare import SourceError
    import psycopg
    if isinstance(error, psycopg.Error):
        return dict(code='DATABASE_ERROR', message='数据库读写失败，本分块未提交；检查数据库与磁盘空间', action='恢复数据库后重试', scope='source', retryable=True, state='failed')
    if isinstance(error, SourceUnavailable):
        return dict(code='SOURCE_NOT_READY', message=redact(str(error)), action='检查行情服务器连接与日历，恢复后重试', scope='source', retryable=True, state='blocked')
    if isinstance(error, DataRejected):
        return dict(code=error.code, message=str(error), action='检查异常样本与上游数据，修正后定向重试', scope='unit', retryable=False, state='failed')
    category = error.category if isinstance(error, SourceError) else ''
    if isinstance(error, NotImplementedError) or isinstance(error, CfquantError) and error.remote_type in ('NotImplementedError', 'SignatureUnavailable'):
        category = 'unsupported'
    if category:
        scope = 'source' if category in ('authentication', 'configuration', 'rate_limit') else 'interface' if category in ('permission', 'unsupported') else 'unit'
        return dict(code='SOURCE_' + category.upper(), message=redact(str(error)), action={
            'permission':'确认该接口权限后重试，不自动重试', 'authentication':'更新账号认证后重试',
            'configuration':'核对任务固定账号与端点', 'unsupported':'改用支持的接口或周期',
            'rate_limit':'降低请求频率，待上游配额恢复后重试', 'incomplete':'缩小请求范围并检查接口返回上限',
        }.get(category, '核对接口参数与响应后重试'), scope=scope, retryable=False,
                    state='blocked' if scope != 'unit' else 'failed')
    from .tasks import network_error
    if network_error(error):
        return dict(code='NETWORK_ERROR', message='数据源连接失败，已用尽本次网络重试', action='恢复连接后重试未完成范围', scope='source', retryable=True, state='failed')
    return dict(code='INVALID_DATA' if isinstance(error, ValueError) else 'INTERNAL_ERROR',
                message=redact(str(error)) if isinstance(error, ValueError) else '内部处理失败：' + type(error).__name__,
                action='检查任务事件与请求范围后重试', scope='unit' if isinstance(error, ValueError) else 'source', retryable=False, state='failed')


def classify_gaps(gaps):
    result = []
    for gap in gaps:
        if 'quality_state' in gap:
            result.append(gap)
            continue
        reason = gap.get('reason', '')
        code, state, action, retry = 'COVERAGE_UNVERIFIED', 'pending_verification', '核对上游覆盖与发布规则', False
        if '周期尚未结束' in reason:
            code, state, action, retry = 'PERIOD_OPEN', 'not_published', '周期结束后更新', True
        elif '夜盘' in reason:
            code, action = 'TRADING_DAY_UNKNOWN', '需上游交易日字段或已核验交易时段；重复下载不能解决'
        elif '分钟内完整性' in reason:
            code, action = 'SESSION_UNVERIFIED', '需核验该合约交易时段；不按股票时段填补'
        elif '无行情' in reason:
            code, state, action, retry = 'BARS_MISSING', 'missing', '核对生命周期与停牌信息，定向补数', True
        elif '映射缺失' in reason:
            code, state, action, retry = 'MAPPING_MISSING', 'missing', '补充该日期主力映射', True
        elif '复权因子' in reason:
            code, action = 'FACTOR_UNAVAILABLE', '仅提供不复权数据，确认终端能力'
        elif '资料' in reason:
            code, action = 'REPORT_UNCONFIRMED', '确认该品种是否发布此资料；空响应不能证明缺失',
        result.append(issue(code, state, reason, action, retry, **{key:value for key,value in gap.items() if key != 'reason'}))
    return result
