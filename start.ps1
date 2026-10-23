# Grape Windows PowerShell 一键启动脚本
#
# 功能：
#   1. 检查 Python 版本 (>= 3.9)
#   2. 创建/激活虚拟环境 (.venv)
#   3. 安装依赖（本地开发用 requirements-local.txt，可用 -Full 安装完整依赖）
#   4. 复制 .env.example → .env（首次启动）
#   5. 运行 doctor.py 自检
#   6. 启动开发模式服务
#
# 用法：
#   .\start.ps1              # 本地开发模式（最小依赖）
#   .\start.ps1 -Full        # 完整依赖（含 OBS/MySQL/飞书）
#   .\start.ps1 -NoInstall   # 跳过依赖安装，直接启动
#   .\start.ps1 -Production  # 生产模式（双进程 3000+8080）
#
# 首次运行如遇执行策略拦截：
#   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
#
param(
    [switch]$Full,
    [switch]$NoInstall,
    [switch]$Production,
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Write-Step([string]$msg) { Write-Host "`n[INFO] $msg" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

# ===== 1. Python 版本检查 =====
Write-Step "检查 Python 版本..."
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    $PythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $PythonCmd) {
    Write-Err "未找到 Python，请安装 Python 3.9+ (https://www.python.org/downloads/)"
}
$PythonExe = $PythonCmd.Source
$VerOutput = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$PyVer = [Version]$VerOutput
if ($PyVer -lt [Version]"3.9") {
    Write-Err "Python 版本 $($PyVer) 过低，需要 3.9+"
}
Write-OK "Python $VerOutput ($PythonExe)"

# ===== 2. 虚拟环境 =====
Write-Step "检查虚拟环境..."
$VenvDir = Join-Path $ScriptDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Step "创建虚拟环境 .venv..."
    & $PythonExe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Err "虚拟环境创建失败"
    }
    Write-OK "虚拟环境已创建"
} else {
    Write-OK "虚拟环境已存在"
}

# ===== 3. 依赖安装 =====
if (-not $NoInstall) {
    $ReqFile = if ($Full) { "requirements.txt" } else { "requirements-local.txt" }
    $ReqPath = Join-Path $ScriptDir $ReqFile

    if (-not (Test-Path $ReqPath)) {
        Write-Warn "依赖文件 $ReqFile 不存在，跳过安装"
    } else {
        Write-Step "安装依赖 ($ReqFile)..."
        Write-Host "  国内网络可加镜像加速: -i https://mirrors.aliyun.com/pypi/simple/"
        $InstallArgs = @("install", "-r", $ReqPath)
        & $VenvPip @InstallArgs 2>&1 | ForEach-Object {
            if ($_ -match 'error|ERROR') { Write-Host "  $_" -ForegroundColor Red }
            elseif ($_ -match 'Successfully installed') { Write-OK "依赖安装完成" }
            else { Write-Host "  $_" -ForegroundColor DarkGray }
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "依赖安装可能未完全成功，尝试继续..."
            Write-Host "  手动安装: $VenvPip install -r $ReqFile -i https://mirrors.aliyun.com/pypi/simple/"
        }
    }
}

# ===== 4. .env 文件 =====
Write-Step "检查 .env 配置文件..."
$EnvPath = Join-Path $ScriptDir ".env"
$EnvExample = Join-Path $ScriptDir ".env.example"

if (-not (Test-Path $EnvPath)) {
    if (Test-Path $EnvExample) {
        Copy-Item $EnvExample $EnvPath
        Write-OK "已从 .env.example 复制 .env（默认 SQLite + OBS 关闭）"
        Write-Host "  请编辑 .env 填入实际配置（如 GitCode Bot Token 等）"
    } else {
        Write-Warn ".env.example 不存在，请手动创建 .env"
    }
} else {
    Write-OK ".env 已存在"
}

# ===== 5. configs 目录 =====
Write-Step "检查 configs 目录..."
$CfgDir = Join-Path $ScriptDir "configs"
$TokenExample = Join-Path $CfgDir "global-bot-token.example.json"
$TokenFile = Join-Path $CfgDir "global-bot-token.json"

if (-not (Test-Path $TokenFile) -and (Test-Path $TokenExample)) {
    Copy-Item $TokenExample $TokenFile
    Write-OK "已从 global-bot-token.example.json 复制 global-bot-token.json"
    Write-Host "  请编辑填入 GitCode Bot Token / OAuth 凭据"
} else {
    Write-OK "global-bot-token.json 已存在或无模板"
}

# ===== 6. data 目录 =====
Write-Step "创建 data 目录..."
$DataDir = Join-Path $ScriptDir "data"
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    Write-OK "data 目录已创建"
} else {
    Write-OK "data 目录已存在"
}

# ===== 7. 自检 =====
if (-not $SkipDoctor) {
    Write-Step "运行环境自检 (doctor.py)..."
    $DoctorScript = Join-Path $ScriptDir "scripts\doctor.py"
    if (Test-Path $DoctorScript) {
        & $VenvPython $DoctorScript
        if ($LASTEXITCODE -eq 2) {
            Write-Err "自检发现错误，请修复后重试（或用 -SkipDoctor 跳过）"
        } elseif ($LASTEXITCODE -eq 1) {
            Write-Warn "自检有警告，继续启动..."
        }
    } else {
        Write-Warn "doctor.py 不存在，跳过自检"
    }
}

# ===== 8. 启动服务 =====
Write-Step "启动 Grape 服务..."
if ($Production) {
    Write-Host "  模式: 生产 (主 API 3000 + Webhook 8080)"
    & $VenvPython -m src.api.run
} else {
    Write-Host "  模式: 开发 (单端口 3000, 热重载)"
    Write-Host "  打开 http://localhost:3000"
    & $VenvPython -m src.api.run --dev
}
