#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'help')]
    [string]$Action = 'status',
    [string]$Config,
    [ValidateRange(1, 10000)]
    [int]$Tail = 80,
    [switch]$Follow,
    [switch]$ErrorLog
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not $Config) { $Config = $env:PFOR_QMT_CONFIG }
if (-not $Config) { $Config = 'config.toml' }
if (-not [IO.Path]::IsPathRooted($Config)) { $Config = Join-Path $projectRoot $Config }
$configPath = [IO.Path]::GetFullPath($Config)

# Use the Windows argument parser so quoted paths cannot match a different service.
if (-not ('Pfor.Arguments' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Pfor {
    public static class Arguments {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr CommandLineToArgvW(string command, out int count);
        [DllImport("kernel32.dll")]
        static extern IntPtr LocalFree(IntPtr memory);
        public static string[] Parse(string command) {
            int count;
            IntPtr memory = CommandLineToArgvW(command, out count);
            if (memory == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
            try {
                string[] result = new string[count];
                for (int i = 0; i < count; i++)
                    result[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, i * IntPtr.Size));
                return result;
            } finally { LocalFree(memory); }
        }
    }
}
'@
}

function Get-ServiceConfig([string]$CommandLine) {
    if (-not $CommandLine) { return }
    $arguments = [Pfor.Arguments]::Parse($CommandLine)
    $index = 1
    $executable = [IO.Path]::GetFileName($arguments[0])
    if ($executable -in @('python.exe', 'pythonw.exe')) {
        while ($index -lt $arguments.Count) {
            if ($arguments[$index] -in @('-X', '-W')) { $index += 2 }
            elseif ($arguments[$index] -cmatch '^-(u|B|E|I|s|S|q|O|OO)$') { $index++ }
            else { break }
        }
        if ($index + 1 -lt $arguments.Count -and $arguments[$index] -ceq '-m' -and $arguments[$index + 1] -ceq 'pfor_qmt.cli') {
            $index += 2
        } elseif ($index -lt $arguments.Count -and [IO.Path]::GetFileName($arguments[$index]) -ieq 'pfor-qmt.exe') {
            $index++
        } else { return }
    } elseif ($executable -ine 'pfor-qmt.exe') { return }
    $path = $null
    while ($index -lt $arguments.Count) {
        $argument = $arguments[$index]
        if ($argument -ceq '--config' -and $index + 1 -lt $arguments.Count) {
            $path = $arguments[$index + 1]
            $index += 2
        } elseif ($argument.StartsWith('--config=')) {
            $path = $argument.Substring(9)
            $index++
        } elseif ($argument -ceq '--runtime' -and $index + 1 -lt $arguments.Count) {
            $index += 2
        } elseif ($argument.StartsWith('--runtime=')) {
            $index++
        } elseif ($argument -ceq 'serve') {
            # CIM does not expose the process working directory. Never guess relative configs.
            if ($path -and ($path -match '^[A-Za-z]:[\\/]' -or $path.StartsWith('\\'))) {
                return [pscustomobject]@{ Path = [IO.Path]::GetFullPath($path) }
            }
            return [pscustomobject]@{ Path = $null }
        } else { return }
    }
}

function Get-ServiceProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='pfor-qmt.exe'" | ForEach-Object {
        $identity = Get-ServiceConfig $_.CommandLine
        if ($null -ne $identity) {
            [pscustomobject]@{ Id = $_.ProcessId; Created = $_.CreationDate; Config = $identity.Path }
        }
    }
}

function Read-ServiceSettings {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Missing .venv\Scripts\python.exe. Follow README.md to install the project.' }
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw "Config not found: $configPath" }
    $code = @'
import json, sys
from pfor_qmt.settings import Settings
try:
    settings = Settings(config_path=sys.argv[1])
    print(json.dumps(dict(runtime=str(settings.runtime), port=settings.value('port'), ws_port=settings.value('ws_port'))))
except Exception:
    sys.exit('Cannot load service settings. Check config.toml and PFOR_QMT_* overrides.')
'@
    Push-Location -LiteralPath $projectRoot
    try {
        $result = & $pythonPath -X utf8 -c $code $configPath
        if ($LASTEXITCODE -ne 0) { throw 'Failed to read service settings.' }
        return ($result | ConvertFrom-Json)
    } finally { Pop-Location }
}

function Get-ServiceListeners {
    Get-NetTCPConnection -State Listen | Select-Object LocalPort, OwningProcess
}

