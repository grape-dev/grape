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

function Log([string]$msg) { Write-Host "[install-worker] $msg" }
function Fail([string]$msg) { Write-Error "[install-worker] 错误: $msg"; exit 1 }

# ===== 参数校验 =====
if (-not $Token) {
    # 尝试从环境变量读取
    $Token = $env:RUNTIME_TOKEN
}
if (-not $Token) {
    Fail "缺少 -Token（rta_ 凭据，在主控 WebUI「Workers」页生成）"
}

if (-not $Server) {
    $Server = "https://grape.example.com"
    Log "未指定 -Server，使用默认 $Server（如不对请加 -Server https://<你的主控域名>）"
}
$Server = $Server.TrimEnd('/')

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

Log "安装目录: $Dir | 设备名: $Device | 主控: $Server"

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
    Log "未找到系统 Python>=3.10，将尝试使用包内自带解释器（解压后自动探测）"
}

# ===== 2. 下载运行包（公开端点，无需鉴权）=====
$PkgUrl = "$Server/api/runtime/grape-worker.tar.gz"
$TmpPkg = Join-Path $env:TEMP "grape-worker-$(Get-Random).tar.gz"
Log "下载运行包: $PkgUrl"
try {
    Invoke-WebRequest -Uri $PkgUrl -OutFile $TmpPkg -UseBasicParsing -TimeoutSec 30
} catch {
    Remove-Item $TmpPkg -ErrorAction SilentlyContinue
    Fail "下载运行包失败: $PkgUrl（请确认主控地址/网络可达）"
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
    Log "已备份当前代码到 $upgradeBackupDir（升级失败时自动回滚）"
}

# restore_upgrade_backup: 恢复备份的旧代码并重启 agent（回滚函数，与 Linux 对齐）
function restore_upgrade_backup {
    Log "!!! 新 agent 启动失败，回滚到升级前代码并重启旧 agent !!!"
    $backupSrc = Join-Path $upgradeBackupDir "src"
    if (-not (Test-Path $backupSrc)) {
        Log "回滚失败: 无备份（$upgradeBackupDir 不存在，可能是全新安装）—— worker 将保持失联，需人工介入"
        return $false
    }
    if (Test-Path $srcDir) { Remove-Item $srcDir -Recurse -Force -ErrorAction SilentlyContinue }
    Copy-Item -Path $backupSrc -Destination $srcDir -Recurse -Force
    $backupVer = Join-Path $upgradeBackupDir "VERSION"
    if (Test-Path $backupVer) {
        Copy-Item -Path $backupVer -Destination $versionFilePath -Force -ErrorAction SilentlyContinue
    }
    $restoredVer = if (Test-Path $versionFilePath) { Get-Content $versionFilePath -ErrorAction SilentlyContinue } else { "未知版本" }
    Log "已恢复旧代码（$restoredVer）"
    # 重启旧 agent
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $startScript -WindowStyle Hidden -PassThru | Out-Null
    Start-Sleep -Seconds 5
    $restoredProc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*src.runtime.agent*"
    } | Select-Object -First 1
    if ($restoredProc) {
        Log "回滚成功：旧 agent 已恢复运行"
        return $true
    }
    Log "回滚后 agent 仍未运行—— worker 失联，需人工介入（日志: $workerLog）"
    return $false
}

