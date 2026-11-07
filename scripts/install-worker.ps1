# Grape Worker PowerShell 一键安装脚本（Windows 等价 install-worker.sh）
#
# 用法（凭据申请页生成，token 即 rta_ 凭据）：
#   irm https://<grape主控>/api/runtime/install.ps1 | iex
#   或带参数：
#   & ([scriptblock]::Create((irm https://<grape主控>/api/runtime/install.ps1))) -Token <rta_...> -Server https://<grape主控>
#
# 流程：
#   1. 从主控公开端点下载运行包 grape-worker.tar.gz（无需鉴权，包内无敏感信息）
#   2. 解压到用户目录（默认 $HOME\grape，-Dir 可改）
#   3. 建 venv + 安装最小依赖
#   4. 写入接入配置（凭据 token + 主系统地址）
#   5. 后台启动 runtime-agent（Start-Process）
#   6. 启动后自动注册到主控系统（RuntimeAgent registered）
#
# 幂等：重复执行安全（复用已有目录/venv，仅更新代码与配置并重启）。
# 不安装执行引擎（opencode 等由用户自行安装，worker 启动自动发现注册）。
param(
    [string]$Token = "",
    [string]$Server = "",
    [string]$Device = "",
    [string]$Dir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# Issue #429: pip 版本检查提示（stderr，如 "[notice] A new release of pip is available"）
# 在 Stop 模式下经 2>$null/2>&1 重定向会被包装成错误记录并终止脚本。从源头禁用该提示；
# 各 pip 调用点另有 EAP 防御层（Invoke-PipQuiet），双保险。
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

# 2026-11-07 Windows GBK incident: pip's auto_decode parses requirements files
# with the locale preferred encoding; on Chinese Windows (GBK locale) it dies on
# UTF-8 non-ASCII bytes with UnicodeDecodeError. requirements-worker.txt is kept
# ASCII-only for this reason; PYTHONUTF8=1 (Python 3.7+) is set per pip call in
# Invoke-PipQuiet (and restored afterwards) as a second line of defense for
# third-party requirements outside the package.

function Log([string]$msg) { Write-Host "[install-worker] $msg" }
function Fail([string]$msg) { Write-Error "[install-worker] ERROR: $msg"; exit 1 }

# Issue #429: 在 $ErrorActionPreference="Stop" 下，PowerShell 对原生命令做 2>$null 重定向
# 会把 stderr 行包装成错误记录，配合 Stop 变成终止性错误（pip "new release available"
# 提示即触发实测崩溃）。此包装器临时切 Continue 执行（stderr 合并后丢弃、不中断），
# 以 $LASTEXITCODE 判定真实成败，然后恢复原 EAP——与 tar 解压段既有模式对齐。
function Invoke-PipQuiet {
    param(
        [Parameter(Mandatory)][string]$CommandPath,
        [string[]]$Arguments
    )
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    # Issue #1409: force UTF-8 for Python child processes (pip requirements
    # parsing) on non-UTF-8 locales; scope strictly to this pip call and
    # restore afterwards so the resident worker daemon does not inherit it.
    $prevUTF8 = $env:PYTHONUTF8
    $env:PYTHONUTF8 = "1"
    $exitCode = 1
    try {
        & $CommandPath @Arguments 2>&1 | Out-Null
        $exitCode = $LASTEXITCODE
    } catch {
        $exitCode = 1
    } finally {
        if ($null -eq $prevUTF8) { Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue }
        else { $env:PYTHONUTF8 = $prevUTF8 }
    }
    $ErrorActionPreference = $prevEAP
    return $exitCode
}

# ===== 参数校验 =====
if (-not $Token) {
    # 尝试从环境变量读取
    $Token = $env:RUNTIME_TOKEN
}
if (-not $Token) {
    Fail "Missing -Token (rta_ credential, generate it from the controller WebUI Workers page)"
}

if (-not $Server) {
    $Server = "https://grape.example.com"
    Log "No -Server specified, using default $Server (add -Server https://<your-controller> if incorrect)"
}
$Server = $Server.TrimEnd('/')

if (-not $Device) {
    # 已有安装记录过设备名就沿用。auto-upgrade 重跑本脚本时不带 -Device，
    # 若在此回落到「worker-<主机名>」，设备名与引擎实例名会漂移，
    # 服务端按实例名派发随之失效（任务卡在 runtime_id=None）。
    $existingEnvFile = Join-Path (Join-Path $HOME ".grape") "runtime.env"
    if (Test-Path $existingEnvFile) {
        $existingDevice = (Select-String -Path $existingEnvFile -Pattern '^RUNTIME_DEVICE_NAME=(.+)$' -ErrorAction SilentlyContinue |
                           Select-Object -First 1)
        if ($existingDevice) {
            $Device = $existingDevice.Matches[0].Groups[1].Value.Trim()
            if ($Device) { Log "Reusing existing device name from runtime.env: $Device" }
        }
    }
}

if (-not $Device) {
    $rawHost = $env:COMPUTERNAME
    if (-not $rawHost) { $rawHost = [System.Net.Dns]::GetHostName() }
    $Device = "worker-" + ($rawHost.ToLower() -replace '[^a-z0-9]','-' -replace '-+','-' -replace '^-|-$','')
}

if (-not $Dir) {
    $Dir = Join-Path $HOME "grape"
}
$Venv = Join-Path $Dir ".venv"
$CfgDir = Join-Path $HOME ".grape"
$EnvFile = Join-Path $CfgDir "runtime.env"
$VersionFile = Join-Path $CfgDir "worker.version"

Log "Install dir: $Dir | Device: $Device | Server: $Server"

# ===== 1. 准备 Python >= 3.10 =====
function Find-Python {
    $candidates = @("python", "python3", "py")
    foreach ($py in $candidates) {
        try {
            $cmd = Get-Command $py -ErrorAction Stop
            $ver = & $py -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
            if ($ver) {
                $parts = $ver.Split('.')
                if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) {
                    return $cmd.Source
                }
            }
        } catch {}
    }
    return $null
}

