# Grape Worker 启动脚本（Windows，支持崩溃自动重启 daemon 保护）
# 等价 start-worker.sh，Windows 版用 Start-Process + WaitForExit 实现 daemon 循环。
param([switch]$NoDaemon)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkerDir = $env:GRAPE_WORKER_DIR
if (-not $WorkerDir) { $WorkerDir = $ScriptDir }
$Python = $env:GRAPE_PYTHON
if (-not $Python) { $Python = Join-Path $WorkerDir ".venv\Scripts\python.exe" }
$MaxRestarts = 10
$RestartWindow = 3600
$RestartDelay = 5

# ===== 公共路径：加载 runtime.env + 扩展 PATH =====
# 必须在 daemon 分支之前执行，确保子进程能继承环境变量
Set-Location $WorkerDir

$envPath = $env:PATH
$localBin = Join-Path $HOME ".local\bin"
$npmBin = Join-Path $HOME ".local\npm-global\bin"
if (Test-Path $localBin) { $envPath = "$localBin;$envPath" }
if (Test-Path $npmBin) { $envPath = "$npmBin;$envPath" }
$env:PATH = $envPath

$envFile = Join-Path $HOME ".grape\runtime.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^([A-Z_]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}

# ===== daemon 模式（默认）=====
if (-not $NoDaemon) {
    $RestartCount = 0
    $WindowStart = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    while ($true) {
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Write-Host "[$ts] Starting worker (attempt $($RestartCount + 1))..."
        # 注意：不用 -NoNewWindow + -RedirectStandardOutput（隐藏窗口下静默返回 $null）
        # Python logging 已写入 data/logs/devops-bot.log，不需要 stdout/stderr redirect
        $proc = Start-Process -FilePath $Python -ArgumentList '-m','src.runtime.agent' `
            -WorkingDirectory $WorkerDir -WindowStyle Hidden -PassThru
        $proc.WaitForExit()
        $ExitCode = $proc.ExitCode
        $Now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $Elapsed = $Now - $WindowStart
        if ($Elapsed -gt $RestartWindow) {
            $RestartCount = 0
            $WindowStart = $Now
        }
        $RestartCount++
        if ($RestartCount -ge $MaxRestarts) {
            Write-Host "[$ts] CRITICAL: Worker crashed $RestartCount times, stopping daemon."
            exit 1
        }
        Write-Host "[$ts] Worker exited (code=$ExitCode), restarting in ${RestartDelay}s... ($RestartCount/$MaxRestarts)"
        Start-Sleep -Seconds $RestartDelay
    }
    exit 0
}

# ===== 非 daemon 模式：前台直接运行 =====
& $Python -m src.runtime.agent
