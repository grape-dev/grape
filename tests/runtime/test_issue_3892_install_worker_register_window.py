"""Issue #3892: install-worker.ps1 升级注册等待窗口回归。

背景（实测）：Windows 冷盘 + Defender 实时扫描 + anaconda 冷启动时，新 agent 07:03:25.452
才写出 `RuntimeAgent registered`，而旧的固定 30s 窗口在 07:03:15 就到期 →
`restore_upgrade_backup` 把健康的新版本杀回旧代码，机器此后长期停留在旧版本；回滚之后脚本
还统一打印 `Install complete!`，把回滚事实掩盖成「升级成功」。

覆盖两层：
1. 静态守卫（Linux CI 恒可跑）：窗口常量（30s 软到期 / 60s 硬上限 / 3s 粒度）、注册判据、
   在途/硬失败迹象、快速失败路径未被放宽、结束提示分支（回滚路径不得出现 `Install complete!`）、
   回滚前主控交叉验证的静默降级、日志水位（只读本轮新增行）与硬失败跳过交叉验证。
2. 行为用例（本机有 pwsh 时才跑）：把脚本里的注册等待整段
   （Issue #3892 标记 → 文件末尾）原样抽到 harness 里，用桩替换 Start-Sleep/Get-CimInstance/
   Invoke-RestMethod，按秒推进模拟日志，验证四条验收：慢注册不被回滚、在途迹象续等到 60s、
   无迹象/硬失败立即停等、回滚路径提示如实；以及 append-only 历史日志（上一轮 Traceback /
   Register failed / registered）不参与本轮判定、硬失败不被主控心跳翻案。

CI 门禁：本文件已接入 `.github/workflows/windows-smoke.yml` 的 `windows-smoke` job
（windows-latest 自带 pwsh），否则 10 条行为用例在无 PowerShell 的环境恒 skip、回归无门禁。
"""
import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
PS1 = os.path.join(REPO, "scripts", "install-worker.ps1")

# 注册等待整段的抽取边界（行为用例按标记抽取，不复制实现，避免与被测代码漂移）
REGION_START = "# Issue #3892：注册等待窗口"
END_MESSAGE_MARK = "# ===== 7. 结束提示"


def _ps1() -> str:
    assert os.path.isfile(PS1), "scripts/install-worker.ps1 should exist"
    with open(PS1, encoding="utf-8-sig") as f:
        return f.read()


def _region(text: str = None) -> str:
    text = text if text is not None else _ps1()
    start = text.find(REGION_START)
    assert start != -1, "install-worker.ps1 应包含 Issue #3892 注册等待段"
    return text[start:]


# ── 1. 窗口常量 ────────────────────────────────────────────────────────
class TestRegisterWindowWidth:
    def test_window_is_60s_with_30s_soft_deadline(self):
        text = _ps1()
        assert re.search(r"^\$regSoftDeadlineSec\s*=\s*30", text, re.M), \
            "软到期应为 30s（= 原固定窗口长度）"
        assert re.search(r"^\$regWindowSec\s*=\s*60", text, re.M), \
            "注册等待硬上限应为 60s（20 × 3s）"
        assert re.search(r"^\$regIntervalSec\s*=\s*3", text, re.M), \
            "采样粒度应为 3s（与旧实现一致）"

    def test_old_fixed_10_loop_window_is_gone(self):
        text = _ps1()
        region = _region(text)
        assert "for ($i = 1; $i -le 10; $i++)" not in region, \
            "旧的 10×3s=30s 固定注册循环应已被 60s 窗口取代"
        assert "Agent alive but not registered after 30s" not in text, \
            "回滚日志不应再写死 30s（实际等待时长改为按 $regElapsed 输出）"

    def test_registration_marker_still_matched_in_new_lines(self):
        region = _region()
        assert '"RuntimeAgent registered"' in region, "注册判据标记不得改动"
        assert "Get-NewAgentLogLines -LogPath $workerLog -Offset $agentLogOffset" in region, \
            "注册判定沿用日志读取，但只读本轮水位之后的新增行"