$Py = Find-Python
if (-not $Py) {
    Log "No system Python>=3.10 found, will try bundled interpreter (auto-detected after extract)"
}

# ===== 2. 下载运行包（公开端点，无需鉴权）=====
$PkgUrl = "$Server/api/runtime/grape-worker.tar.gz"
$TmpPkg = Join-Path $env:TEMP "grape-worker-$(Get-Random).tar.gz"
Log "Downloading package: $PkgUrl"
try {
    Invoke-WebRequest -Uri $PkgUrl -OutFile $TmpPkg -UseBasicParsing -TimeoutSec 30
} catch {
    Remove-Item $TmpPkg -ErrorAction SilentlyContinue
    Fail "Download failed: $PkgUrl (check controller address/network)"
}

# ===== 3. 解压到安装目录（幂等，升级前备份旧代码供回滚）=====
if (-not (Test-Path $Dir)) {
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
}

# 升级回滚保护：覆盖代码前备份当前 src/ + VERSION（与 Linux install-worker.sh 对齐）。
# 备份只留最近一份（.upgrade-backup），旧备份先清，磁盘占用可控。
$upgradeBackupDir = Join-Path $Dir ".upgrade-backup"
$srcDir = Join-Path $Dir "src"
$versionFilePath = Join-Path $Dir "VERSION"
if (Test-Path $srcDir) {
    if (Test-Path $upgradeBackupDir) { Remove-Item $upgradeBackupDir -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $upgradeBackupDir -Force | Out-Null
    Copy-Item -Path $srcDir -Destination (Join-Path $upgradeBackupDir "src") -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $versionFilePath) {
        Copy-Item -Path $versionFilePath -Destination (Join-Path $upgradeBackupDir "VERSION") -Force -ErrorAction SilentlyContinue
    }
    Log "Backed up current code to $upgradeBackupDir (auto-rollback on upgrade failure)"
}