function Test-ServiceReady($Settings, $Processes) {
    $listeners = @(Get-ServiceListeners | Where-Object { $_.OwningProcess -in $Processes.Id })
    if ($Settings.port -notin $listeners.LocalPort -or $Settings.ws_port -notin $listeners.LocalPort) { return $false }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$($Settings.port)/" -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Show-ServiceStatus {
    $processes = @(Get-ServiceProcesses)
    $owned = @($processes | Where-Object { $_.Config -eq $configPath })
    Write-Host "Config: $configPath"
    if (-not $owned.Count) {
        Write-Host 'Status: stopped'
        if ($processes.Count) { Write-Host 'Another pfor-qmt service exists (different or relative config); it is not managed here.' }
        return
    }
    Write-Host "Status: running (PID: $($owned.Id -join ', '))"
    $settings = Read-ServiceSettings
    Write-Host "Web: http://127.0.0.1:$($settings.port)"
    Write-Host "HTTP/WebSocket ready: $(Test-ServiceReady $settings $owned)"
    Write-Host "Runtime: $($settings.runtime)"
}

function Stop-ServiceProcess {
    $owned = @(Get-ServiceProcesses | Where-Object { $_.Config -eq $configPath })
    foreach ($process in $owned) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)"
        if ($current -and $current.CreationDate -eq $process.Created -and (Get-ServiceConfig $current.CommandLine).Path -eq $configPath) {
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        }
    }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (@(Get-ServiceProcesses | Where-Object { $_.Config -eq $configPath }).Count) {
        if ($timer.Elapsed.TotalSeconds -ge 15) { throw 'Service did not stop. Check process permissions.' }
        Start-Sleep -Milliseconds 200
    }
    Write-Host 'Status: stopped. Saved checkpoints remain; requests already sent to QMT are not cancelled.'
}

function Start-ServiceProcess {
    $processes = @(Get-ServiceProcesses)
    if (@($processes | Where-Object { $_.Config -eq $configPath }).Count) {
        Write-Host 'Service is already running; no duplicate started.'
        Show-ServiceStatus
        return
    }
    if ($processes.Count) { throw 'Another pfor-qmt service is running. Stop it in its original terminal or use its absolute -Config path.' }
    $settings = Read-ServiceSettings
    $busy = @(Get-ServiceListeners | Where-Object { $_.LocalPort -in @($settings.port, $settings.ws_port) })
    if ($busy.Count) { throw "Configured ports are occupied: $($busy.LocalPort -join ', '). No process was stopped." }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $outputLog = Join-Path $settings.runtime "pfor-$stamp.out.log"
    $errorLogPath = Join-Path $settings.runtime "pfor-$stamp.err.log"
    $arguments = '-X utf8 -u -m pfor_qmt.cli --config "' + $configPath + '" serve'
    $launched = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $outputLog -RedirectStandardError $errorLogPath -PassThru
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt 30) {
        $owned = @(Get-ServiceProcesses | Where-Object { $_.Config -eq $configPath })
        if ($owned.Count -and (Test-ServiceReady $settings $owned)) {
            Write-Host "Started: http://127.0.0.1:$($settings.port) (PID: $($owned.Id -join ', '))"
            Write-Host "Log: $outputLog"
            return
        }
        if ($launched.HasExited) { break }
        Start-Sleep -Milliseconds 300
    }
    throw "Service did not become ready. Check status and logs: $errorLogPath"
}

function Show-ServiceLogs {
    $settings = Read-ServiceSettings
    $latest = Get-ChildItem -LiteralPath $settings.runtime -Filter 'pfor-*.out.log' -File | Sort-Object Name -Descending | Select-Object -First 1
    if ($latest) {
        $path = if ($ErrorLog) { Join-Path $latest.DirectoryName ($latest.Name -replace '\.out\.log$', '.err.log') } else { $latest.FullName }
    } else {
        $path = Join-Path $settings.runtime $(if ($ErrorLog) { 'server-error.log' } else { 'server.log' })
    }
    Write-Host "Log: $path"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Write-Host 'No log file yet.'; return }
    if ($Follow) { Get-Content -LiteralPath $path -Encoding UTF8 -Tail $Tail -Wait }
    else { Get-Content -LiteralPath $path -Encoding UTF8 -Tail $Tail }
}

try {
    switch ($Action) {
        'start' { Start-ServiceProcess }
        'stop' { Stop-ServiceProcess }
        'restart' { $null = Read-ServiceSettings; Stop-ServiceProcess; Start-ServiceProcess }
        'status' { Show-ServiceStatus }
        'logs' { Show-ServiceLogs }
        'help' {
            Write-Host @'
Usage: .\pfor.ps1 [start|stop|restart|status|logs|help] [-Config path]
       .\pfor.ps1 logs [-Tail 80] [-Follow] [-ErrorLog]
Default: status. start runs in the background. stop terminates the service process.
Config: -Config > PFOR_QMT_CONFIG > config.toml (relative paths use the script directory).
Logs: each start keeps a separate stdout/stderr pair in the configured runtime directory.
Only a service with the exact absolute config path is managed; QMT/PostgreSQL are untouched.
'@
        }
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