# ── 2. 在途 / 硬失败迹象 ───────────────────────────────────────────────
class TestInFlightJudgement:
    def test_in_flight_pattern_covers_register_and_heartbeat(self):
        region = _region()
        m = re.search(r"^\$regInFlightPattern\s*=\s*'([^']*)'", region, re.M)
        assert m, "应声明 $regInFlightPattern"
        pattern = m.group(1)
        # 注册 RPC 已发出 / 心跳相关——验收要求的两类在途迹象
        assert "registering with" in pattern
        assert "heartbeat" in pattern
        assert "/register" in pattern

    def test_failed_pattern_covers_register_rejection(self):
        region = _region()
        m = re.search(r"^\$regFailedPattern\s*=\s*'([^']*)'", region, re.M)
        assert m, "应声明 $regFailedPattern"
        pattern = m.group(1)
        assert "Register failed" in pattern
        assert "register 401" in pattern

    def test_patterns_are_ascii_only(self):
        """Windows PowerShell 5.1 的 Get-Content 无 BOM 时按 ANSI 解码，中文匹配不中。"""
        region = _region()
        for name in ("$regInFlightPattern", "$regFailedPattern"):
            line = next(l for l in region.splitlines() if l.strip().startswith(name))
            assert not re.search(r"[^\x00-\x7f]", line.split("=", 1)[1]), \
                f"{name} 只能用 ASCII 关键字（日志中文字符串会被 ANSI 解码成乱码）"

    def test_soft_deadline_decides_continue_or_stop(self):
        region = _region()
        soft = region.find("if ($regElapsed -ge $regSoftDeadlineSec -and -not $regSoftChecked)")
        assert soft != -1, "应在软到期处做一次在途判定"
        block = region[soft:soft + 1200]
        assert "Test-RegistrationInFlight" in block, "软到期应调用在途判定"
        assert "break" in block, "无在途迹象时必须立即停等（不得空等到 60s）"
        assert "extending the wait to" in block or "extending the wait" in block

    def test_in_flight_falls_back_to_process_liveness(self):
        """冷盘首扫时 30s 内可能一行日志都没落盘 → 以进程/daemon 存活为准。"""
        region = _region()
        fn = region[region.find("function Test-RegistrationInFlight"):]
        fn = fn[:fn.find("\n}\n") + 3]
        assert "$NewLog" in fn, "在途判定只看调用方给的本轮新增行"
        assert "-match $regFailedPattern" in fn
        assert "-match $regInFlightPattern" in fn
        assert "$AgentProcessAlive" in fn and "$DaemonAlive" in fn, \
            "无日志线索时必须回落到进程/daemon 存活判定（否则 35s 慢注册仍会被误杀）"


# ── 2b. 历史日志隔离（QA 审查 Required#1）─────────────────────────────
class TestStaleLogIsolation:
    """devops-bot.log 是 append-only（安装器不截断/不轮转），上一轮残留的 Traceback /
    Register failed 必须被水位挡在本轮判定之外，否则新 agent 在 0s 就被判死回滚。"""

    def test_log_offset_sampled_before_agent_start(self):
        text = _ps1()
        anchor = text.find("$workerLog = Join-Path $workerLogDir")
        assert anchor != -1
        offset = text.find("$agentLogOffset = 0L", anchor)
        start = text.find('Start-Process -FilePath "powershell.exe"', anchor)
        assert offset != -1 and start != -1 and offset < start, \
            "必须在启动新 agent 之前采样日志水位（否则上轮残留行会参与本轮判定）"

    def test_wait_loop_reads_only_new_lines(self):
        region = _region()
        assert "Get-NewAgentLogLines" in region and "-Offset $agentLogOffset" in region
        assert "Get-Content $workerLog -Tail 40" not in region, \
            "注册判定不得再读整份日志尾部（历史失败行会让新 agent 0s 被判死）"
        assert "Get-Content $LogPath -Tail 60" not in region, \
            "在途判定同样必须只看本轮新增行（历史 Register failed 会把 30s 软到期判成不在途）"

    def test_incremental_reader_tolerates_writer_and_rotation(self):
        region = _region()
        fn = region[region.find("function Get-NewAgentLogLines"):]
        fn = fn[:fn.find("\n}\n") + 3]
        assert "FileShare]::ReadWrite" in fn, "agent 正在追加写，读取不能独占文件"
        assert "if ($fs.Length -lt $Offset) { $Offset = 0 }" in fn, "轮转/截断后应退化为从 0 读"
        assert "StreamReader" in fn
        assert "return @()" in fn, "读不到（文件缺失/共享冲突）应返回空行集，交给存活判定兜底"

    def test_hard_failure_pattern_excludes_generic_markers(self):
        region = _region()
        m = re.search(r"^\$regFailedPattern\s*=\s*'([^']*)'", region, re.M)
        assert m, "应声明 $regFailedPattern"
        pattern = m.group(1)
        for generic in ("Traceback", "SystemExit", "worker exiting"):
            assert generic not in pattern, \
                f"{generic} 属泛化关键字（单个引擎探针失败也会打 Traceback），会短路掉可成功的启动"
        assert "Register failed" in pattern and "credential revoked" in pattern

    def test_hard_failure_skips_master_cross_check(self):
        """硬失败在 10s 内发生时，老 agent 的最后心跳仍在容差窗内 → 必须跳过交叉验证。"""
        region = _region()
        assert "$regHardFailed = $true" in region, "硬失败必须留下本轮标记"
        call = region.find("Test-MasterRuntimeRecentlySeen -SinceUtc")
        assert call != -1
        guard = region.rfind("if (-not $regOk", 0, call)
        assert guard != -1 and guard < call
        guard_line = region[guard:region.find("\n", guard)]
        assert "$regHardFailed" in guard_line, "交叉验证调用点必须排除硬失败分支"

    def test_process_alive_scoped_to_install_dir(self):
        region = _region()
        fn = region[region.find("function Test-NewAgentProcessAlive"):]
        fn = fn[:fn.find("\n}\n") + 3]
        assert "$Dir" in fn, \
            "同机可能有多份 grape 安装，只按 agent 命令行匹配会把别的安装判成本轮进程仍存活"