# restore_upgrade_backup: 恢复备份的旧代码并重启 agent（回滚函数，与 Linux 对齐）
function restore_upgrade_backup {
    Log "!!! New agent failed to start, rolling back to pre-upgrade code and restarting old agent !!!"
    $backupSrc = Join-Path $upgradeBackupDir "src"
    if (-not (Test-Path $backupSrc)) {
        Log "Rollback failed: no backup ($upgradeBackupDir missing, maybe fresh install) - worker stays offline, manual intervention needed"
        return $false
    }
    if (Test-Path $srcDir) { Remove-Item $srcDir -Recurse -Force -ErrorAction SilentlyContinue }
    Copy-Item -Path $backupSrc -Destination $srcDir -Recurse -Force
    $backupVer = Join-Path $upgradeBackupDir "VERSION"
    if (Test-Path $backupVer) {
        Copy-Item -Path $backupVer -Destination $versionFilePath -Force -ErrorAction SilentlyContinue
    }
    $restoredVer = if (Test-Path $versionFilePath) { Get-Content $versionFilePath -ErrorAction SilentlyContinue } else { "unknown" }
    Log "Restored old code ($restoredVer)"
    # 重启旧 agent
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $startScript -WindowStyle Hidden -PassThru | Out-Null
    Start-Sleep -Seconds 5
    $restoredProc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*src.runtime.agent*"
    } | Select-Object -First 1
    if ($restoredProc) {
        Log "Rollback OK: old agent restored and running"
        return $true
    }
    Log "Agent still not running after rollback - worker offline, check log: $workerLog"
    return $false
}

# 解压前记录当前版本 + 从主控取目标版本，供解压后做「版本内容」校验
# （旧实现只 Test-Path 判 VERSION/src 是否存在，升级场景下这两者恒存在，
#   会把「一个文件都没解压出来」的失败误判为成功）
$preVersion = ""
if (Test-Path $versionFilePath) {
    $preVersion = ((Get-Content $versionFilePath -Raw -ErrorAction SilentlyContinue) -replace '\s', '')
}
$expectedVersion = ""
try {
    $lvi = Invoke-RestMethod -Uri "$Server/api/runtime/install/latest" -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop
    $expectedVersion = ("" + $lvi.version).Trim()
    if ($expectedVersion) { Log "Target worker version from controller: $expectedVersion" }
} catch {
    Log "Warning: cannot fetch target version from $Server/api/runtime/install/latest - will fall back to changed-or-not check"
}

