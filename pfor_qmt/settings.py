"""One TOML configuration with non-persistent environment and CLI overrides."""
import hashlib
import hmac
import json
import math
import os
import secrets
from pathlib import Path

import tomlkit

# key: (TOML table, field, default, environment variable)
FIELDS = {
    'runtime_dir': ('app', 'runtime_dir', 'runtime', 'PFOR_QMT_RUNTIME_DIR'),
    'port': ('server', 'port', 8766, 'PFOR_QMT_PORT'),
    'ws_port': ('server', 'ws_port', 8767, 'PFOR_QMT_WS_PORT'),
    'dsn': ('database', 'dsn', '', 'PFOR_QMT_DATABASE_URL'),
    'qmt_root': ('qmt', 'root', '', 'PFOR_QMT_QMT_ROOT'),
    'pipe_name': ('qmt', 'pipe_name', r'\\.\pipe\pfor_qmt_pipe_hub', 'PFOR_QMT_PIPE_NAME'),
    'request_channel': ('qmt', 'request_channel', 'pfor_qmt.market.request', 'PFOR_QMT_REQUEST_CHANNEL'),
    'timeout': ('qmt', 'timeout', 15.0, 'PFOR_QMT_TIMEOUT'),
    'pipe_connect_timeout_ms': ('qmt', 'connect_timeout_ms', 1500, 'PFOR_QMT_PIPE_CONNECT_TIMEOUT_MS'),
    'heartbeat_seconds': ('qmt', 'heartbeat_seconds', 10.0, 'PFOR_QMT_PIPE_HEARTBEAT_SECONDS'),
    'pending_timeout': ('hub', 'pending_timeout', 60.0, 'PFOR_QMT_PIPE_HUB_PENDING_TIMEOUT'),
    'heartbeat_timeout': ('hub', 'heartbeat_timeout', 30.0, 'PFOR_QMT_PIPE_HUB_QMT_HEARTBEAT_TIMEOUT'),
    'maintenance_interval': ('hub', 'maintenance_interval', 2.0, 'PFOR_QMT_PIPE_HUB_MAINTENANCE_INTERVAL'),
    'api_key': ('security', 'api_key', '', 'PFOR_QMT_API_KEY'),
    'login_hash': ('security', 'login_hash', '', None),
    'event_retention_days': ('operations', 'event_retention_days', 90, 'PFOR_QMT_EVENT_RETENTION_DAYS'),
    'sample_retention_days': ('operations', 'sample_retention_days', 30, 'PFOR_QMT_SAMPLE_RETENTION_DAYS'),
    'repair_attempts': ('operations', 'repair_attempts', 3, 'PFOR_QMT_REPAIR_ATTEMPTS'),
}