# ── 3. 快速失败路径不变 ────────────────────────────────────────────────
class TestFastFailUnchanged:
    def test_process_alive_loop_unchanged(self):
        text = _ps1()
        assert "for ($i = 1; $i -le 30; $i++)" in text, "进程判活仍是 30×1s"
        assert "Start-Sleep -Seconds 1" in text
        assert "Worker daemon exited early" in text, "daemon 早退日志保持不变"
        assert "New agent failed to start (process missing), rolling back..." in text

    def test_fast_fail_rollback_still_precedes_window(self):
        text = _ps1()
        fast = text.find("New agent failed to start (process missing), rolling back...")
        loop = text.find("while ($regElapsed -lt $regWindowSec)")
        assert fast != -1 and loop != -1 and fast < loop, \
            "进程缺失的快速回滚必须仍在注册等待之前"

    def test_hard_failure_short_circuits_wait(self):
        region = _region()
        idx = region.find('if ($logContent -match $regFailedPattern)')
        assert idx != -1, "等待循环内应识别硬失败迹象"
        assert "stopping the wait early" in region[idx:idx + 400]


# ── 4. 结束提示分支 ────────────────────────────────────────────────────
class TestEndMessageBranching:
    def test_install_complete_only_in_success_branch(self):
        text = _ps1()
        assert text.count("Install complete!") == 1, \
            "Install complete! 只应出现一次（成功分支），回滚路径不得出现"
        end = text.find(END_MESSAGE_MARK)
        assert end != -1, "应有结束提示分支段"
        block = text[end:]
        success = block.find('if ($installOutcome -eq "installed") {')
        rolled = block.find('elseif ($installOutcome -eq "rolled_back") {')
        assert -1 < success < block.find("Install complete!") < rolled, \
            "Install complete! 必须落在 installed 分支内、rolled_back 分支之前"
        rolled_block = block[rolled:block.find("} else {", rolled)]
        assert "Install complete!" not in rolled_block

    def test_rolled_back_prompt_has_version_backup_and_retry(self):
        text = _ps1()
        block = text[text.find(END_MESSAGE_MARK):]
        rolled = block[block.find('elseif ($installOutcome -eq "rolled_back") {'):]
        rolled = rolled[:rolled.find("} else {")]
        assert "ROLLED BACK" in rolled, "回滚路径应显式说明已回滚"
        assert "$rolledBackVer" in rolled, "回滚提示必须带旧版本标识"
        assert "$upgradeBackupDir" in rolled, "回滚提示必须带备份位置"
        assert "re-run the installer" in rolled, "回滚提示必须给出重试建议"
        assert "$workerLog" in rolled

    def test_rollback_failure_branch_keeps_abnormal_prompt(self):
        text = _ps1()
        block = text[text.find(END_MESSAGE_MARK):]
        tail = block[block.rfind("} else {"):]
        assert "Install FAILED" in tail and "OFFLINE" in tail
        assert "Install complete!" not in tail

    def test_outcome_tracked_by_rollback_result(self):
        region = _region()
        assert '$installOutcome = "installed"' in _ps1(), "默认结果应为 installed"
        assert '$installOutcome = "rolled_back"' in region, "回滚成功路径应标记 rolled_back"
        assert '$installOutcome = "rollback_failed"' in region, "回滚失败路径应标记 rollback_failed"

    def test_restore_reports_restored_version(self):
        text = _ps1()
        fn = text[text.find("function restore_upgrade_backup"):]
        fn = fn[:fn.find("\n# 解压前记录当前版本")]
        assert "$script:rolledBackVersion = \"unknown\"" in fn
        assert "$script:rolledBackVersion = \"$restoredVer\"" in fn, \
            "回滚函数应把恢复到的旧版本标识交给结尾提示"


