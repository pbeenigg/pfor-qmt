import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'pfor.ps1'
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows service management')


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture(params=['powershell', 'pwsh'])
def shell(request):
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(request.param + ' is not installed')
    return executable


def run_ps(shell, body, cwd=None):
    code = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n" + body
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    env = {key: value for key, value in os.environ.items() if not key.startswith('PFOR_QMT_')}
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-EncodedCommand', encoded], cwd=cwd, env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_service_identity_uses_windows_arguments_and_absolute_config(shell, tmp_path):
    config = str(tmp_path / '\u4e2d\u6587 project' / 'config.toml')
    python = r'C:\Python 3.12\python.exe'
    cases = [
        ([python, '-X', 'utf8', '-u', '-m', 'pfor_qmt.cli', '--config', config, 'serve'], True, config),
        ([python, '-m', 'pfor_qmt.cli', '--config=' + config, '--runtime', 'local run', 'serve'], True, config),
        ([r'C:\project\.venv\Scripts\pfor-qmt.exe', '--config', config, 'serve'], True, config),
        ([python, r'C:\project\.venv\Scripts\pfor-qmt.exe', '--config', config, 'serve'], True, config),
        ([python, '-m', 'pfor_qmt.cli', '--config', config + '.backup', 'serve'], True, config + '.backup'),
        ([python, '-m', 'pfor_qmt.cli', '--config', 'config.toml', 'serve'], True, None),
        ([python, '-m', 'pfor_qmt.cli', '--config', r'D:config.toml', 'serve'], True, None),
        ([python, '-m', 'pfor_qmt.cli', '--config', r'\config.toml', 'serve'], True, None),
        ([python, '-c', 'print("-m pfor_qmt.cli")', '--config', config, 'serve'], False, None),
        ([python, 'other.py', '-m', 'pfor_qmt.cli', '--config', config, 'serve'], False, None),
        ([python, '-m', 'cfquant.cli', '--config', config, 'serve'], False, None),
        ([python, '-m', 'pfor_qmt.cli', '--config', config, 'key'], False, None),
    ]
    values = [dict(command=subprocess.list2cmdline(args), service=service, path=path)
              for args, service, path in cases]
    encoded = base64.b64encode(json.dumps(values).encode()).decode()
    run_ps(shell, f"""
. {ps_string(SCRIPT)} help 6>$null
$cases = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')) | ConvertFrom-Json
foreach ($case in $cases) {{
    $result = Get-ServiceConfig $case.command
    if (($null -ne $result) -ne $case.service) {{ throw 'Service identity mismatch' }}
    if ($result -and $result.Path -ne $case.path) {{ throw 'Config path mismatch' }}
}}
""")


def test_stop_preserves_foreign_processes_and_reused_pid(shell):
    run_ps(shell, f"""
. {ps_string(SCRIPT)} help 6>$null
$script:queries = 0
$script:killed = @()
function Get-ServiceProcesses {{
    $script:queries++
    if ($script:queries -eq 1) {{
        [pscustomobject]@{{ Id = 100; Created = 1; Config = $configPath }}
        [pscustomobject]@{{ Id = 101; Created = 1; Config = $configPath }}
        [pscustomobject]@{{ Id = 102; Created = 1; Config = $configPath + '.other' }}
    }}
}}
function Get-CimInstance($ClassName, $Filter) {{
    $created = if ($Filter -eq 'ProcessId=101') {{ 2 }} else {{ 1 }}
    [pscustomobject]@{{ CreationDate = $created; CommandLine = 'python.exe -m pfor_qmt.cli --config "' + $configPath + '" serve' }}
}}
function Stop-Process($Id, $ErrorAction) {{ $script:killed += $Id }}
Stop-ServiceProcess
if ($script:killed.Count -ne 1 -or $script:killed[0] -ne 100) {{ throw 'Stopped an unowned or reused PID' }}
""")


def test_start_refuses_busy_port_and_duplicate_service(shell):
    run_ps(shell, f"""
. {ps_string(SCRIPT)} help 6>$null
function Get-ServiceProcesses {{}}
function Read-ServiceSettings {{ [pscustomobject]@{{ port = 8766; ws_port = 8767 }} }}
function Get-ServiceListeners {{ [pscustomobject]@{{ LocalPort = 8766; OwningProcess = 200 }} }}
function Start-Process {{ throw 'Unexpected spawn' }}
function Stop-Process {{ throw 'Unexpected stop' }}
$refused = $false
try {{ Start-ServiceProcess }} catch {{ $refused = $_.Exception.Message -like 'Configured ports are occupied:*' }}
if (-not $refused) {{ throw 'Port collision not rejected' }}
function Get-ServiceProcesses {{ [pscustomobject]@{{ Id = 300; Config = $configPath }} }}
$script:statusShown = $false
function Show-ServiceStatus {{ $script:statusShown = $true }}
Start-ServiceProcess
if (-not $script:statusShown) {{ throw 'Existing service not reported' }}
function Get-ServiceProcesses {{ [pscustomobject]@{{ Id = 301; Config = $null }} }}
$refused = $false
try {{ Start-ServiceProcess }} catch {{ $refused = $_.Exception.Message -like 'Another pfor-qmt service is running.*' }}
if (-not $refused) {{ throw 'Unidentifiable service not protected' }}
""")


def test_script_reuses_settings_without_exposing_credentials(shell, tmp_path):
    config = tmp_path / '\u4e2d\u6587 config.toml'
    config.write_text("[app]\nruntime_dir='separate logs'\n[server]\nport=18766\nws_port=18767\n"
                      "[security]\napi_key='offline-test-not-for-display'\n", encoding='utf-8')
    output = run_ps(shell, f"""
. {ps_string(SCRIPT)} help -Config {ps_string(config)} 6>$null
$env:PFOR_QMT_PORT = '18768'
$settings = Read-ServiceSettings
if ($settings.port -ne 18768 -or $settings.ws_port -ne 18767) {{ throw 'Settings precedence lost' }}
if ($settings.runtime -ne {ps_string(tmp_path / 'separate logs')}) {{ throw 'Wrong runtime base directory' }}
""", cwd=tmp_path)
    assert 'offline-test-not-for-display' not in output