Log "解压运行包到 $Dir"
try {
    # Windows 没有 tar 命令时用 .NET System.IO.Compression
    $tarCmd = Get-Command tar -ErrorAction SilentlyContinue
    if ($tarCmd) {
        # tar -xzf --strip-components=1（Windows 10+ 自带 bsdtar）
        # Windows tar 遇到符号链接会报错——$ErrorActionPreference="Stop" 会将 stderr 当作异常
        # 临时切换为 Continue，让 tar 正常返回退出码
        $tarErr = $null
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & tar -xzf $TmpPkg -C $Dir --strip-components=1 2>&1 | Out-Null
        } catch {
            $tarErr = $_
        }
        $ErrorActionPreference = $prevEAP
        if ($LASTEXITCODE -ne 0 -or $tarErr) {
            # 检查是否只是符号链接错误（关键文件已解压即可继续）
            $hasVersion = Test-Path (Join-Path $Dir "VERSION")
            $hasSrc = Test-Path (Join-Path $Dir "src")
            if (-not ($hasVersion -and $hasSrc)) {
                # 关键文件缺失——回退不带 strip 重试
                $tmpExtract = Join-Path $env:TEMP "grape-extract-$(Get-Random)"
                $ErrorActionPreference = "Continue"
                try {
                    & tar -xzf $TmpPkg -C $tmpExtract 2>&1 | Out-Null
                } catch {}
                $ErrorActionPreference = $prevEAP
                if (Test-Path (Join-Path $tmpExtract "grape-worker")) {
                    Copy-Item -Path (Join-Path $tmpExtract "grape-worker\*") -Destination $Dir -Recurse -Force
                    Remove-Item $tmpExtract -Recurse -Force -ErrorAction SilentlyContinue
                }
            } else {
                Log "tar 退出码非零（可能是符号链接错误），关键文件已解压，继续安装"
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
    Fail "解压运行包失败: $_"
}
Remove-Item $TmpPkg -ErrorAction SilentlyContinue

if (-not (Test-Path $CfgDir)) {
    New-Item -ItemType Directory -Path $CfgDir -Force | Out-Null
}
if (Test-Path (Join-Path $Dir "VERSION")) {
    Copy-Item (Join-Path $Dir "VERSION") $VersionFile -Force -ErrorAction SilentlyContinue
    $ver = Get-Content $VersionFile -ErrorAction SilentlyContinue
    if ($ver) { Log "Worker 版本: $ver" }
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
        Log "使用包内自带 Python"
    }
}
if (-not $Py) {
    Fail "未找到 Python>=3.10，请先安装 Python 3.10+ 后重试（https://python.org/downloads）"
}
Log "使用 Python: $Py ($(& $Py --version 2>&1))"

# ===== 4. 建 venv + 最小依赖 =====
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$VenvPip = Join-Path $Venv "Scripts\pip.exe"

if (-not (Test-Path $VenvPy)) {
    Log "创建 venv ($Py)"
    & $Py -m venv $Venv
}

if (-not (Test-Path $VenvPip)) {
    Log "venv 缺 pip, 手动安装 ensurepip..."
    & $VenvPy -m ensurepip --upgrade 2>$null
    if (-not (Test-Path $VenvPip)) {
        & $VenvPy -m pip install --upgrade pip 2>$null
    }
}

$reqFile = Join-Path $Dir "requirements-worker.txt"
if (Test-Path $reqFile) {
    $pipIndex = $env:PIP_INDEX_URL
    if (-not $pipIndex) { $pipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple" }

    # 离线 wheels 优先
    $wheelsDir = Join-Path $Dir "wheels"
    if ((Test-Path $wheelsDir) -and (Get-ChildItem "$wheelsDir\*.whl" -ErrorAction SilentlyContinue)) {
        Log "离线安装依赖（包内 wheels）"
        & $VenvPip install --quiet --no-index --find-links $wheelsDir -r $reqFile
        if ($LASTEXITCODE -ne 0) {
            Log "离线安装失败，回退在线安装..."
            & $VenvPip install --quiet -i $pipIndex --retries 3 --timeout 30 -r $reqFile
            if ($LASTEXITCODE -ne 0) {
                Fail "依赖安装失败：请重试本安装命令，或设置 PIP_INDEX_URL 环境变量指定可用 pip 镜像"
            }
        }
    } else {
        Log "在线安装依赖（镜像: $pipIndex，首次需数分钟）"
        & $VenvPip install --quiet -i $pipIndex --retries 3 --timeout 30 -r $reqFile
        if ($LASTEXITCODE -ne 0) {
            Fail "依赖安装失败：请重试本安装命令，或设置 PIP_INDEX_URL 环境变量指定可用 pip 镜像"
        }
    }
}

# ===== 4.5 安装 grape CLI（离线 wheel 优先，回退在线）=====
$grapeWheel = Get-ChildItem "$wheelsDir\grape-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($grapeWheel) {
    & $VenvPip install -q --no-index --find-links $wheelsDir grape 2>$null
    if ($LASTEXITCODE -eq 0) {
        Log "grape CLI 已装（离线 wheel）: $Venv\Scripts\grape.exe"
    } else {
        Log "离线 grape wheel 安装失败，回退在线..."
        & $VenvPip install -q --no-cache-dir "git+https://gitcode.com/grape-dev/grape-cli.git" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Log "grape CLI 已装（在线）"
        } else {
            Log "警告: grape-cli 安装失败，不影响 worker 运行"
        }
    }
} else {
    & $VenvPip install -q --no-cache-dir "git+https://gitcode.com/grape-dev/grape-cli.git" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Log "grape CLI 已装（在线）"
    } else {
        Log "警告: grape-cli 在线安装失败，不影响 worker 运行"
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
    Log "警告: 设置配置文件权限失败（不影响功能）"
}
Log "接入配置已写入 $EnvFile（token + 主控地址）"