# ── 5. 主控交叉验证的静默降级 ─────────────────────────────────────────
class TestMasterCrossCheckDegrades:
    def test_cross_check_uses_rta_header_and_short_timeout(self):
        region = _region()
        fn = region[region.find("function Test-MasterRuntimeRecentlySeen"):]
        fn = fn[:fn.find("\n}\n") + 3]
        assert "api/runtime/whoami" in fn, "复用凭据下的既有只读端点（不新增主控接口）"
        assert 'Authorization = "Bearer $Token"' in fn
        assert "-TimeoutSec 5" in fn

    def test_cross_check_never_blocks_rollback(self):
        region = _region()
        fn = region[region.find("function Test-MasterRuntimeRecentlySeen"):]
        fn = fn[:fn.find("\n}\n") + 3]
        assert fn.count("catch") >= 3, "请求失败/无字段/解析失败都必须 catch"
        assert fn.count("return $false") >= 3, "任一失败分支降级为「未确认」而不是抛错"
        assert "throw" not in fn, "交叉验证不得向主流程抛错"

    def test_cross_check_call_site_only_after_window_expiry(self):
        region = _region()
        call = region.find("Test-MasterRuntimeRecentlySeen -SinceUtc")
        loop = region.find("while ($regElapsed -lt $regWindowSec)")
        assert loop != -1 and call > loop, "交叉验证只在等待窗口到期后调用"
        guard = region.rfind("if (-not $regOk)", 0, call)
        assert guard != -1 and guard < call, "仅在日志判定未注册时才做主控交叉验证"
        # 调用点唯一：guard 之前不得再出现同一调用
        assert region.count("Test-MasterRuntimeRecentlySeen -SinceUtc") == 1


# ── 6. 行为用例（需 pwsh）─────────────────────────────────────────────
PWSH = shutil.which("pwsh")