class Settings:
    def __init__(self, runtime=None, config_path=None, port=None, ws_port=None):
        runtime = os.fspath(runtime) if runtime is not None else None
        self.path = Path(config_path or os.environ.get('PFOR_QMT_CONFIG') or 'config.toml').expanduser().resolve()
        self.overrides = {key:value for key,value in {'runtime_dir':runtime,'port':port,'ws_port':ws_port}.items() if value is not None}
        exists = self.path.exists()
        self._source_text = self.path.read_text('utf-8-sig') if exists else None
        try:
            self.document = tomlkit.parse(self._source_text) if exists else tomlkit.document()
        except (ValueError, tomlkit.exceptions.ParseError):
            # Parser messages may include a source line containing credentials.
            raise ValueError('config.toml 语法错误，请检查 TOML 格式') from None
        known = {(table,field) for table,field,_,_ in FIELDS.values()}
        for table, values in self.document.items():
            if table == 'tushare':
                if not isinstance(values, dict) or set(values) - {'accounts', 'default_account_id'}:
                    raise ValueError('无效Tushare配置段')
                continue
            if table not in {item[0] for item in known} or not isinstance(values, dict) or any((table,field) not in known for field in values):
                raise ValueError('config.toml 存在未知配置项或无效配置段')
        self.data = {key:self.document.get(table, {}).get(field, default) for key,(table,field,default,_) in FIELDS.items()}
        raw_accounts = self.document.get('tushare', {}).get('accounts', [])
        if not isinstance(raw_accounts, list) or any(not isinstance(value, dict) for value in raw_accounts):
            raise ValueError('tushare.accounts必须是账号配置数组')
        self.accounts = [dict(value) for value in raw_accounts]
        self.default_account_id = self.document.get('tushare', {}).get('default_account_id', '')
        self._validate()
        runtime_value = Path(self.value('runtime_dir')).expanduser()
        self.runtime = (self.path.parent / runtime_value).resolve() if not runtime_value.is_absolute() else runtime_value.resolve()
        legacy = self.runtime / 'settings.local.json'
        if not exists and legacy.is_file():
            previous = json.loads(legacy.read_text('utf-8'))
            for key in ('dsn','qmt_root','api_key','login_hash'):
                if key in previous:
                    self.data[key] = previous[key]
        if not self.data['api_key']:
            self.data['api_key'] = secrets.token_urlsafe(32)
        self._validate()
        self.runtime.mkdir(parents=True, exist_ok=True)
        if not exists or not self.document.get('security', {}).get('api_key'):
            self.save()

    def value(self, key):
        if key in self.overrides:
            return self.overrides[key]
        env = FIELDS[key][3]
        value = os.environ.get(env) if env else None
        if value is None:
            return self.data[key]
        default = FIELDS[key][2]
        try:
            if isinstance(default, int):
                return int(value)
            if isinstance(default, float):
                return float(value)
            return value
        except ValueError:
            raise ValueError('环境变量 %s 的类型无效' % env) from None

    def _validate(self):
        from .accounts import profile
        identifiers = []
        if not isinstance(self.default_account_id,str):
            raise ValueError('Tushare默认账号ID必须是字符串')
        for account in self.accounts:
            profile(account, effective=False)
            identifiers.append(profile(account)['id'])
        if len(set(identifiers)) != len(identifiers) or self.default_account_id and self.default_account_id not in identifiers:
            raise ValueError('Tushare账号ID重复或默认账号不存在')
        for key, (table,field,default,_) in FIELDS.items():
            for value in (self.data[key], self.value(key)):
                valid = isinstance(value,str) if isinstance(default,str) else isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value > 0
                if isinstance(default,int):
                    valid = valid and isinstance(value,int)
                if not valid:
                    raise ValueError('配置项 %s.%s 类型或范围无效' % (table,field))
        if any(not 1 <= self.value(key) <= 65535 for key in ('port','ws_port')) or self.value('port') == self.value('ws_port'):
            raise ValueError('HTTP/WebSocket 端口必须在 1..65535 且不能相同')
        if not self.value('runtime_dir'):
            raise ValueError('app.runtime_dir 不得为空')
        if self.value('sample_retention_days') > self.value('event_retention_days') or self.value('repair_attempts') > 3:
            raise ValueError('异常样本保留期不得超过事件保留期；自动补数最多3轮')
        if 'PFOR_QMT_API_KEY' in os.environ and not self.value('api_key'):
            raise ValueError('PFOR_QMT_API_KEY 不得为空')
        if not self.value('pipe_name').startswith('\\\\.\\pipe\\pfor_qmt') or not self.value('request_channel').startswith('pfor_qmt.'):
            raise ValueError('行情管道和通道必须使用 pfor_qmt 独立命名')

    @property
    def dsn(self):
        return self.value('dsn')

    @property
    def api_key(self):
        return self.value('api_key')

    @property
    def qmt_root(self):
        value = self.value('qmt_root')
        return str((self.path.parent / Path(value).expanduser()).resolve()) if value else ''

    @property
    def pipe(self):
        return {key:self.value(key) for key in ('pipe_name','request_channel','timeout','pipe_connect_timeout_ms','heartbeat_seconds')}

    def save(self):
        self._validate()
        current = self.path.read_text('utf-8-sig') if self.path.exists() else None
        if current != self._source_text:
            raise ValueError('config.toml 已被外部修改，请重启服务加载后再保存')
        doc = self.document.copy()
        for key,(table,field,_,_) in FIELDS.items():
            if table not in doc:
                doc[table] = tomlkit.table()
            doc[table][field] = self.data[key]
        if self.accounts or 'tushare' in doc:
            doc['tushare'] = {'default_account_id': self.default_account_id, 'accounts': self.accounts}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.local.toml')
        text = tomlkit.dumps(doc)
        temp.write_text(text, encoding='utf-8', newline='\n')
        temp.replace(self.path)
        self.document = doc
        self._source_text = text

    def set_password(self, password):
        if password and len(password) < 12:
            raise ValueError('网页登录密码至少 12 位')
        salt = secrets.token_hex(16)
        self.data['login_hash'] = salt + ':' + hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex() if password else ''

    def check_password(self, password):
        if not self.data['login_hash']:
            return False
        salt, expected = self.data['login_hash'].split(':')
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)

    def public(self):
        return {'database_configured': bool(self.dsn), 'qmt_root': self.qmt_root,
                'login_enabled': bool(self.data['login_hash']), 'schema': 'pfor_qmt',
                'timezone': 'Asia/Shanghai', 'adjustment': 'none', 'config_file': str(self.path)}

    def account(self, identifier=None):
        from .accounts import profile
        identifier = identifier or self.default_account_id
        raw = next((item for item in self.accounts if item['id'] == identifier), None)
        if raw is None:
            raise ValueError('Tushare账号不存在，请先配置账号')
        value = profile(raw)
        if not value['enabled'] or not value['token']:
            raise ValueError('Tushare账号已停用或Token未配置')
        return value

    def public_accounts(self):
        from .accounts import public
        return {'accounts': [public(item) for item in self.accounts], 'default_account_id': self.default_account_id}
