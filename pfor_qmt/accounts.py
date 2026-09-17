"""Local Tushare account profiles; never serialize credentials publicly."""
import math
import os
import re
from urllib.parse import urlsplit

DEFAULTS = {'name': '', 'endpoint': 'https://api.tushare.pro', 'token': '',
            'enabled': True, 'timeout': 30.0, 'requests_per_minute': 60}


def validate_endpoint(value):
    try:
        url = urlsplit(value)
        valid = url.scheme in ('http', 'https') and url.hostname and not (url.username or url.password or url.query or url.fragment)
        url.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('API端点必须是无凭据、无查询参数的HTTP(S)地址')
    return value.rstrip('/')


def profile(raw, effective=True):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS) - {'id'}:
        raise ValueError('Tushare账号包含未知配置')
    result = dict(DEFAULTS, **raw)
    identifier = result.get('id', '')
    if not isinstance(identifier,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', identifier):
        raise ValueError('账号ID需要小写字母开头，最多32位字母、数字或下划线')
    if effective:
        for key in ('token', 'endpoint', 'timeout', 'requests_per_minute'):
            value = os.environ.get('PFOR_QMT_TUSHARE_' + identifier.upper() + '_' + key.upper())
            if value is not None:
                try:
                    result[key] = int(value) if key == 'requests_per_minute' else float(value) if key == 'timeout' else value
                except ValueError:
                    raise ValueError('Tushare账号环境变量类型无效') from None
    if not all(isinstance(result[k], str) for k in ('name', 'token', 'endpoint')) or not isinstance(result['enabled'], bool):
        raise ValueError('Tushare账号字段类型无效')
    for key in ('timeout', 'requests_per_minute'):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Tushare超时和请求频率必须大于0')
    if not isinstance(result['requests_per_minute'], int) or result['timeout'] > 300:
        raise ValueError('请求频率必须为整数，超时不超过300秒')
    result['endpoint'] = validate_endpoint(result['endpoint'])
    result['name'] = result['name'].strip() or identifier
    return result


def public(raw):
    result = profile(raw)
    result['token_configured'] = bool(result.pop('token'))
    result['managed_by_env'] = [key for key in ('token', 'endpoint', 'timeout', 'requests_per_minute')
                                if 'PFOR_QMT_TUSHARE_' + result['id'].upper() + '_' + key.upper() in os.environ]
    return result
