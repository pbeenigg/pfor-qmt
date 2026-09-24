"""Isolated deployment for the managed QMT market model."""
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from xml.dom import minidom

from .qmt_strategy_package import build_package

MODEL = 'PFOR_MARKET'
EMBEDDED = ('__init__.py', 'version.py', 'config.py', 'protocol.py', 'pipe_transport.py',
            'symbols.py', 'policy.py', 'qmt_methods.py', 'quote.py', 'market_bridge.py', 'logging_i18n.py')


def root_path(value):
    root = Path(value).expanduser().resolve()
    if root.name.lower() in ('bin.x64', 'python'):
        root = root.parent
    if not (root / 'bin.x64' / 'XtItClient.exe').is_file():
        raise ValueError('请选择包含 bin.x64/XtItClient.exe 的大 QMT 目录')
    return root


def running(root):
    if os.name != 'nt':
        return False
    result = subprocess.run(['powershell', '-NoProfile', '-Command',
        '$ErrorActionPreference = "Stop"; [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); '
        'Get-CimInstance Win32_Process -Filter "name=\'XtItClient.exe\'" | '
        'ForEach-Object { if (-not $_.ExecutablePath) { throw "Process path unavailable" }; $_.ExecutablePath }'],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    if result.returncode:
        raise ValueError('无法核实 QMT 是否退出，请检查进程查询权限')
    target = (Path(root) / 'bin.x64' / 'XtItClient.exe').resolve()
    return any(Path(line.strip()).resolve() == target for line in result.stdout.splitlines() if line.strip())


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.pfor-tmp')
    temp.write_bytes(content)
    temp.replace(path)


def inspect_root(value):
    root = root_path(value)
    return {'root': str(root), 'running': running(root), 'model': MODEL,
            'queued': (root / 'formulas' / (MODEL + '.rzrk')).exists(),
            'imported': (root / 'python' / (MODEL + '.py')).exists(),
            'managed': (root / 'pfor_qmt_managed' / 'ownership.json').exists()}


def prepare(value, process_checker=running, pipe_config=None):
    root = root_path(value)
    if process_checker(root):
        raise ValueError('请退出 QMT 后再部署；不会自动结束终端')
    owner = root / 'pfor_qmt_managed' / 'ownership.json'
    queue = root / 'formulas' / (MODEL + '.rzrk')
    script = root / 'python' / (MODEL + '.py')
    if not owner.exists() and (queue.exists() or script.exists()):
        raise ValueError('存在同名未托管模型，部署已停止')
    folder = root / 'pfor_qmt_managed' / 'embedded'
    manifest = {}
    for name in EMBEDDED:
        content = (Path(__file__).parent / name).read_bytes()
        atomic(folder / 'pfor_qmt' / name, content)
        manifest[name] = hashlib.sha256(content).hexdigest()
    import qmt_scripts
    source = (Path(qmt_scripts.__file__).parent / 'PFOR_MARKET.py').read_text('ascii')
    source = source.replace("PFOR_RUNTIME = ''", 'PFOR_RUNTIME = ' + ascii(str(folder)))
    if pipe_config is not None:
        from .config import get_config
        if set(pipe_config) != set(get_config()):
            raise ValueError('无效的行情连接配置')
        source = source.replace('PFOR_PIPE = {}', 'PFOR_PIPE = ' + ascii(pipe_config))
    if script.exists():
        atomic(root / 'pfor_qmt_managed' / 'backups' / (uuid.uuid4().hex + '.py'), script.read_bytes())
        atomic(script, source.encode('gbk'))
    else:
        atomic(queue, build_package(MODEL, source))
    atomic(owner, json.dumps({'model': MODEL, 'files': manifest}, indent=2).encode())
    return {'state': 'restart_required' if script.exists() else 'waiting_import', 'model': MODEL,
            'message': '自有模型已更新，请启动并登录 QMT；现有启用设置保持不变' if script.exists() else '启动 QMT 完成模型导入，然后退出并执行模型启用'}


def activate(value, account='', process_checker=running):
    root = root_path(value)
    if process_checker(root):
        raise ValueError('请先退出 QMT')
    owner = root / 'pfor_qmt_managed' / 'ownership.json'
    if not owner.exists() or json.loads(owner.read_text())['model'] != MODEL:
        raise ValueError('请先准备 pfor-qmt 模型')
    if not (root / 'python' / (MODEL + '.py')).exists():
        raise ValueError('请先启动 QMT 完成模型导入，再退出终端')
    config = root / 'config' / 'indexUserConfig.xml'
    original = config.read_bytes()
    if len(original) > 16 * 1024 * 1024 or b'<!DOCTYPE' in original.upper() or b'<!ENTITY' in original.upper():
        raise ValueError('不支持的 XML 配置')
    doc = minidom.parseString(original)
    if doc.documentElement.tagName != 'ICUserConfigFile':
        raise ValueError('不支持的 QMT 配置版本')
    sections = doc.getElementsByTagName('strategyTrade')
    if len(sections) > 1:
        raise ValueError('存在多个模型配置段')
    section = sections[0] if sections else doc.createElement('strategyTrade')
    items = list(section.getElementsByTagName('item'))
    bindings = {(item.getAttribute('account'), item.getAttribute('accountType'), item.getAttribute('m_strAccountKey'))
                for item in items if item.getAttribute('account') and item.getAttribute('m_strAccountKey') and (not account or item.getAttribute('account') == account)}
    if len(bindings) != 1:
        raise ValueError('无法唯一识别终端模型绑定，请先在 QMT 为 PFOR_MARKET 手工选择账户并保存；此绑定不启用交易接口')
    binding = next(iter(bindings))
    matches = [item for item in items if item.getAttribute('name') == MODEL]
    if len(matches) > 1:
        raise ValueError('存在重复的 PFOR_MARKET 模型')
    item = matches[0] if matches else doc.createElement('item')
    if not matches:
        ids = [int(node.getAttribute('id')) for node in items if node.getAttribute('id').isdigit()]
        item.setAttribute('id', str(max(ids or [0]) + 1))
        section.appendChild(item)
    if not sections:
        doc.documentElement.appendChild(section)
    attrs = {'name': MODEL, 'account': binding[0], 'accountType': binding[1], 'm_strAccountKey': binding[2],
             'stock': 'SH000300', 'peroid': '86400', 'eStrategyType': '4', 'strategymall': '0',
             'specialType': '48', 'recover_type': '14', 'runname': '', 'strategyRemark': 'pfor-qmt managed market-only',
             'runMode': '1', 'startupAutorun': '1',
             'FromulaExpandData': json.dumps({'m_qsFormula': MODEL, 'm_qsAccount': binding[0], 'variableSize': '0'})}
    for key, value in attrs.items():
        item.setAttribute(key, value)
    if process_checker(root) or config.read_bytes() != original:
        raise ValueError('QMT 已启动或配置发生并发修改，请退出后重试')
    updated = doc.toxml(encoding='utf-8')
    minidom.parseString(updated)
    atomic(root / 'pfor_qmt_managed' / 'backups' / (uuid.uuid4().hex + '.xml'), original)
    atomic(config, updated)
    return {'state': 'restart_required', 'model': MODEL, 'message': '仅更新 PFOR_MARKET，请启动并登录 QMT 后检查连接'}