HARNESS_PRELUDE = r"""
$ErrorActionPreference = "Stop"
$script:tick = 0
# 桩里不能用 Write-Output：它会污染函数返回值（测试替身里的 Log 在被测函数里被调用时，
# 输出项会和 return $false 一起成为返回值 → if 判定为真）。真脚本的 Log 是 Write-Host。
function Log([string]$msg) { [Console]::WriteLine("LOG $msg") }
function Test-GrapeAgentProcess {
    param($Proc)
    if (-not $Proc -or -not $Proc.CommandLine) { return $false }
    return ($Proc.CommandLine -like "*runtime.engine.agent*")
}
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    if ($Filter -like "*python.exe*") {
        if ($env:HARNESS_PROC_ALIVE -eq "1") {
            # ExecutablePath/CommandLine 都带安装目录：实现侧按 $Dir 过滤（QA 审查 Optional）
            return [pscustomobject]@{
                ProcessId = 4242
                ExecutablePath = "/harness/venv/Scripts/python.exe"
                CommandLine = "/harness/venv/Scripts/python.exe -m src.domains.runtime.engine.agent"
            }
        }
        return $null
    }
    if ($Filter -like "*powershell.exe*") {
        if ($env:HARNESS_DAEMON_ALIVE -eq "1") {
            return [pscustomobject]@{ ProcessId = 4243; CommandLine = "powershell -File $startScript" }
        }
        return $null
    }
    return $null
}
function Invoke-RestMethod {
    param($Uri, $TimeoutSec, [switch]$UseBasicParsing, $Headers, $ErrorAction)
    switch ($env:HARNESS_WHOAMI) {
        "offline" { throw "connection refused" }
        "nofield" { return [pscustomobject]@{ credential_id = 1 } }
        "fresh"   { return [pscustomobject]@{ last_seen_at = ([datetime]::UtcNow.ToString("o")) } }
        "stale"   { return [pscustomobject]@{ last_seen_at = ([datetime]::UtcNow.AddMinutes(-10).ToString("o")) } }
        "epochfresh" { return [pscustomobject]@{ last_seen_at = [int]([datetime]::UtcNow - [datetime]::new(1970,1,1,0,0,0,[System.DateTimeKind]::Utc)).TotalSeconds } }
        default   { return [pscustomobject]@{ credential_id = 1 } }
    }
}
function Start-Sleep {
    param($Seconds)
    $script:tick += [int]$Seconds
    $regAt = [int]$env:HARNESS_REGISTER_AT_SEC
    if ($regAt -gt 0 -and $script:tick -ge $regAt) {
        Add-Content -Path $env:HARNESS_LOG -Value "RuntimeAgent registered: ['1'] -> http://master"
    }
    $inflightAt = [int]$env:HARNESS_INFLIGHT_AT_SEC
    if ($inflightAt -gt 0 -and $script:tick -ge $inflightAt) {
        Add-Content -Path $env:HARNESS_LOG -Value "No engines installed locally yet, registering with empty engine list"
    }
    $failAt = [int]$env:HARNESS_FAIL_AT_SEC
    if ($failAt -gt 0 -and $script:tick -ge $failAt) {
        Add-Content -Path $env:HARNESS_LOG -Value "Register failed (check RUNTIME_TOKEN / server URL)"
    }
    $tracebackAt = [int]$env:HARNESS_TRACEBACK_AT_SEC
    if ($tracebackAt -gt 0 -and $script:tick -ge $tracebackAt) {
        Add-Content -Path $env:HARNESS_LOG -Value "Traceback (most recent call last):"
        Add-Content -Path $env:HARNESS_LOG -Value "  engine probe failed: opencode not installed (continuing)"
    }
}
function restore_upgrade_backup {
    $script:rolledBackVersion = "0.9.9-old"
    return [bool]::Parse($env:HARNESS_ROLLBACK_OK)
}
"""


def _build_harness(tmp_path, region: str) -> str:
    log_path = os.path.join(str(tmp_path), "devops-bot.log")
    prefix = (
        "$Server = 'http://master.invalid'\n"
        "$Token = 'rta_harness'\n"
        "$startScript = '/harness/start-worker.ps1'\n"
        "$Device = 'worker-harness'\n"
        "$Dir = '/harness'\n"
        "$upgradeBackupDir = '/harness/.upgrade-backup'\n"
        "$workerLog = $env:HARNESS_LOG\n"
        "$workerProc = [pscustomobject]@{ ProcessId = 4242 }\n"
        "$agentStartUtc = [datetime]::UtcNow\n"
        "$installOutcome = 'installed'\n"
        # 真脚本在 Start-Process 之前采样水位；harness 在脚本启动时采样，等价于「上一轮日志已落盘」。
        "$agentLogOffset = if (Test-Path $env:HARNESS_LOG) { [long](Get-Item $env:HARNESS_LOG).Length } else { [long]0 }\n"
    )
    body = prefix + HARNESS_PRELUDE + "\n" + region + "\n"
    body += 'Write-Output ("RESULT regOk=$regOk elapsed=$regElapsed outcome=$installOutcome version=$rolledBackVer")\n'
    return body


