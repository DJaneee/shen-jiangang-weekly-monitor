param(
    [ValidateSet('collect', 'digest', 'all')]
    [string]$Mode = 'all',
    [string]$PythonPath = '',
    [switch]$OfflineTest
)

$ErrorActionPreference = 'Stop'
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MonitorScript = Join-Path $PackageDir 'monitor.py'

if (-not $PythonPath -and $env:SHEN_MONITOR_PYTHON) {
    $PythonPath = $env:SHEN_MONITOR_PYTHON
}

if (-not $PythonPath) {
    $BundledPython = 'C:\Users\46260\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $BundledPython) {
        $PythonPath = $BundledPython
    }
}

if (-not $PythonPath) {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PythonCommand -and $PythonCommand.Source -notlike '*WindowsApps*') {
        $PythonPath = $PythonCommand.Source
    }
}

if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw '未找到可用Python。请安装Python 3.11+，或设置环境变量 SHEN_MONITOR_PYTHON 为python.exe的完整路径。'
}

$Arguments = @($MonitorScript, $Mode)
if ($OfflineTest) {
    $Arguments += @('--fixture', (Join-Path $PackageDir 'fixtures\sample_candidates.json'), '--include-all')
}

& $PythonPath @Arguments
exit $LASTEXITCODE