Log "Extracting runtime package to $Dir"
try {
    # Windows 没有 tar 命令时用 .NET System.IO.Compression
    # 优先使用 Windows 自带 bsdtar（%SystemRoot%\System32\tar.exe）。
    # 装了 MSYS/git 的节点（如 Hermes 自带的 git）PATH 里会有一个 GNU tar，
    # 它把 `C:\...` 当作「远端主机:路径」解析，报
    #   tar: Cannot connect to C: resolve failed
    # 并以 exit 2 结束、解压出 0 个文件；而旧校验只看 VERSION/src 是否存在，
    # 于是这种静默失败被当成成功 → 拿旧代码走完安装 → 版本不变 → 升级死循环。
    $tarCmd = $null
    $sysTar = Join-Path $env:SystemRoot "System32\tar.exe"
    if (Test-Path $sysTar) {
        $tarCmd = Get-Command $sysTar -ErrorAction SilentlyContinue
    }
    if (-not $tarCmd) {
        $tarCmd = Get-Command tar -All -ErrorAction SilentlyContinue |
                  Where-Object { $_.Source -notmatch '\\git\\usr\\bin\\|\\usr\\bin\\|\\msys|\\mingw' } |
                  Select-Object -First 1
    }
    # 注意：必须按解析出的路径调用，不能写 `& tar` —— 那样仍会命中 PATH 里的 MSYS tar
    $tarExe = if ($tarCmd) { $tarCmd.Source } else { $null }
    if ($tarExe) {
        # tar -xzf --strip-components=1（Windows 10+ 自带 bsdtar）
        # Windows tar 遇到符号链接会报错——$ErrorActionPreference="Stop" 会将 stderr 当作异常
        # 临时切换为 Continue，让 tar 正常返回退出码
        $tarErr = $null
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $tarExe -xzf $TmpPkg -C $Dir --strip-components=1 2>&1 | Out-Null
        } catch {
            $tarErr = $_
        }
        $ErrorActionPreference = $prevEAP
        $postVersion = ""
        if (Test-Path $versionFilePath) {
            $postVersion = ((Get-Content $versionFilePath -Raw -ErrorAction SilentlyContinue) -replace '\s', '')
        }
        # 成功判据是「VERSION 内容换成了新版本」，不是「文件还在」：升级场景下
        # VERSION/src 本来就在，只看存在性会把「没解压出任何文件」误判为成功。
        $versionChanged = ($postVersion -and $postVersion -ne $preVersion)
        if ($LASTEXITCODE -ne 0 -or $tarErr -or -not $versionChanged) {
            # 关键文件缺失 / 版本未变——回退不带 strip 重试
            $tmpExtract = Join-Path $env:TEMP "grape-extract-$(Get-Random)"
            $ErrorActionPreference = "Continue"
            try {
                & $tarExe -xzf $TmpPkg -C $tmpExtract 2>&1 | Out-Null
            } catch {}
            $ErrorActionPreference = $prevEAP
            if (Test-Path (Join-Path $tmpExtract "grape-worker")) {
                Copy-Item -Path (Join-Path $tmpExtract "grape-worker\*") -Destination $Dir -Recurse -Force
                Remove-Item $tmpExtract -Recurse -Force -ErrorAction SilentlyContinue
            }
            $postVersion = ""
            if (Test-Path $versionFilePath) {
                $postVersion = ((Get-Content $versionFilePath -Raw -ErrorAction SilentlyContinue) -replace '\s', '')
            }
            $versionChanged = ($postVersion -and $postVersion -ne $preVersion)
            if ($versionChanged) {
                Log "tar exit code non-zero (possible symlink error), key files extracted, continuing"
            } else {
                Log "Warning: fallback extraction also produced no new VERSION (still '$postVersion')"
            }
        }
    } else {
        # 无 tar：用 Python tarfile 解压
        & python -c "
import tarfile, os, sys
tmp = sys.argv[1]
dest = sys.argv[2]
with tarfile.open(tmp) as t:
    members = t.getmembers()
    for m in members:
        # 跳过符号链接（Windows 不支持）
        if m.issym() or m.islnk():
            continue
        parts = m.name.split('/', 1)
        if len(parts) > 1:
            m.name = parts[1]
            t.extract(m, dest)
        elif len(parts) == 1 and parts[0]:
            m.name = parts[0]
            t.extract(m, dest)
" $TmpPkg $Dir 2>$null
    }
} catch {
    Fail "Extract failed: $_"
}
Remove-Item $TmpPkg -ErrorAction SilentlyContinue

# ===== 解压后最终校验：目标版本没落地就明确失败 =====
# 宁可让本次安装报错并留下日志，也不要用旧代码继续走完安装流程 ——
# 那会让 worker 以旧版本重启，下一轮又发现版本不一致，形成无限升级循环。
$finalVersion = ""
if (Test-Path $versionFilePath) {
    $finalVersion = ((Get-Content $versionFilePath -Raw -ErrorAction SilentlyContinue) -replace '\s', '')
}
if ($expectedVersion) {
    if ($finalVersion -ne $expectedVersion) {
        Fail "Extract verification failed: expected VERSION=$expectedVersion but got '$finalVersion'. Extraction did not land - check that PATH does not shadow tar with MSYS/git (tar -xzf on C:\ paths exits 2)."
    }
    Log "Extract verified: VERSION=$finalVersion"
} elseif ($finalVersion -and $finalVersion -eq $preVersion) {
    Fail "Extract verification failed: VERSION unchanged ('$finalVersion') after extraction."
}

if (-not (Test-Path $CfgDir)) {
    New-Item -ItemType Directory -Path $CfgDir -Force | Out-Null
}
if (Test-Path (Join-Path $Dir "VERSION")) {
    Copy-Item (Join-Path $Dir "VERSION") $VersionFile -Force -ErrorAction SilentlyContinue
    $ver = Get-Content $VersionFile -ErrorAction SilentlyContinue
    if ($ver) { Log "Worker version: $ver" }
}