def _run_harness(tmp_path, env_overrides: dict, pre_log=None):
    region = _region()
    script = os.path.join(str(tmp_path), "harness.ps1")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_build_harness(tmp_path, region))
    log_path = os.path.join(str(tmp_path), "devops-bot.log")
    if pre_log is not None:
        # 上一轮运行残留的日志：harness 在启动时按文件长度采样水位，这些行本轮不应参与判定。
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in pre_log))
    elif env_overrides.pop("_create_log", True):
        open(log_path, "w").close()
    env = dict(os.environ)
    env.update({
        "HARNESS_LOG": log_path,
        "HARNESS_PROC_ALIVE": "1",
        "HARNESS_DAEMON_ALIVE": "1",
        "HARNESS_ROLLBACK_OK": "true",
        "HARNESS_WHOAMI": "nofield",
        "HARNESS_REGISTER_AT_SEC": "0",
        "HARNESS_INFLIGHT_AT_SEC": "0",
        "HARNESS_FAIL_AT_SEC": "0",
        "HARNESS_TRACEBACK_AT_SEC": "0",
    })
    env.update({k: str(v) for k, v in env_overrides.items()})
    proc = subprocess.run([PWSH, "-NoProfile", "-File", script],
                          capture_output=True, text=True, timeout=180, env=env)
    assert proc.returncode == 0, f"harness failed: {proc.stdout}\n{proc.stderr}"
    m = re.search(r"RESULT regOk=(\w+) elapsed=(\d+) outcome=(\S+) version=(\S*)", proc.stdout)
    assert m, f"harness 未输出结果行: {proc.stdout}\n{proc.stderr}"
    return {"regOk": m.group(1) == "True", "elapsed": int(m.group(2)),
            "outcome": m.group(3), "version": m.group(4),
            "stdout": proc.stdout, "stderr": proc.stderr}


pytestmark_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh not installed (CI without PowerShell)")


