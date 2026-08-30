#!/usr/bin/env pwsh

$ErrorActionPreference = "Stop"

# 上报地址写死
$REPORT_URL = "http://skillsop.topxtopx.com/api/telemetry"

Write-Host ""
Write-Host "════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Huawei Cloud Skill Reporter 安装脚本 (Windows)" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$HomeDir = $env:USERPROFILE

# ── 路径 ──────────────────────────────────────────────────────
$ConfigDir = "$HomeDir\.config\huawei-cloud-skill-reporter"
$ConfigFile = "$ConfigDir\server_url.txt"

$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$HomeDir\.hermes" }
$HermesConfig = "$HermesHome\config.yaml"
$HermesPluginDir = "$HermesHome\plugins\huawei-cloud-skill-reporter"

$OpencodePluginDir = "$HomeDir\.config\opencode\plugins"
$OpencodePluginFile = "$OpencodePluginDir\huawei-cloud-skill-reporter.js"

# ── 1. 保存配置 ──────────────────────────────────────────────
Write-Host "[1/3] 保存配置..." -ForegroundColor Yellow
$null = New-Item -ItemType Directory -Path $ConfigDir -Force
$REPORT_URL | Out-File -FilePath $ConfigFile -Encoding utf8
Write-Host "     -> $ConfigFile"

# ── 2. 安装插件 ──────────────────────────────────────────────
Write-Host "[2/3] 安装插件..." -ForegroundColor Yellow

# Hermes Python 插件
$HermesSource = Join-Path $ScriptDir "hermes-plugin"
if (Test-Path $HermesSource) {
    $null = New-Item -ItemType Directory -Path $HermesPluginDir -Force
    Get-ChildItem -Path $HermesSource -File | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $HermesPluginDir $_.Name) -Force
    }
    $REPORT_URL | Out-File -FilePath (Join-Path $HermesPluginDir "server_url.txt") -Encoding utf8
    Write-Host "     Hermes Python  -> $HermesPluginDir"

    $configContent = Get-Content -Path $HermesConfig -Raw -ErrorAction SilentlyContinue
    if ($configContent -and $configContent.Contains("huawei-cloud-skill-reporter")) {
        Write-Host "     Hermes config.yaml 已有配置，跳过"
    } else {
        $null = New-Item -ItemType Directory -Path $HermesHome -Force
        if (-not (Test-Path $HermesConfig)) {
            "# Hermes Agent Configuration" | Out-File -FilePath $HermesConfig -Encoding utf8
        }
        Add-Content -Path $HermesConfig -Value @"

# ── Huawei Cloud Skill Reporter ──────────────────────────────
plugins:
  enabled:
    - huawei-cloud-skill-reporter
"@ -Encoding utf8
        Write-Host "     Hermes config.yaml 已更新"
    }
} else {
    Write-Host "     hermes-plugin 目录不存在，跳过" -ForegroundColor Yellow
}

# Opencode 插件
$OpencodeSource = Join-Path $ScriptDir "plugin\huawei-cloud-skill-reporter.js"
if (Test-Path $OpencodeSource) {
    $null = New-Item -ItemType Directory -Path $OpencodePluginDir -Force
    $content = Get-Content $OpencodeSource -Raw
    $content = $content -replace 'DEFAULT_URL = "http://skillsop.topxtopx.com/api/telemetry"', "DEFAULT_URL = `"$REPORT_URL`""
    Set-Content -Path $OpencodePluginFile -Value $content -Encoding utf8
    Write-Host "     Opencode       -> $OpencodePluginFile"
} else {
    Write-Host "     plugin/huawei-cloud-skill-reporter.js 不存在，跳过" -ForegroundColor Yellow
}

# ── 3. 验证 ──────────────────────────────────────────────────
Write-Host "[3/3] 验证安装..." -ForegroundColor Yellow
$checks = @(
    @("Hermes Python 插件", "$HermesPluginDir\__init__.py"),
    @("Hermes manifest", "$HermesPluginDir\plugin.yaml"),
    @("Opencode 插件", $OpencodePluginFile),
    @("配置文件", $ConfigFile)
)

foreach ($check in $checks) {
    $label = $check[0]
    $path = $check[1]
    $exists = Test-Path $path
    Write-Host "     $label: $(if ($exists) { '存在' } else { '缺失' })"
}

Write-Host ""
Write-Host "════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  安装完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  上报地址: $REPORT_URL"
Write-Host "  配置文件: $ConfigFile"
Write-Host ""
Write-Host "  已安装的插件:"
Write-Host "  • Hermes Python  -> $HermesPluginDir"
Write-Host "  • Opencode       -> $OpencodePluginFile"
Write-Host ""
Write-Host "  重启 OpenCode / Hermes 后生效" -ForegroundColor Yellow
Write-Host "════════════════════════════════════════════════" -ForegroundColor Cyan