# data 目录
$dataDir = Join-Path $Dir "data"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}

# ===== 3.5 包内解释器补探测（解压后）=====
if (-not $Py) {
    $bundledPy = Join-Path $Dir "python\python.exe"
    if (Test-Path $bundledPy) {
        $Py = $bundledPy
        Log "Using bundled Python"
    }
}
if (-not $Py) {
    Fail "Python>=3.10 not found. Please install Python 3.10+ first (https://python.org/downloads)"
}
Log "Using Python: $Py ($(& $Py --version 2>&1))"

# ===== 4. venv + deps (B1: three-level pip self-healing) =====
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$VenvPip = Join-Path $Venv "Scripts\pip.exe"

function _VenvPipOk {
    return ((Invoke-PipQuiet $VenvPy @("-m", "pip", "--version")) -eq 0)
}

if (-not (Test-Path $VenvPy)) {
    Log "Creating venv ($Py)"
    & $Py -m venv $Venv
}

if (-not (Test-Path $VenvPip) -or -not (_VenvPipOk)) {
    Log "venv pip missing or broken, starting 3-level self-healing..."
    # Level 1: ensurepip
    Log "  [1/3] ensurepip --upgrade..."
    Invoke-PipQuiet $VenvPy @("-m", "ensurepip", "--upgrade") | Out-Null
    if (-not (_VenvPipOk)) {
        # Level 2: get-pip.py
        Log "  [2/3] ensurepip failed, trying get-pip.py..."
        $getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
        $getPkgTmp = Join-Path $env:TEMP "get-pip-$(Get-Random).py"
        try {
            Invoke-WebRequest -Uri $getPipUrl -OutFile $getPkgTmp -UseBasicParsing -TimeoutSec 30
            Invoke-PipQuiet $VenvPy @($getPkgTmp) | Out-Null
            Remove-Item $getPkgTmp -ErrorAction SilentlyContinue
        } catch {
            Log "  get-pip.py download failed: $_"
        }
        if (-not (_VenvPipOk)) {
            # Level 3: rebuild venv
            Log "  [3/3] pip severely broken (site-packages damaged), rebuilding venv..."
            $savedVer = ""
            if (Test-Path $VersionFile) { $savedVer = Get-Content $VersionFile -ErrorAction SilentlyContinue }
            Remove-Item $Venv -Recurse -Force -ErrorAction SilentlyContinue
            & $Py -m venv $Venv
            Invoke-PipQuiet $VenvPy @("-m", "ensurepip", "--upgrade") | Out-Null
            if (-not (_VenvPipOk)) {
                $getPkgTmp2 = Join-Path $env:TEMP "get-pip-$(Get-Random).py"
                try {
                    Invoke-WebRequest -Uri $getPipUrl -OutFile $getPkgTmp2 -UseBasicParsing -TimeoutSec 30
                    Invoke-PipQuiet $VenvPy @($getPkgTmp2) | Out-Null
                    Remove-Item $getPkgTmp2 -ErrorAction SilentlyContinue
                } catch {}
            }
            if ($savedVer) { Set-Content -Path $VersionFile -Value $savedVer -Encoding UTF8 -ErrorAction SilentlyContinue }
            if (_VenvPipOk) {
                Log "  venv rebuilt, pip restored"
            } else {
                Fail "venv 3-level self-healing all failed. Please manually delete $Venv and retry."
            }
        }
    }
    if (-not (Test-Path $VenvPip)) {
        $VenvPip3 = Join-Path $Venv "Scripts\pip3.exe"
        if (Test-Path $VenvPip3) { $VenvPip = $VenvPip3 }
    }
}