@pytestmark_pwsh
class TestRegisterWindowBehaviour:
    def test_slow_registration_at_36s_is_not_rolled_back(self, tmp_path):
        """验收 1：冷盘/杀软扫描导致 36s 才注册 → 不被回滚，输出 Install complete!。"""
        res = _run_harness(tmp_path, {"HARNESS_REGISTER_AT_SEC": 36})
        assert res["regOk"] is True
        assert res["elapsed"] >= 36
        assert res["outcome"] == "installed"
        assert "Install complete!" in res["stdout"]

    def test_cold_start_without_log_lines_still_waits(self, tmp_path):
        """验收 1 关键：30s 时日志一行都没有、进程仍存活 → 按在途续等到 48s 注册成功。"""
        res = _run_harness(tmp_path, {"HARNESS_REGISTER_AT_SEC": 48, "_create_log": False})
        assert res["regOk"] is True
        assert res["elapsed"] >= 48

    def test_in_flight_log_evidence_extends_to_60s(self, tmp_path):
        """验收 2：窗口内出现注册在途迹象 → 续等，最终 57s 注册成功不被回滚。"""
        res = _run_harness(tmp_path, {"HARNESS_INFLIGHT_AT_SEC": 12, "HARNESS_REGISTER_AT_SEC": 57})
        assert res["regOk"] is True
        assert res["elapsed"] >= 57

    def test_no_evidence_and_dead_processes_stops_at_30s(self, tmp_path):
        """验收 4：无迹象 + 进程与 daemon 都已消失 → 30s 即停，不等满 60s。"""
        res = _run_harness(tmp_path, {
            "HARNESS_PROC_ALIVE": "0", "HARNESS_DAEMON_ALIVE": "0", "_create_log": False})
        assert res["regOk"] is False
        assert res["elapsed"] == 30
        assert res["outcome"] == "rolled_back"

    def test_hard_failure_short_circuits(self, tmp_path):
        """验收 4：日志出现硬失败（Register failed）→ 立即停等回滚。"""
        res = _run_harness(tmp_path, {"HARNESS_FAIL_AT_SEC": 9})
        assert res["regOk"] is False
        assert res["elapsed"] <= 12, "硬失败后不该继续空等到 60s"

    def test_expiry_rolls_back_with_honest_message(self, tmp_path):
        """验收 3：回滚路径结尾无 Install complete!，含旧版本标识 + 备份 + 重试建议。"""
        res = _run_harness(tmp_path, {"HARNESS_REGISTER_AT_SEC": 0, "HARNESS_PROC_ALIVE": "1",
                                      "HARNESS_WHOAMI": "offline"})
        assert res["regOk"] is False
        assert res["elapsed"] == 60, "窗口内无迹象但进程存活 → 等满 60s 硬上限"
        assert res["outcome"] == "rolled_back"
        out = res["stdout"]
        assert "Install complete!" not in out
        assert "Install ROLLED BACK" in out
        assert "0.9.9-old" in out
        assert "/harness/.upgrade-backup" in out
        assert "re-run the installer" in out

    def test_rollback_failure_message(self, tmp_path):
        res = _run_harness(tmp_path, {"HARNESS_ROLLBACK_OK": "false", "HARNESS_WHOAMI": "offline",
                                      "HARNESS_PROC_ALIVE": "1"})
        assert res["outcome"] == "rollback_failed"
        assert "Install FAILED" in res["stdout"]
        assert "Install complete!" not in res["stdout"]

    def test_registration_at_window_edge_is_not_missed(self, tmp_path):
        """窗口末尾（60s）才落盘的注册行必须被最后一次补检捕获，不得回滚。"""
        res = _run_harness(tmp_path, {"HARNESS_REGISTER_AT_SEC": 60})
        assert res["regOk"] is True
        assert res["elapsed"] == 60
        assert "Install complete!" in res["stdout"]

    def test_master_fresh_heartbeat_prevents_rollback(self, tmp_path):
        """交叉验证生效时（主控能看到窗口内心跳）不回滚；验收不依赖该项，故与降级分开验证。"""
        res = _run_harness(tmp_path, {"HARNESS_WHOAMI": "fresh"})
        assert res["regOk"] is True
        assert res["outcome"] == "installed"

    def test_master_stale_heartbeat_degrades_to_rollback(self, tmp_path):
        res = _run_harness(tmp_path, {"HARNESS_WHOAMI": "stale"})
        assert res["regOk"] is False
        assert res["outcome"] == "rolled_back"

    # ── QA 审查 Required#1/#2 的行为回归 ──────────────────────────────

    def test_previous_run_failure_lines_do_not_kill_this_round(self, tmp_path):
        """Required#1：append-only 日志里上一轮残留的 Traceback / Register failed /
        worker exiting 必须被水位挡掉——36s 才注册的新 agent 不得在 0s 被判死回滚。"""
        stale = [
            "Traceback (most recent call last):",
            '  File "agent.py", line 1, in <module>',
            "SystemExit: 1",
            "Register failed (check RUNTIME_TOKEN / server URL)",
            "worker exiting (code 42)",
        ]
        res = _run_harness(tmp_path, {"HARNESS_REGISTER_AT_SEC": 36}, pre_log=stale)
        assert res["regOk"] is True, res["stdout"]
        assert res["elapsed"] >= 36, "不得因历史失败行提前停等"
        assert res["outcome"] == "installed"
        assert "Install complete!" in res["stdout"]
        assert "stopping the wait early" not in res["stdout"]

    def test_stale_registered_line_is_not_accepted(self, tmp_path):
        """Required#1 反向：历史 registered 行不得顶替本轮注册（否则回滚场景会误报升级成功）。"""
        res = _run_harness(tmp_path, {"HARNESS_REGISTER_AT_SEC": 0, "HARNESS_WHOAMI": "offline",
                                      "HARNESS_PROC_ALIVE": "1"},
                           pre_log=["RuntimeAgent registered: ['old'] -> http://master"])
        assert res["regOk"] is False, res["stdout"]
        assert res["outcome"] == "rolled_back"
        assert "Install complete!" not in res["stdout"]

    def test_nonfatal_traceback_does_not_short_circuit_wait(self, tmp_path):
        """Required#1：本轮新增的 Traceback（引擎探针失败等非致命）不再被当硬失败短路。"""
        res = _run_harness(tmp_path, {"HARNESS_TRACEBACK_AT_SEC": 6, "HARNESS_REGISTER_AT_SEC": 36})
        assert res["regOk"] is True, res["stdout"]
        assert "stopping the wait early" not in res["stdout"]

    def test_hard_failure_not_masked_by_master_heartbeat(self, tmp_path):
        """Required#2：硬失败必须跳过主控交叉验证——老 agent 在 10s 容差窗内的心跳不得
        把已失败的本轮翻成「已注册」并打印 Install complete!。"""
        res = _run_harness(tmp_path, {"HARNESS_FAIL_AT_SEC": 9, "HARNESS_WHOAMI": "fresh"})
        assert res["regOk"] is False, res["stdout"]
        assert res["elapsed"] <= 12
        assert res["outcome"] == "rolled_back"
        assert "cross-check skipped (hard registration failure" in res["stdout"]
        assert "Install complete!" not in res["stdout"]