# ===== 6. 生成启动脚本并启动 =====
$startScript = Join-Path $Dir "start-worker.ps1"

# 每次升级都重新下载 start-worker.ps1（与 Linux 侧 cat > $START_SCRIPT 行为对齐，
# 确保升级时 daemon 逻辑也同步更新，避免旧版 start-worker.ps1 与新代码不匹配）
Log "下载 start-worker.ps1..."
try {
    $startUrl = "$Server/api/runtime/start-worker.ps1"
    irm $startUrl -OutFile $startScript
    Log "start-worker.ps1 已下载"
} catch {
    Log "警告: 下载 start-worker.ps1 失败: $_（不影响安装，worker 可手动启动）"
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
    Log "新 agent 启动失败（进程不存在），尝试回滚旧版本..."
    restore_upgrade_backup
    Fail "新 agent 启动失败（已尝试回滚旧版本），日志: $workerLog"
}

Log "新 agent 进程已启动（pid $($workerProc.ProcessId)），等待注册..."

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
    Log "已后台启动并注册成功（pid $($workerProc.ProcessId)），日志: $workerLog"
} else {
    Log "新 agent 进程存活但 30s 未注册成功，尝试回滚旧版本..."
    $rollbackOk = restore_upgrade_backup
    if ($rollbackOk) {
        Log "已回滚旧版本（新版本可能无法连接 master）"
    } else {
        Log "回滚失败，worker 状态异常，请人工检查: $workerLog"
    }
}

# ===== 6.5 hermes 沙箱降级标注 =====
Log "注意: hermes 沙箱（namespace/cgroup 隔离）在 Windows 上不支持，已降级关闭。其他引擎不受影响。"

# ===== 7. 执行引擎提示 =====
$engineHints = @()
$engines = @("opencode", "hermes", "codearts", "codex")
foreach ($eng in $engines) {
    $found = $false
    try { $cmd = Get-Command $eng -ErrorAction Stop; $found = $true } catch {}
    if (-not $found) {
        $exts = @(".cmd", ".exe", ".bat")
        foreach ($ext in $exts) {
            if (Get-Command "$eng$ext" -ErrorAction SilentlyContinue) { $found = $true; break }
        }
    }
    if (-not $found) {
        switch ($eng) {
            "opencode" { $engineHints += "  npm install -g opencode-ai（opencode 引擎）" }
            "hermes" { $engineHints += "  pip install 'hermes[acp]'（hermes 引擎，含 ACP 协议包——普通 pip install hermes 不带，会导致引擎在线但调不通）" }
            "codearts" { $engineHints += "  安装 CodeArts CLI（codearts 引擎）" }
            "codex" { $engineHints += "  npm install -g @openai/codex（codex 引擎）" }
        }
    }
}

if ($engineHints.Count -gt 0) {
    Log "未检测到部分执行引擎。请自行安装后重启 worker："
    foreach ($hint in $engineHints) { Log $hint }
    Log "  $startScript"
    Log "（引擎类型不限定，worker 启动会自动发现本机已装引擎并注册）"
}

Log "安装完成！验证注册："
Log "  查看日志: $workerLog  → 应看到 RuntimeAgent registered: [...]"
Log "  主控 WebUI Workers 页应出现设备 $Device"