$reqFile = Join-Path $Dir "requirements-worker.txt"
if (Test-Path $reqFile) {
    $pipIndex = $env:PIP_INDEX_URL
    if (-not $pipIndex) { $pipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple" }

    # 离线 wheels 优先 —— Issue #499: 包内 wheels 为多平台矩阵（linux x86_64/aarch64 + win_amd64），
    # pip 按 wheel 平台标签自动精确匹配本机平台；不匹配平台（或 Python 版本不匹配）离线失败回退在线。
    $wheelsDir = Join-Path $Dir "wheels"
    $useWheels = $false
    if ((Test-Path $wheelsDir) -and (Get-ChildItem "$wheelsDir\*.whl" -ErrorAction SilentlyContinue)) {
        $useWheels = $true
    }
    if ($useWheels) {
        Log "Installing deps from bundled wheels (offline)"
        $rc = Invoke-PipQuiet $VenvPip @("install", "--quiet", "--no-index", "--find-links", $wheelsDir, "-r", $reqFile)
        if ($rc -ne 0) {
            Log "Offline install failed (platform wheel not bundled or python version mismatch), falling back to online..."
            $rc = Invoke-PipQuiet $VenvPip @("install", "--quiet", "-i", $pipIndex, "--retries", "3", "--timeout", "30", "-r", $reqFile)
            if ($rc -ne 0) {
                Fail "Dependency install failed. Retry the install command, or set PIP_INDEX_URL to specify a pip mirror"
            }
        }
    } else {
        Log "Installing deps online (mirror: $pipIndex, first run takes minutes)"
        $rc = Invoke-PipQuiet $VenvPip @("install", "--quiet", "-i", $pipIndex, "--retries", "3", "--timeout", "30", "-r", $reqFile)
        if ($rc -ne 0) {
            Fail "Dependency install failed. Retry the install command, or set PIP_INDEX_URL to specify a pip mirror"
        }
    }
}

# ===== 4.5 安装 grape CLI（离线 wheel 优先——grape wheel 为纯 Python 平台无关，回退在线）=====
$grapeWheel = $null
if ($useWheels) {
    $grapeWheel = Get-ChildItem "$wheelsDir\grape-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
}
if ($grapeWheel) {
    # rc 取 Invoke-PipQuiet 返回值（$LASTEXITCODE 在函数作用域内设置，不向调用者传播）
    $rc = Invoke-PipQuiet $VenvPip @("install", "-q", "--no-index", "--find-links", $wheelsDir, "grape")
    if ($rc -eq 0) {
        Log "grape CLI installed (offline wheel): $Venv\Scripts\grape.exe"
    } else {
        Log "Offline grape wheel install failed, falling back to online..."
        $rc = Invoke-PipQuiet $VenvPip @("install", "-q", "--no-cache-dir", "git+https://gitcode.com/grape-dev/grape-cli.git")
        if ($rc -eq 0) {
            Log "grape CLI installed (online)"
        } else {
            Log "Warning: grape-cli install failed, worker will run without grape CLI"
        }
    }
} else {
    $rc = Invoke-PipQuiet $VenvPip @("install", "-q", "--no-cache-dir", "git+https://gitcode.com/grape-dev/grape-cli.git")
    if ($rc -eq 0) {
        Log "grape CLI installed (online)"
    } else {
        Log "Warning: grape-cli online install failed, worker will run without grape CLI"
    }
}

# ===== 5. 写入接入配置（凭据 + 主系统地址）=====
# 保留已有 HERMES_CUSTOM_* 等 API key
$preservedKeys = @()
if (Test-Path $EnvFile) {
    $lines = Get-Content $EnvFile -ErrorAction SilentlyContinue
    foreach ($line in $lines) {
        if ($line -match '^HERMES_CUSTOM') {
            $preservedKeys += $line
        }
    }
}

$envContent = @"
RUNTIME_TOKEN=$Token
RUNTIME_SERVER_URL=$Server
RUNTIME_DEVICE_NAME=$Device
RUNTIME_MAX_CONCURRENT_TASKS=20
RUNTIME_POLL_INTERVAL=3
RUNTIME_HEARTBEAT_INTERVAL=15
"@

if ($preservedKeys.Count -gt 0) {
    $envContent += "`n" + ($preservedKeys -join "`n")
}

# 从环境变量注入 HERMES_CUSTOM API keys（防重装丢 key）
$hermesKeys = @(
    "HERMES_CUSTOM_API_DEEPSEEK_COM_API_KEY",
    "HERMES_CUSTOM_OPENGW_CLOUDDEVELOPER_CLUB_API_KEY"
)
foreach ($keyName in $hermesKeys) {
    $keyValue = [Environment]::GetEnvironmentVariable($keyName)
    if ($keyValue -and ($envContent -notmatch "^$keyName=")) {
        $envContent += "`n$keyName=$keyValue"
    }
}

Set-Content -Path $EnvFile -Value $envContent -Encoding UTF8 -Force
# Windows ACL：限制当前用户可读写（等价 Unix chmod 600）
try {
    $acl = Get-Acl $EnvFile
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($env:USERNAME, "FullControl", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -Path $EnvFile -AclObject $acl
} catch {
    Log "Warning: failed to set config file permissions (non-blocking)"
}
Log "Config written to $EnvFile (token + controller address)"

# ===== 6. 生成启动脚本并启动 =====
$startScript = Join-Path $Dir "start-worker.ps1"

# # Re-download start-worker.ps1 on every upgrade (aligns with Linux side; keeps daemon logic in sync)
Log "Downloading start-worker.ps1..."
try {
    $startUrl = "$Server/api/runtime/start-worker.ps1"
    irm $startUrl -OutFile $startScript
    Log "start-worker.ps1 downloaded"
} catch {
    Log "Warning: failed to download start-worker.ps1: $_ (non-blocking, worker can be started manually)"
}

# 停掉旧 daemon（start-worker.ps1 的 powershell 进程），防止旧 daemon 重启被杀的 worker → 双 daemon 循环竞争
# 按本安装目录的 start-worker.ps1 完整路径匹配，避免误杀同机其他 grape 安装的 daemon
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*$startScript*"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 1

# 停掉旧 worker（按 grape 目录路径匹配 python 进程）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -like "*grape*" -and $_.CommandLine -like "*src.runtime.agent*"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 1

# 后台启动（Start-Process，脱离当前会话）
$workerLogDir = Join-Path $Dir "data\\logs"
$workerLog = Join-Path $workerLogDir "devops-bot.log"

Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $startScript -WindowStyle Hidden -PassThru | Out-Null

# 启动验证 + 失败回滚（与 Linux install-worker.sh 对齐）：
# 1. 进程存活检查（3s 后）
# 2. 注册成功检查（最多等 30s，日志出现 "registered"）
# 3. 任一失败 → restore_upgrade_backup 恢复旧代码重启
Start-Sleep -Seconds 3

$workerProc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*src.runtime.agent*"
} | Select-Object -First 1

if (-not $workerProc) {
    Log "New agent failed to start (process missing), rolling back..."
    restore_upgrade_backup
    Fail "New agent failed to start (rollback attempted), log: $workerLog"
}

Log "New agent started (pid $($workerProc.ProcessId)), waiting for registration..."

# 等待注册成功（最多 30s）
$regOk = $false
for ($i = 1; $i -le 10; $i++) {
    if (Test-Path $workerLog) {
        $logContent = Get-Content $workerLog -Tail 40 -ErrorAction SilentlyContinue
        if ($logContent -match "RuntimeAgent registered") {
            $regOk = $true
            break
        }
    }
    Start-Sleep -Seconds 3
}

if ($regOk) {
    Log "Started in background and registered (pid $($workerProc.ProcessId)), log: $workerLog"
} else {
    Log "Agent alive but not registered after 30s, rolling back..."
    $rollbackOk = restore_upgrade_backup
    if ($rollbackOk) {
        Log "Rolled back to old version (new version may not reach master)"
    } else {
        Log "Rollback failed, worker state abnormal, check log: $workerLog"
    }
}

# ===== 6.5 hermes 沙箱降级标注 =====
Log "Note: hermes sandbox (namespace/cgroup isolation) is not supported on Windows, disabled. Other engines are unaffected."

# Issue #499: 执行引擎安装引导段已删除——worker 启动自动发现并注册本机已装引擎，
# 无需在安装期引导用户安装 opencode/hermes/codearts/codex。

Log "Install complete! Verify registration:"
Log "  Check log: $workerLog  -> should see RuntimeAgent registered: [...]"
Log "  Controller WebUI Workers page should show device $Device"
